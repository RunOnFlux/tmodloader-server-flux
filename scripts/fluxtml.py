#!/usr/bin/env python3
"""Every decision about the tModLoader server's files, its mods, and the hub's commands.

    fluxtml.py install                 make sure the tModLoader build TML_VERSION names is installed
                                       (GitHub releases); print its folder
    fluxtml.py sync-mods               resolve the Workshop mods and their dependencies, download them,
                                       write Mods/enabled.json (the entrypoint, before every start)
    fluxtml.py apply                   write serverconfig.txt from the environment and the panel
    fluxtml.py state <state> [msg]     record what the supervisor is doing (flux/state.json)
    fluxtml.py updates                 print "tml <installed> <latest>" and "mod <id> <name>" lines for
                                       what is out of date (the watcher)

    flux-tml status                    state, players, world, mods loaded, version (one JSON document)
    flux-tml settings                  the panel's settings, the env that overrides them, the choices
    flux-tml set-settings '<json>'     {"world": "MyWorld", "password": "...", "motd": "...", ...}
    flux-tml worlds                    the worlds on the server (.wld + .twld), the active one
    flux-tml delete-world <name>       delete a world that is not the active one
    flux-tml mods                      every mod: Workshop or uploaded, its dependencies, on or off
    flux-tml set-mods '<json>'         {"workshop": ["2824688072", ...], "disabled": ["CalamityModMusic"]}
    flux-tml mod-search-deps <id>      the Workshop items <id> requires, recursively (to preview)
    flux-tml modconfigs                the mods' server-side config files (ModConfigs/*.json)
    flux-tml set-modconfig <file> '<json>'  replace one of them
    flux-tml bans / unban <name|ip>    the ban list
    flux-tml restart                   save, stop and start again (applies the above)
    flux-tml backups                   the backups in flux/backups: name, size, time, why
    flux-tml restore <backup>          put a backup back: the server is saved and stopped, what is there
                                       now is backed up first ("pre-restore"), then it starts again
    flux-tml mark-pending <what>       the panel changed a file on the volume (an upload): restart applies it
    fluxtml.py backup <why>            the supervisor's backups (a "start" one only if the newest is old)
    fluxtml.py do-restore              the supervisor, between two generations: a restore asked for

Settings, mods and configs changed by the panel are applied on the NEXT start. Precedence: an
environment variable that is SET is applied on every start; one that is not set leaves the panel's
value, and then the default. Two exceptions: the size, difficulty and seed of a NEW world, where the
panel's choice wins (the environment is the default); and an EMPTY MOTD, password or seed variable,
which counts as unset.
"""

import calendar
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
import zlib

SERVER_DIR = os.environ.get("FLUX_SERVER_DIR", "/mnt/tml/server")
DATA_DIR = os.environ.get("FLUX_DATA_DIR", "/mnt/tml/data")
STATE_DIR = os.environ.get("FLUX_STATE_DIR", os.path.join(DATA_DIR, "flux"))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")
MODS_FILE = os.path.join(STATE_DIR, "mods.json")
MODS_STATE_FILE = os.path.join(STATE_DIR, "mods-state.json")
DEPS_CACHE = os.path.join(STATE_DIR, "workshop-deps.json")
CONFIG_FILE = os.path.join(STATE_DIR, "serverconfig.txt")
# The game's log is on the install volume (local), capped by the supervisor: flux-lib.sh.
GAME_LOG_DIR = os.environ.get("FLUX_GAME_LOG_DIR", os.path.join(SERVER_DIR, "flux-logs"))
GAME_LOG = os.path.join(GAME_LOG_DIR, "current.log")
BACKUP_DIR = os.environ.get("FLUX_BACKUP_DIR", os.path.join(STATE_DIR, "backups"))
BACKUP_INDEX = os.path.join(BACKUP_DIR, "index.json")
BACKUP_SUFFIX = "-tmodloader.tar.gz"
# What a backup holds, relative to the data volume (the mods themselves are downloaded again).
BACKUP_ITEMS = ["Worlds", "ModConfigs", "Mods", "banlist.txt", "flux/settings.json", "flux/mods.json"]
# A restore asked for by the panel: persistent, so it survives a container restart, and done by the
# supervisor between two generations (fluxtml.py do-restore), never under a running server.
RESTORE_REQUEST = os.path.join(STATE_DIR, "restore-request.json")
RELEASES_CACHE = os.path.join(STATE_DIR, "releases.json")
UPDATE_WANTED = os.path.join(STATE_DIR, "update-wanted.json")
UPDATE_FAILED = os.path.join(STATE_DIR, "update-failed.json")
PENDING_MARK = os.path.join(STATE_DIR, "pending-configs")
# How long an update that could not be installed is left alone before it is tried again.
UPDATE_RETRY_SECONDS = int(os.environ.get("FLUX_UPDATE_RETRY_HOURS", "6") or 6) * 3600
WORLDS_DIR = os.path.join(DATA_DIR, "Worlds")
MODS_DIR = os.path.join(DATA_DIR, "Mods")
MODCONFIGS_DIR = os.path.join(DATA_DIR, "ModConfigs")
BANLIST = os.path.join(DATA_DIR, "banlist.txt")
INSTALLS = os.path.join(SERVER_DIR, "tml")
WORKSHOP = os.path.join(SERVER_DIR, "workshop")
WORKSHOP_CONTENT = os.path.join(WORKSHOP, "steamapps", "workshop", "content", "1281930")
APP_ID = "1281930"
RELEASES_API = os.environ.get("FLUX_RELEASES_API", "https://api.github.com/repos/tModLoader/tModLoader/releases?per_page=100")
# The newest stable release without the API (no rate limit): GitHub redirects it to the tag's page.
RELEASES_LATEST = os.environ.get("FLUX_RELEASES_LATEST", "https://github.com/tModLoader/tModLoader/releases/latest")
RELEASE_ZIP = os.environ.get("FLUX_RELEASE_ZIP", "https://github.com/tModLoader/tModLoader/releases/download/{tag}/tModLoader.zip")
# The release list is read at most this often (each start and each hourly check would otherwise
# spend the API's 60 unauthenticated requests an hour per address, shared by every server on a node).
RELEASES_TTL = 3600
WORKSHOP_PAGE = "https://steamcommunity.com/sharedfiles/filedetails/?id={id}"
DETAILS_API = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"

SIZES = {"small": 1, "medium": 2, "large": 3}
DIFFICULTIES = {"classic": 0, "expert": 1, "master": 2, "journey": 3}
LANGUAGES = ["en-US", "de-DE", "it-IT", "fr-FR", "es-ES", "ru-RU", "zh-Hans", "pt-BR", "pl-PL"]
MAX_PLAYERS_CAP = 255
DEPS_TTL = 24 * 3600


# ------------------------------------------------------------------------------------------------
# Files and values
# ------------------------------------------------------------------------------------------------

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_text(path, text):
    """Written beside and renamed, so a cut-off write never leaves half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".flux-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json(path, data, indent=2):
    write_text(path, json.dumps(data, indent=indent, ensure_ascii=False) + "\n")


def out(data):
    print(json.dumps(data, ensure_ascii=False))


def fail(message, code=1):
    out({"ok": False, "error": message})
    sys.exit(code)


def log(message):
    print(f"# {message}")


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def clean_text(value, limit):
    value = re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).strip()
    return value[:limit]


def world_stem(value):
    """The file name a NEW world gets, without .wld: letters, digits, spaces, _ and -, never a
    path. Never used to find a world that is there: that is existing_world's (a world copied in by
    hand, "Bob's World.wld", would be cleaned into another name)."""
    value = os.path.basename(str(value).strip())
    value = re.sub(r"\.t?wld$", "", value, flags=re.I)
    value = re.sub(r"[^\w \-]", "", value).strip()
    return value[:64]


def world_files():
    """The stems of the worlds in Worlds/, exactly as their files are named."""
    if not os.path.isdir(WORLDS_DIR):
        return []
    return sorted((f[:-4] for f in os.listdir(WORLDS_DIR) if f.endswith(".wld") and len(f) > 4), key=str.lower)


def existing_world(value):
    """The stem of a world that IS in Worlds/, matched exactly as `worlds` lists it (a ".wld"
    ending is allowed); None when there is none. No cleaning: the name is compared, never used to
    build a path that was not listed."""
    name = str(value if value is not None else "")
    if name.lower().endswith(".wld"):
        name = name[:-4]
    return name if name and name in world_files() else None


def workshop_ids(value):
    """IDs or item links, separated by commas, spaces or new lines, in order, once each."""
    ids = []
    for item in re.split(r"[\s,;]+", str(value or "")):
        if not item:
            continue
        m = re.search(r"[?&]id=(\d{1,20})", item) or re.fullmatch(r"(\d{1,20})", item)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def load_settings():
    data = read_json(SETTINGS_FILE, {})
    return data if isinstance(data, dict) else {}


# The size, difficulty and seed of a NEW world: the environment is the deploy's DEFAULT, and a
# world created from the panel with its own choices gets those (the spec always sets the first
# two, so env-first would make the panel's New World form do nothing).
NEW_WORLD_KEYS = ("worldSize", "difficulty", "seed")
def effective(settings, env_name, key, default=None):
    """(value, source). An EMPTY variable counts as unset, for every setting: the marketplace
    writes the declared ones even when left blank, and a blank must never erase the panel's value
    (none of these settings has "empty = off")."""
    if key in NEW_WORLD_KEYS and key in settings:
        return settings[key], "panel"
    if env_name and str(os.environ.get(env_name, "")).strip():
        return os.environ[env_name], "env"
    if key in settings:
        return settings[key], "panel"
    return default, "default"


# ------------------------------------------------------------------------------------------------
# tModLoader builds (GitHub releases)
# ------------------------------------------------------------------------------------------------

def http_get(url, timeout=30, data=None, tries=3, headers=None):
    """GET (or POST with data). Steam's community pages answer 429 to bursts, for a while: those
    are retried, 5 then 15 seconds later."""
    import urllib.error
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "runonflux/tmodloader-server-flux", "Accept": "*/*", **(headers or {})})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == tries - 1:
                raise
            time.sleep(5 if attempt == 0 else 15)
    raise RuntimeError("unreachable")


def parse_version(tag):
    """v2026.08.3.0 -> (2026, 8, 3, 0); None for anything else."""
    m = re.fullmatch(r"v?(\d{4})\.(\d{1,2})\.(\d{1,3})\.(\d{1,3})", str(tag).strip())
    return tuple(int(x) for x in m.groups()) if m else None


def pick_release(releases, wanted):
    """The release tag TML_VERSION names: "stable" (the newest not marked pre-release), "preview"
    (the newest of all), or an exact tag. None when there is no such release."""
    wanted = (wanted or "stable").strip()
    tags = [(r.get("tag_name"), bool(r.get("prerelease"))) for r in releases if parse_version(r.get("tag_name") or "")]
    if wanted.lower() == "stable":
        tags = [t for t in tags if not t[1]]
    elif wanted.lower() != "preview":
        want = parse_version(wanted)
        return next((t for t, _ in tags if parse_version(t) == want), None)
    tags.sort(key=lambda t: parse_version(t[0]), reverse=True)
    return tags[0][0] if tags else None


def installed_versions():
    if not os.path.isdir(INSTALLS):
        return []
    found = [d for d in os.listdir(INSTALLS) if parse_version(d) and os.path.isfile(os.path.join(INSTALLS, d, "tModLoader.dll"))]
    return sorted(found, key=parse_version, reverse=True)


def github_headers():
    """GITHUB_TOKEN, when the deploy sets one: 5000 requests an hour instead of 60 per address."""
    token = str(os.environ.get("GITHUB_TOKEN", "")).strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def release_list(now=None):
    """The release list, from the cache on the data volume when it is younger than RELEASES_TTL or
    when GitHub said to wait (403/429, until its reset time). None when there is neither."""
    import urllib.error
    now = time.time() if now is None else now
    cache = read_json(RELEASES_CACHE, {}) or {}
    fresh = isinstance(cache.get("releases"), list) and now - cache.get("at", 0) < RELEASES_TTL
    if fresh or now < cache.get("backoffUntil", 0):
        return cache.get("releases")
    try:
        releases = json.loads(http_get(RELEASES_API, tries=1, headers=github_headers()))
        if not isinstance(releases, list):
            raise ValueError("not a release list")
        write_json(RELEASES_CACHE, {"at": int(now), "releases": [
            {"tag_name": r.get("tag_name"), "prerelease": bool(r.get("prerelease"))} for r in releases if isinstance(r, dict)]}, indent=None)
        return read_json(RELEASES_CACHE, {}).get("releases")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            reset = e.headers.get("X-RateLimit-Reset") or ""
            retry = e.headers.get("Retry-After") or ""
            until = int(reset) if reset.isdigit() else now + (int(retry) if retry.isdigit() else 3600)
            cache["backoffUntil"] = int(min(until, now + 6 * 3600))
            try:
                write_json(RELEASES_CACHE, cache, indent=None)
            except OSError:
                pass
            log(f"WARN GitHub's API asked to wait (HTTP {e.code}) until {time.strftime('%H:%M UTC', time.gmtime(cache['backoffUntil']))}: using what is known")
    except Exception:
        pass
    return cache.get("releases")


def latest_stable_from_redirect():
    """The newest stable tag from github.com/<repo>/releases/latest, which redirects to the tag's
    page: no API, no rate limit."""
    try:
        req = urllib.request.Request(RELEASES_LATEST, headers={"User-Agent": "runonflux/tmodloader-server-flux"})
        with urllib.request.urlopen(req, timeout=20) as r:
            tag = r.geturl().rstrip("/").rsplit("/", 1)[-1]
        return tag if parse_version(tag) else None
    except Exception:
        return None


def normalize_tag(wanted):
    """An exact version as GitHub names the tag: "2026.8.3.0" -> "v2026.08.3.0"."""
    v = parse_version(wanted)
    return f"v{v[0]}.{v[1]:02d}.{v[2]}.{v[3]}" if v else None


def latest_release_tag(wanted, now=None):
    """The tag TML_VERSION names. An exact version needs no lookup at all; stable falls back on the
    /releases/latest redirect when the API cannot answer; preview needs the list."""
    wanted = (wanted or "stable").strip()
    exact = wanted.lower() not in ("stable", "preview")
    releases = release_list(now)
    if releases:
        tag = pick_release(releases, wanted)
        if tag:
            return tag
    if exact:
        return normalize_tag(wanted)
    if wanted.lower() == "stable":
        return latest_stable_from_redirect()
    return None


def cmd_install():
    wanted = os.environ.get("TML_VERSION", "stable") or "stable"
    tag = latest_release_tag(wanted)
    have = installed_versions()
    if tag is None:
        exact = parse_version(wanted)
        local = [v for v in have if exact is None or parse_version(v) == exact]
        if local:
            log(f"WARN GitHub did not answer (or has no {wanted} release): starting the installed {local[0]}")
            print(os.path.join(INSTALLS, local[0]))
            return
        fail(f"no tModLoader release for TML_VERSION={wanted}, and none installed", 3)
    folder = os.path.join(INSTALLS, tag)
    if tag in have:
        log(f"tModLoader {tag} ({wanted}) is installed")
    else:
        log(f"installing tModLoader {tag} ({wanted}) from GitHub")
        os.makedirs(INSTALLS, exist_ok=True)
        staging = tempfile.mkdtemp(dir=INSTALLS, prefix=".flux-")
        try:
            archive = os.path.join(staging, "tModLoader.zip")
            last = None
            for attempt in range(4):
                try:
                    with open(archive, "wb") as f:
                        f.write(http_get(RELEASE_ZIP.format(tag=tag), timeout=300))
                    break
                except Exception as e:  # network: retried
                    last = e
                    time.sleep(5 * (attempt + 1))
            else:
                raise RuntimeError(f"download failed: {last}")
            with zipfile.ZipFile(archive) as z:
                z.extractall(os.path.join(staging, "tml"))
            os.replace(os.path.join(staging, "tml"), folder)
        except Exception as e:
            shutil.rmtree(staging, ignore_errors=True)
            mark_update_failed(f"tml:{tag}")
            if have:
                log(f"WARN could not install {tag} ({e}): starting the installed {have[0]}; not tried again for {UPDATE_RETRY_SECONDS // 3600} h")
                print(os.path.join(INSTALLS, have[0]))
                return
            fail(f"could not install tModLoader {tag}: {e}", 3)
        shutil.rmtree(staging, ignore_errors=True)
    # Older builds are removed: they are 170 MB each and never started again.
    for old in installed_versions():
        if old != tag:
            shutil.rmtree(os.path.join(INSTALLS, old), ignore_errors=True)
    print(folder)


# ------------------------------------------------------------------------------------------------
# .tmod files
# ------------------------------------------------------------------------------------------------

def read_7bit_string(f):
    n = shift = 0
    while True:
        b = f.read(1)
        if not b:
            raise ValueError("truncated")
        n |= (b[0] & 0x7F) << shift
        shift += 7
        if b[0] < 0x80:
            break
    return f.read(n).decode("utf-8", "replace")


def read_tmod(path):
    """The header of a .tmod: {name, version, tml, displayName, modReferences, side, author}.

    The format (tModLoader's TmodFile): "TMOD", the tML version it was built with, a SHA-1, a
    256-byte signature, the data length, then the mod's name, its version and the file table; the
    "Info" file is a list of key/value pairs (build.txt, serialized)."""
    with open(path, "rb") as f:
        if f.read(4) != b"TMOD":
            raise ValueError("not a .tmod")
        tml = read_7bit_string(f)
        f.read(20 + 256 + 4)
        name = read_7bit_string(f)
        version = read_7bit_string(f)
        count = struct.unpack("<i", f.read(4))[0]
        table = []
        for _ in range(count):
            fname = read_7bit_string(f)
            size, csize = struct.unpack("<ii", f.read(8))
            table.append((fname, size, csize))
        info = {}
        for fname, size, csize in table:
            data = f.read(csize)
            if fname == "Info":
                if csize != size:
                    data = zlib.decompress(data, -15)
                info = parse_info(data)
                break
    refs = [r.split("@")[0].strip() for r in info.get("modReferences", "").split(",") if r.strip()]
    return {"name": name, "version": version, "tml": tml, "displayName": info.get("displayName") or name,
            "modReferences": refs, "side": info.get("side") or "Both", "author": info.get("author")}


def parse_info(data):
    """build.txt as tModLoader serializes it: (key, value) strings until an empty key; list values
    are one string per item ending with an empty one. Read loosely: only strings matter here."""
    import io
    f = io.BytesIO(data)
    info = {}
    list_keys = {"modReferences", "weakReferences", "sortAfter", "sortBefore", "dllReferences"}
    try:
        while True:
            key = read_7bit_string(f)
            if not key:
                break
            if key in list_keys:
                items = []
                while True:
                    item = read_7bit_string(f)
                    if not item:
                        break
                    items.append(item)
                info[key] = ",".join(items)
            elif key in ("noCompile", "playableOnPreview", "translationMod", "hideCode", "hideResources", "includeSource"):
                continue  # flags: the key alone
            else:
                info[key] = read_7bit_string(f)
    except (ValueError, IndexError):
        pass
    return info


def tml_series(tml_version):
    """2026.8.3.0 -> (2026, 8): what Workshop folders are named after."""
    v = parse_version("v" + str(tml_version)) or parse_version(str(tml_version))
    return (v[0], v[1]) if v else None


def best_tmod(item_dir, series):
    """The .tmod of a Workshop item for this tModLoader: items keep one folder per tML release they
    were built for (2025.7, 2026.6...); the newest not newer than ours wins. Old items have the
    .tmod at the top."""
    folders = []
    for d in os.listdir(item_dir):
        m = re.fullmatch(r"(\d{4})\.(\d{1,2})", d)
        if m and os.path.isdir(os.path.join(item_dir, d)):
            folders.append(((int(m.group(1)), int(m.group(2))), d))
    folders.sort(reverse=True)
    for ver, d in folders:
        if series is None or ver <= series:
            tmods = [x for x in os.listdir(os.path.join(item_dir, d)) if x.endswith(".tmod")]
            if tmods:
                return os.path.join(item_dir, d, tmods[0])
    tmods = [x for x in os.listdir(item_dir) if x.endswith(".tmod")]
    return os.path.join(item_dir, tmods[0]) if tmods else None


# ------------------------------------------------------------------------------------------------
# Workshop: dependencies and downloads
# ------------------------------------------------------------------------------------------------

REQUIRED_RE = re.compile(r'filedetails/\?id=(\d+)"[^>]*>\s*<div class="requiredItem">\s*([^<]*?)\s*</div>')


def required_items(item_id, cache):
    """The items a Workshop item lists under "Required items" (its page: the Web API does not
    give them without a key). Cached for a day; on a network error the cache, however old."""
    entry = cache.get(item_id)
    if entry and time.time() - entry.get("at", 0) < DEPS_TTL:
        return entry["deps"]
    try:
        html = http_get(WORKSHOP_PAGE.format(id=item_id), timeout=20).decode("utf-8", "replace")
        start = html.find('id="RequiredItems"')
        deps = [{"id": i, "title": t} for i, t in REQUIRED_RE.findall(html[start:start + 20000])] if start >= 0 else []
        cache[item_id] = {"deps": deps, "at": int(time.time())}
        return deps
    except Exception:
        return entry["deps"] if entry else []


def resolve(ids, cache):
    """The items asked for and everything they require, dependencies first; and who required what."""
    order, seen, required_by = [], set(), {}

    def visit(item_id, parent):
        if parent:
            required_by.setdefault(item_id, []).append(parent)
        if item_id in seen:
            return
        seen.add(item_id)
        for dep in required_items(item_id, cache):
            visit(dep["id"], item_id)
        order.append(item_id)

    for item_id in ids:
        visit(item_id, None)
    return order, required_by


def download(ids):
    """One SteamCMD run for all of them, anonymously (the official manage script does the same).
    Retried: the Workshop sometimes times out ("Timed out waiting for AppInfo update")."""
    if not ids:
        return True
    os.makedirs(WORKSHOP, exist_ok=True)
    cmd = ["steamcmd", "+force_install_dir", WORKSHOP, "+login", "anonymous"]
    for item_id in ids:
        cmd += ["+workshop_download_item", APP_ID, item_id]
    cmd.append("+quit")
    for attempt in range(3):
        r = subprocess.run(cmd, capture_output=True, text=True)
        missing = [i for i in ids if not os.path.isdir(os.path.join(WORKSHOP_CONTENT, i))]
        if r.returncode == 0 and not missing:
            return True
        tail = " | ".join(line.strip() for line in (r.stdout + r.stderr).splitlines() if re.search(r"ERROR|Error|fail|Timed out", line))[-400:]
        log(f"WARN steamcmd attempt {attempt + 1}/3 (exit {r.returncode}): {len(missing)} item(s) missing{': ' + tail if tail else ''}")
        time.sleep(5)
    return False


def workshop_times():
    """{item id: the time_updated of the files SteamCMD has}, from its appworkshop manifest."""
    acf = os.path.join(WORKSHOP, "steamapps", "workshop", f"appworkshop_{APP_ID}.acf")
    try:
        text = open(acf, encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    times = {}
    for m in re.finditer(r'"(\d+)"\s*\{[^{}]*?"timeupdated"\s+"(\d+)"', text):
        times.setdefault(m.group(1), int(m.group(2)))
    return times


# ------------------------------------------------------------------------------------------------
# Updates that could not be installed: remembered, so a failing download is not retried (with a
# restart and five minutes of warnings in chat each time) every hour
# ------------------------------------------------------------------------------------------------

def failed_updates(now=None):
    now = time.time() if now is None else now
    data = read_json(UPDATE_FAILED, {}) or {}
    return {k: v for k, v in data.items() if isinstance(v, (int, float)) and now - v < UPDATE_RETRY_SECONDS}


def mark_update_failed(target, now=None):
    data = failed_updates(now)
    data[target] = int(time.time() if now is None else now)
    try:
        write_json(UPDATE_FAILED, data, indent=None)
    except OSError:
        pass


def settle_wanted_updates():
    """After a start: every Workshop update the watcher restarted for that is still not on disk is
    remembered as failed (its tModLoader half is cmd_install's)."""
    wanted = read_json(UPDATE_WANTED, {}) or {}
    times = workshop_times()
    for target in wanted.get("targets", []):
        parts = str(target).split(":")
        if parts[0] == "mod" and len(parts) == 3 and times.get(parts[1], 0) < int(parts[2] or 0):
            mark_update_failed(target)
            log(f"WARN the update of Workshop item {parts[1]} did not download: not tried again for {UPDATE_RETRY_SECONDS // 3600} h")
    try:
        os.remove(UPDATE_WANTED)
    except OSError:
        pass


def load_mods_file():
    data = read_json(MODS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    return {"workshop": workshop_ids(",".join(map(str, data.get("workshop") or []))),
            "disabled": [str(n) for n in (data.get("disabled") or []) if str(n).strip()]}


def current_tml_version():
    have = installed_versions()
    return have[0].lstrip("v") if have else None


def cmd_sync_mods():
    sys.stdout.reconfigure(line_buffering=True)
    _sync_mods()


def _sync_mods():
    """Before every start: the mods asked for (TML_MODS and the panel) and their dependencies,
    downloaded or updated, then Mods/enabled.json: everything except what the panel switched off.
    Uploaded .tmod files in Mods/ are mods too."""
    os.makedirs(MODS_DIR, exist_ok=True)
    panel = load_mods_file()
    asked = workshop_ids(os.environ.get("TML_MODS", "")) + [i for i in panel["workshop"] if i not in workshop_ids(os.environ.get("TML_MODS", ""))]
    cache = read_json(DEPS_CACHE, {}) or {}
    order, required_by = resolve(asked, cache)
    try:
        write_json(DEPS_CACHE, cache, indent=None)
    except OSError:
        pass
    if order:
        log(f"Workshop: {len(asked)} mod(s) asked for, {len(order) - len(asked)} dependenc{'y' if len(order) - len(asked) == 1 else 'ies'}: downloading")
        if not download(order):
            log("WARN some Workshop items could not be downloaded; the server starts with what it has")
    series = tml_series(current_tml_version())
    mods = []
    for item_id in order:
        folder = os.path.join(WORKSHOP_CONTENT, item_id)
        tmod = best_tmod(folder, series) if os.path.isdir(folder) else None
        entry = {"id": item_id, "source": "workshop", "asked": item_id in asked, "requiredBy": required_by.get(item_id, [])}
        if not tmod:
            entry.update({"name": None, "error": "not downloaded, or no build for this tModLoader"})
        else:
            try:
                entry.update(read_tmod(tmod))
                entry["file"] = os.path.relpath(tmod, SERVER_DIR)
                entry["size"] = os.path.getsize(tmod)
            except (OSError, ValueError, struct.error, zlib.error) as e:
                entry.update({"name": None, "error": f"unreadable .tmod ({e})"})
        mods.append(entry)
    for fname in sorted(os.listdir(MODS_DIR)):
        if fname.endswith(".tmod"):
            path = os.path.join(MODS_DIR, fname)
            entry = {"id": None, "source": "upload", "asked": True, "requiredBy": [], "file": fname, "size": os.path.getsize(path)}
            try:
                entry.update(read_tmod(path))
            except (OSError, ValueError, struct.error, zlib.error) as e:
                entry.update({"name": fname[:-5], "error": f"unreadable .tmod ({e})"})
            mods.append(entry)
    disabled = set(panel["disabled"])
    # The same mod twice (a Workshop item and an uploaded .tmod of the same internal name):
    # tModLoader loads one of them, and which one is not documented. Both are flagged.
    by_name = {}
    for m in mods:
        if m.get("name"):
            by_name.setdefault(m["name"], []).append(m)
    for name, copies in by_name.items():
        if len(copies) > 1:
            for m in copies:
                m["duplicate"] = True
            log(f"WARN two copies of {name} ({', '.join(c['source'] for c in copies)}): tModLoader loads only one; remove the one you do not want")
    enabled = []
    for m in mods:
        m["enabled"] = bool(m.get("name")) and m["name"] not in disabled and not m.get("error")
        if m["enabled"] and m["name"] not in enabled:
            enabled.append(m["name"])
    names = set(enabled)
    for m in mods:
        missing = [r for r in m.get("modReferences", []) if r not in names]
        if m["enabled"] and missing:
            m["missing"] = missing
            log(f"WARN {m['name']} needs {', '.join(missing)}, which is off or not installed: tModLoader will disable it")
    write_json(os.path.join(MODS_DIR, "enabled.json"), enabled)
    write_json(MODS_STATE_FILE, {"mods": mods, "enabled": enabled, "at": int(time.time()), "tml": current_tml_version()})
    log(f"enabled.json: {', '.join(enabled) if enabled else '(no mods)'}")
    settle_wanted_updates()


# ------------------------------------------------------------------------------------------------
# serverconfig.txt
# ------------------------------------------------------------------------------------------------

def cmd_apply():
    os.makedirs(WORLDS_DIR, exist_ok=True)
    settings = load_settings()
    lines = []

    name, src = effective(settings, "TML_WORLD_NAME", "worldName", "Terraria on Flux")
    name = clean_text(name, 64) or "Terraria on Flux"
    world, wsrc = effective(settings, "TML_WORLD", "world", None)
    # A world that is there is loaded by its EXACT file name ("Bob's World.wld" too); only a world
    # that does not exist yet gets a cleaned name, which is the file the server then creates.
    stem = (existing_world(world) or world_stem(world)) if world else (existing_world(name) or world_stem(name))
    stem = stem or "World"
    world_file = os.path.join(WORLDS_DIR, stem + ".wld")
    new_world = not os.path.exists(world_file)
    lines += [f"world={world_file}", f"worldpath={WORLDS_DIR}", f"banlist={BANLIST}"]
    log(f"world: {stem}.wld ({'a new world' if new_world else 'exists'}; {wsrc if world else src})")
    if new_world:
        size, s1 = effective(settings, "TML_WORLD_SIZE", "worldSize", "medium")
        diff, s2 = effective(settings, "TML_DIFFICULTY", "difficulty", "classic")
        seed, s3 = effective(settings, "TML_SEED", "seed", "")
        size_n = SIZES.get(str(size).strip().lower(), 2)
        diff_n = DIFFICULTIES.get(str(diff).strip().lower(), 0)
        lines += [f"autocreate={size_n}", f"worldname={name if not world else stem}", f"difficulty={diff_n}"]
        if str(seed).strip():
            lines.append(f"seed={clean_text(seed, 64)}")
        log(f"new world: size {size} ({s1}), difficulty {diff} ({s2}){', seed ' + str(seed) if str(seed).strip() else ''}")

    players, src = effective(settings, "TML_MAX_PLAYERS", "maxPlayers", 8)
    try:
        players = min(max(int(str(players).strip()), 1), MAX_PLAYERS_CAP)
    except ValueError:
        log(f"WARN max players {players!r} is not a number; 8")
        players = 8
    lines.append(f"maxplayers={players}")

    port = str(os.environ.get("TML_PORT", "7777")).strip()
    if not re.fullmatch(r"\d{2,5}", port):
        port = "7777"
    lines.append(f"port={port}")

    password, src = effective(settings, "TML_PASSWORD", "password", "")
    password = clean_text(password, 64)
    if password:
        lines.append(f"password={password}")
    log(f"password: {'set' if password else 'none (anyone with the address can join)'} ({src})")

    motd, _ = effective(settings, "TML_MOTD", "motd", "")
    if str(motd).strip():
        lines.append(f"motd={clean_text(motd, 200)}")

    secure, _ = effective(settings, "TML_SECURE", "secure", "false")
    lines.append(f"secure={1 if truthy(secure) else 0}")
    lang, _ = effective(settings, "TML_LANGUAGE", "language", "en-US")
    lines.append(f"language={lang if lang in LANGUAGES else 'en-US'}")
    npc, _ = effective(settings, "TML_NPCSTREAM", "npcStream", "60")
    lines.append(f"npcstream={int(npc) if str(npc).isdigit() else 60}")
    lines += ["upnp=0", "priority=1"]

    perms = settings.get("journeyPermissions") or {}
    for power, level in perms.items():
        if re.fullmatch(r"[a-z_]{3,40}", power) and str(level) in ("0", "1", "2"):
            lines.append(f"journeypermission_{power}={level}")

    write_text(CONFIG_FILE, "# Written by runonflux/tmodloader-server-flux on every start. Edit in the panel.\n" + "\n".join(lines) + "\n")
    state = read_json(STATE_FILE, {}) or {}
    state["applied"] = int(time.time()) + 1
    state["world"] = stem
    # What the server starts with, for changes_pending: the uploaded .tmod files.
    state["tmods"] = tmod_snapshot()
    write_json(STATE_FILE, state, indent=None)
    try:
        os.remove(PENDING_MARK)
    except OSError:
        pass


# ------------------------------------------------------------------------------------------------
# Updates
# ------------------------------------------------------------------------------------------------

def cmd_updates():
    """What is out of date, as lines for the watcher, leaving out what failed to install less than
    FLUX_UPDATE_RETRY_HOURS ago; and the targets, for settle_wanted_updates after the restart."""
    wanted = os.environ.get("TML_VERSION", "stable") or "stable"
    have = installed_versions()
    failed = failed_updates()
    lines, targets = [], []
    if wanted.lower() in ("stable", "preview") and have:
        tag = latest_release_tag(wanted)
        if tag and parse_version(tag) > parse_version(have[0]) and f"tml:{tag}" not in failed:
            lines.append(f"tml {have[0]} {tag}")
            targets.append(f"tml:{tag}")
    state = read_json(MODS_STATE_FILE, {}) or {}
    ids = [m["id"] for m in state.get("mods", []) if m.get("id")]
    if ids:
        form = "&".join([f"itemcount={len(ids)}"] + [f"publishedfileids%5B{i}%5D={x}" for i, x in enumerate(ids)])
        try:
            details = json.loads(http_get(DETAILS_API, data=form.encode()))["response"]["publishedfiledetails"]
        except Exception:
            details = []
        times = workshop_times()
        for d in details:
            item = str(d.get("publishedfileid"))
            remote = int(d.get("time_updated") or 0)
            target = f"mod:{item}:{remote}"
            if item in times and remote > times[item] and target not in failed:
                lines.append(f"mod {item} {d.get('title', '')}")
                targets.append(target)
    if targets:
        try:
            write_json(UPDATE_WANTED, {"targets": targets, "at": int(time.time())}, indent=None)
        except OSError:
            pass
    for line in lines:
        print(line)


# ------------------------------------------------------------------------------------------------
# State, status, console
# ------------------------------------------------------------------------------------------------

def cmd_state(state, message=None):
    data = read_json(STATE_FILE, {}) or {}
    if data.get("state") != state:
        data["since"] = int(time.time())
    data.update({"state": state, "message": message or None, "image": os.environ.get("FLUX_IMAGE_VERSION", "dev")})
    # The supervisor's generation: the panel waits for a NEW one after a restart, never comparing
    # its own clock with the node's.
    if str(os.environ.get("FLUX_GENERATION", "")).isdigit():
        data["generation"] = int(os.environ["FLUX_GENERATION"])
    if state == "running":
        tml, terraria = game_versions(read_head(GAME_LOG, 200))
        if tml or terraria:
            data["versions"] = {"tml": tml, "terraria": terraria}
    write_json(STATE_FILE, data, indent=None)


def console(command, timeout=8):
    try:
        r = subprocess.run(["flux-console", command], capture_output=True, text=True, timeout=timeout + 4)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


PLAYER_LINE = re.compile(r"^(.+?) \((\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]+)(?::\d+)?\)\s*$")
SUMMARY_LINE = re.compile(r"^(?:No players connected\.|\d+ players? connected\.)$")


def parse_playing(text):
    """`playing` as flux-console prints it (the console's ": " taken off): one line per player,
    "Name (IP:port)", then the summary ("No players connected." / "N player(s) connected.").

    Only the lines BEFORE the summary are players, and only when there is a summary: anything else
    the server printed in that second (chat "<Bob> brb (10.0.0.1)", a join) is not the answer. A
    name may hold anything, "Disconnected Dan" included."""
    lines = [re.sub(r"^:\s?", "", line.strip()).strip() for line in (text or "").splitlines()]
    end = next((i for i, line in enumerate(lines) if SUMMARY_LINE.match(line)), None)
    if end is None:
        return []
    players = []
    for line in lines[:end]:
        if not line or line.startswith("<") or line.lower() == "playing":
            continue
        m = PLAYER_LINE.match(line)
        if m:
            players.append({"name": m.group(1).strip(), "ip": m.group(2)})
    return players


def tmod_snapshot():
    """The uploaded .tmod files as "name:mtime": a file added, removed or replaced changes it."""
    if not os.path.isdir(MODS_DIR):
        return []
    out_ = []
    for fname in sorted(os.listdir(MODS_DIR)):
        if fname.endswith(".tmod"):
            try:
                out_.append(f"{fname}:{int(os.path.getmtime(os.path.join(MODS_DIR, fname)))}")
            except OSError:
                pass
    return out_


def changes_pending(state):
    """The panel saved something the running server has not loaded: settings, the mod list, an
    uploaded or removed .tmod, a mod config (set-modconfig marks it; tModLoader writes its own
    configs while it runs, so the folder's times cannot tell)."""
    applied = state.get("applied") or 0
    for path in (SETTINGS_FILE, MODS_FILE):
        try:
            if os.path.getmtime(path) > applied:
                return True
        except OSError:
            pass
    if os.path.exists(PENDING_MARK) or os.path.exists(RESTORE_REQUEST):
        return True
    return "tmods" in state and tmod_snapshot() != state.get("tmods")


def game_versions(lines):
    """(tModLoader, Terraria) from the start banner: "Terraria Server v1.4.4.9 - tModLoader v2026.8.3.0"."""
    tml = terraria = None
    for line in lines:
        m = re.search(r"tModLoader v(\d{4}\.\d+\.\d+\.\d+)", line)
        if m:
            tml = m.group(1)
        m = re.search(r"Terraria Server v(\d+\.\d+\.\d+(?:\.\d+)?)", line)
        if m:
            terraria = m.group(1)
    return tml, terraria


def cmd_status():
    state = read_json(STATE_FILE, {}) or {}
    running = state.get("state") == "running"
    players = parse_playing(console("playing")) if running else []
    version, terraria = game_versions(read_head(GAME_LOG, 200))
    known = state.get("versions") or {}
    version, terraria = version or known.get("tml"), terraria or known.get("terraria")
    mods = read_json(MODS_STATE_FILE, {}) or {}
    settings = load_settings()
    players_max, _ = effective(settings, "TML_MAX_PLAYERS", "maxPlayers", 8)
    out({
        "state": state.get("state", "stopped"),
        "message": state.get("message"),
        "since": state.get("since"),
        "generation": state.get("generation"),
        "image": state.get("image"),
        "version": version or current_tml_version(),
        "terraria": terraria,
        "world": state.get("world"),
        "players": players,
        "max_players": int(players_max) if str(players_max).isdigit() else None,
        "mods": [m["name"] for m in mods.get("mods", []) if m.get("enabled")],
        "pending": changes_pending(state),
    })


def read_head(path, n, limit=256 * 1024):
    """The first n lines, reading at most `limit` bytes: the start banner, whatever the log grew to."""
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace").splitlines()[:n]
    except OSError:
        return []


def read_tail(path, n, limit=512 * 1024):
    """The last n lines, reading at most the last `limit` bytes (never a whole log)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


# ------------------------------------------------------------------------------------------------
# Panel: settings
# ------------------------------------------------------------------------------------------------

SETTING_KEYS = {
    "worldName": "TML_WORLD_NAME", "world": "TML_WORLD", "worldSize": "TML_WORLD_SIZE", "difficulty": "TML_DIFFICULTY",
    "seed": "TML_SEED", "maxPlayers": "TML_MAX_PLAYERS", "password": "TML_PASSWORD", "motd": "TML_MOTD",
    "secure": "TML_SECURE", "language": "TML_LANGUAGE", "npcStream": "TML_NPCSTREAM", "journeyPermissions": None,
}


def cmd_settings():
    env = {v: os.environ[v] for v in SETTING_KEYS.values() if v and v in os.environ}
    for secret in ("TML_PASSWORD",):
        if secret in env:
            env[secret] = "(set)" if env[secret] else ""
    for k in ("TML_MODS", "TML_VERSION", "TML_PORT", "TML_RESTART_TIME", "TML_AUTOSAVE_MINUTES"):
        if k in os.environ:
            env[k] = os.environ[k]
    out({"settings": load_settings(), "env": env, "envKeys": SETTING_KEYS,
         "choices": {"worldSize": list(SIZES), "difficulty": list(DIFFICULTIES), "language": LANGUAGES,
                     "maxPlayersCap": MAX_PLAYERS_CAP}})


def cmd_set_settings(raw):
    try:
        incoming = json.loads(raw)
    except ValueError:
        fail("not JSON")
    if not isinstance(incoming, dict):
        fail("expected a JSON object")
    settings = load_settings()
    errors = []
    for key, value in incoming.items():
        if key not in SETTING_KEYS:
            errors.append(f"unknown setting {key}")
            continue
        if value is None:
            settings.pop(key, None)
            continue
        if key == "world":
            # A world that is there, by its exact name; or a NEW one, whose name must already be
            # the file name the server creates (letters, digits, spaces, _ and -).
            exact = existing_world(value)
            if exact:
                value = exact
            elif str(value).strip() and world_stem(value) == str(value).strip():
                value = str(value).strip()
            else:
                errors.append("world must be the exact name of a world in Worlds/, or a new name of letters, digits, spaces, - and _")
                continue
        elif key == "worldSize" and str(value).lower() not in SIZES:
            errors.append("worldSize must be small, medium or large")
            continue
        elif key == "difficulty" and str(value).lower() not in DIFFICULTIES:
            errors.append("difficulty must be classic, expert, master or journey")
            continue
        elif key == "maxPlayers":
            if not str(value).isdigit() or not 1 <= int(value) <= MAX_PLAYERS_CAP:
                errors.append(f"maxPlayers must be 1 to {MAX_PLAYERS_CAP}")
                continue
            value = int(value)
        elif key == "language" and value not in LANGUAGES:
            errors.append("unknown language")
            continue
        elif key == "journeyPermissions" and not isinstance(value, dict):
            errors.append("journeyPermissions must be an object")
            continue
        elif key in ("worldName", "seed", "password", "motd"):
            value = clean_text(value, 200 if key == "motd" else 64)
        elif key == "secure":
            value = truthy(value)
        settings[key] = value.lower() if key in ("worldSize", "difficulty") else value
    if errors:
        fail("; ".join(errors), 2)
    write_json(SETTINGS_FILE, settings)
    out({"ok": True, "settings": settings, "appliesOn": "restart"})


# ------------------------------------------------------------------------------------------------
# Panel: worlds
# ------------------------------------------------------------------------------------------------

def cmd_worlds():
    state = read_json(STATE_FILE, {}) or {}
    active = state.get("world")
    worlds = []
    if os.path.isdir(WORLDS_DIR):
        for fname in sorted(os.listdir(WORLDS_DIR), key=str.lower):
            if not fname.endswith(".wld"):
                continue
            stem = fname[:-4]
            path = os.path.join(WORLDS_DIR, fname)
            st = os.stat(path)
            twld = os.path.join(WORLDS_DIR, stem + ".twld")
            worlds.append({"name": stem, "size": st.st_size, "modified": int(st.st_mtime),
                           "modded": os.path.exists(twld), "active": stem == active,
                           "backups": sorted(x for x in os.listdir(WORLDS_DIR) if x.startswith(stem + ".") and ".bak" in x)})
    out({"active": active, "next": load_settings().get("world"), "worlds": worlds, "path": "Worlds"})


def cmd_delete_world(name):
    """Deletes ONE world, matched exactly as `worlds` lists it: its .wld and .twld and the game's
    own copies of exactly those files (".wld.bak", ".twld.bak", ".wld.bak2"...). Never the active
    world, and never by a cleaned-up name (which could be another world's)."""
    stem = existing_world(name)
    state = read_json(STATE_FILE, {}) or {}
    if not stem:
        fail("no such world", 2)
    if stem == state.get("world"):
        fail("that is the active world: switch to another one first", 2)
    removed = []
    for fname in os.listdir(WORLDS_DIR):
        for ext in (".wld", ".twld"):
            if fname == stem + ext or (fname.startswith(stem + ext + ".") and re.fullmatch(r"bak\d*", fname[len(stem + ext) + 1:])):
                os.remove(os.path.join(WORLDS_DIR, fname))
                removed.append(fname)
    out({"ok": True, "removed": sorted(removed)})


# ------------------------------------------------------------------------------------------------
# Panel: mods and their configs
# ------------------------------------------------------------------------------------------------

def cmd_mods():
    state = read_json(MODS_STATE_FILE, {}) or {}
    out({"mods": state.get("mods", []), "enabled": state.get("enabled", []), "at": state.get("at"),
         "tml": state.get("tml"), "panel": load_mods_file(),
         "env": workshop_ids(os.environ.get("TML_MODS", "")), "pending": changes_pending(read_json(STATE_FILE, {}) or {})})


def cmd_set_mods(raw):
    try:
        incoming = json.loads(raw)
    except ValueError:
        fail("not JSON")
    if not isinstance(incoming, dict):
        fail("expected a JSON object")
    current = load_mods_file()
    if "workshop" in incoming:
        current["workshop"] = workshop_ids(",".join(map(str, incoming["workshop"] or [])))
    if "disabled" in incoming:
        current["disabled"] = [clean_text(n, 80) for n in incoming["disabled"] or [] if re.fullmatch(r"[\w.\-]{1,80}", str(n))]
    write_json(MODS_FILE, current)
    out({"ok": True, **current, "appliesOn": "restart"})


def cmd_mod_search_deps(item_id):
    if not re.fullmatch(r"\d{1,20}", item_id):
        fail("not a Workshop ID", 2)
    cache = read_json(DEPS_CACHE, {}) or {}
    order, required_by = resolve([item_id], cache)
    try:
        write_json(DEPS_CACHE, cache, indent=None)
    except OSError:
        pass
    out({"id": item_id, "needs": [i for i in order if i != item_id], "requiredBy": required_by})


def cmd_modconfigs():
    configs = []
    if os.path.isdir(MODCONFIGS_DIR):
        for fname in sorted(os.listdir(MODCONFIGS_DIR)):
            if fname.endswith(".json"):
                data = read_json(os.path.join(MODCONFIGS_DIR, fname))
                configs.append({"file": fname, "mod": fname.split("_")[0], "config": data})
    out({"configs": configs, "path": "ModConfigs"})


def cmd_set_modconfig(fname, raw):
    if not re.fullmatch(r"[\w.\-]{1,120}\.json", fname):
        fail("not a config file name", 2)
    try:
        data = json.loads(raw)
    except ValueError:
        fail("not JSON", 2)
    write_json(os.path.join(MODCONFIGS_DIR, fname), data)
    write_text(PENDING_MARK, f"{fname}\n")
    out({"ok": True, "file": fname, "appliesOn": "restart"})


def cmd_mark_pending(what):
    """The panel changed something on the volume itself (an uploaded or removed file): the next
    status says a restart applies it."""
    write_text(PENDING_MARK, f"{clean_text(what, 120)}\n")
    out({"ok": True})


# ------------------------------------------------------------------------------------------------
# Panel: bans
# ------------------------------------------------------------------------------------------------

def read_bans():
    """banlist.txt: "//Name" then the IP, a pair per ban (Terraria's server)."""
    bans, name = [], None
    for line in read_tail(BANLIST, 100000):
        line = line.strip()
        if line.startswith("//"):
            name = line[2:].strip()
        elif line:
            bans.append({"name": name, "ip": line})
            name = None
    return bans


def cmd_bans():
    out({"bans": read_bans()})


def cmd_unban(who):
    bans = read_bans()
    keep = [b for b in bans if who not in (b["name"], b["ip"])]
    if len(keep) == len(bans):
        fail("not on the ban list", 2)
    write_text(BANLIST, "".join(f"//{b['name'] or ''}\n{b['ip']}\n" for b in keep))
    out({"ok": True, "lifted": len(bans) - len(keep), "appliesOn": "restart"})


# ------------------------------------------------------------------------------------------------
# Backups and restore
# ------------------------------------------------------------------------------------------------

BACKUP_NAME = re.compile(r"^\d{8}-\d{6}" + re.escape(BACKUP_SUFFIX) + r"$")


def backup_list():
    """[(name, epoch)] newest first. The epoch is the name's UTC time (the file's own time can be
    changed by a copy)."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    found = []
    for n in os.listdir(BACKUP_DIR):
        if BACKUP_NAME.match(n):
            try:
                found.append((n, calendar.timegm(time.strptime(n[:15], "%Y%m%d-%H%M%S"))))
            except ValueError:
                pass
    return sorted(found, key=lambda x: x[0], reverse=True)


def backups_to_keep(names_times, keep, now):
    """The newest `keep`, AND the newest one older than a day and the newest one older than a
    week: a server that crash-loops (one backup per start) can never rotate every good backup
    away. names_times newest first."""
    kept = {n for n, _ in names_times[:max(keep, 1)]}
    for age in (86400, 7 * 86400):
        older = [n for n, t in names_times if now - t >= age]
        if older:
            kept.add(older[0])
    return kept


def cmd_backup(why, now=None):
    """A .tar.gz of BACKUP_ITEMS. A "start" backup is skipped when the newest one is younger than
    FLUX_BACKUP_MIN_AGE_MINUTES (every restart, crash restart and update would otherwise rotate the
    history away within the hour). Written beside, then renamed."""
    import tarfile
    now = time.time() if now is None else now
    keep = int(os.environ.get("FLUX_BACKUP_KEEP", "10") or 10)
    if keep <= 0:
        return
    if not any(f.endswith(".wld") for f in (os.listdir(WORLDS_DIR) if os.path.isdir(WORLDS_DIR) else [])):
        return
    existing = backup_list()
    min_age = int(os.environ.get("FLUX_BACKUP_MIN_AGE_MINUTES", "60") or 60) * 60
    if why == "start" and existing and now - existing[0][1] < min_age:
        log(f"backup (start): skipped, the newest is {int((now - existing[0][1]) // 60)} min old")
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)) + BACKUP_SUFFIX
    path = os.path.join(BACKUP_DIR, name)
    part = path + ".part"
    try:
        with tarfile.open(part, "w:gz") as tar:
            for item in BACKUP_ITEMS:
                src = os.path.join(DATA_DIR, item)
                if os.path.exists(src):
                    # A file the server rewrites while it is read is archived as it was read: the
                    # world itself was saved just before (`save` / `exit`).
                    # tModLoader's own dated zips (Worlds/Backups) are left out: they only grow.
                    tar.add(src, arcname=item, filter=lambda ti: None if ti.name == "Worlds/Backups" or ti.name.startswith("Worlds/Backups/") else ti)
        os.replace(part, path)
    except (OSError, tarfile.TarError) as e:
        try:
            os.remove(part)
        except OSError:
            pass
        log(f"WARN backup ({why}) failed: {e}")
        return
    index = read_json(BACKUP_INDEX, {}) or {}
    index[name] = {"why": why, "at": int(now)}
    names_times = backup_list()
    kept = backups_to_keep(names_times, keep, now)
    for n, _ in names_times:
        if n not in kept:
            try:
                os.remove(os.path.join(BACKUP_DIR, n))
            except OSError:
                pass
            index.pop(n, None)
    write_json(BACKUP_INDEX, {k: v for k, v in index.items() if os.path.exists(os.path.join(BACKUP_DIR, k))}, indent=None)
    log(f"backup ({why}): flux/backups/{name}, {os.path.getsize(path) // 1024} KB")


def cmd_backups():
    index = read_json(BACKUP_INDEX, {}) or {}
    rows = []
    for n, t in backup_list():
        try:
            size = os.path.getsize(os.path.join(BACKUP_DIR, n))
        except OSError:
            continue
        rows.append({"name": n, "size": size, "at": t, "why": (index.get(n) or {}).get("why")})
    out({"backups": rows, "path": "flux/backups", "restore": read_json(RESTORE_REQUEST)})


def cmd_restore(name):
    """Asks for a restore: the supervisor stops the server (saving it), takes a "pre-restore"
    backup of what is there now, puts the backup's files in place and starts again. Persistent, so a
    server that is not running gets it on its next start."""
    if not BACKUP_NAME.match(str(name)) or not os.path.isfile(os.path.join(BACKUP_DIR, name)):
        fail("no such backup", 2)
    write_json(RESTORE_REQUEST, {"name": name, "at": int(time.time())}, indent=None)
    if not server_pids():
        out({"ok": True, "appliesOn": "start"})
        return
    request_restart(f"requested: restore {name}")
    out({"ok": True, "appliesOn": "now"})


def cmd_do_restore():
    """The supervisor, between two generations: the restore the panel asked for, if any."""
    import tarfile
    req = read_json(RESTORE_REQUEST)
    if not isinstance(req, dict):
        return
    name = str(req.get("name", ""))
    archive = os.path.join(BACKUP_DIR, name)
    try:
        os.remove(RESTORE_REQUEST)
    except OSError:
        pass
    if not BACKUP_NAME.match(name) or not os.path.isfile(archive):
        log(f"WARN restore: no backup named {name!r}")
        return
    cmd_backup("pre-restore")
    staging = os.path.join(DATA_DIR, ".flux-restore")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = []
            for m in tar.getmembers():
                top = m.name.split("/")[0]
                if m.name.startswith("/") or ".." in m.name.split("/") or not (m.isfile() or m.isdir()):
                    continue
                if top in ("Worlds", "ModConfigs", "Mods", "banlist.txt") or m.name in ("flux", "flux/settings.json", "flux/mods.json"):
                    members.append(m)
            tar.extractall(staging, members=members)
        restored = []
        for item in BACKUP_ITEMS:
            src = os.path.join(staging, item)
            if not os.path.exists(src):
                continue
            dst = os.path.join(DATA_DIR, item)
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            elif os.path.exists(dst):
                os.remove(dst)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
            restored.append(item)
        log(f"restore: {name} put back ({', '.join(restored)}); what was there is in a pre-restore backup")
    except (OSError, tarfile.TarError) as e:
        log(f"WARN restore of {name} failed: {e}; nothing was replaced past that point")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def server_pids():
    return subprocess.run(["pgrep", "-f", r"^dotnet tModLoader\.dll "], capture_output=True, text=True).stdout.split()


def request_restart(why):
    with open(os.environ.get("FLUX_RESTART_MARKER", "/tmp/flux-restart-requested"), "w") as f:
        f.write(why + "\n")
    console("exit", timeout=2)


def cmd_restart():
    if not server_pids():
        fail("the server is not running", 3)
    request_restart("requested: from the panel")
    out({"ok": True})


def main(argv):
    commands = {
        "install": (cmd_install, 0), "sync-mods": (cmd_sync_mods, 0), "apply": (cmd_apply, 0),
        "state": (cmd_state, (1, 2)), "updates": (cmd_updates, 0), "status": (cmd_status, 0),
        "settings": (cmd_settings, 0), "set-settings": (cmd_set_settings, 1), "worlds": (cmd_worlds, 0),
        "delete-world": (cmd_delete_world, 1), "mods": (cmd_mods, 0), "set-mods": (cmd_set_mods, 1),
        "mod-search-deps": (cmd_mod_search_deps, 1), "modconfigs": (cmd_modconfigs, 0),
        "set-modconfig": (cmd_set_modconfig, 2), "mark-pending": (cmd_mark_pending, 1),
        "bans": (cmd_bans, 0), "unban": (cmd_unban, 1), "restart": (cmd_restart, 0),
        "backup": (cmd_backup, 1), "backups": (cmd_backups, 0), "restore": (cmd_restore, 1),
        "do-restore": (cmd_do_restore, 0),
    }
    if not argv or argv[0] not in commands:
        print(__doc__, file=sys.stderr)
        return 64
    fn, arity = commands[argv[0]]
    if len(argv) - 1 not in (arity if isinstance(arity, tuple) else (arity,)):
        print(__doc__, file=sys.stderr)
        return 64
    fn(*argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
