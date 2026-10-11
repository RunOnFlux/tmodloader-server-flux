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

Settings, mods and configs changed by the panel are applied on the NEXT start. Precedence: an
environment variable that is SET is applied on every start; one that is not set leaves the panel's
value, and then the default. Two exceptions: the size, difficulty and seed of a NEW world, where the
panel's choice wins (the environment is the default); and an EMPTY MOTD, password or seed variable,
which counts as unset.
"""

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
GAME_LOG = os.path.join(STATE_DIR, "current.log")
WORLDS_DIR = os.path.join(DATA_DIR, "Worlds")
MODS_DIR = os.path.join(DATA_DIR, "Mods")
MODCONFIGS_DIR = os.path.join(DATA_DIR, "ModConfigs")
BANLIST = os.path.join(DATA_DIR, "banlist.txt")
INSTALLS = os.path.join(SERVER_DIR, "tml")
WORKSHOP = os.path.join(SERVER_DIR, "workshop")
WORKSHOP_CONTENT = os.path.join(WORKSHOP, "steamapps", "workshop", "content", "1281930")
APP_ID = "1281930"
RELEASES_API = os.environ.get("FLUX_RELEASES_API", "https://api.github.com/repos/tModLoader/tModLoader/releases?per_page=30")
RELEASE_ZIP = "https://github.com/tModLoader/tModLoader/releases/download/{tag}/tModLoader.zip"
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
    """A world's file name without .wld: what Terraria itself makes of a world name (letters,
    digits, spaces, _ and -), never a path."""
    value = os.path.basename(str(value).strip())
    value = re.sub(r"\.t?wld$", "", value, flags=re.I)
    value = re.sub(r"[^\w \-]", "", value).strip()
    return value[:64]


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
# Keys whose empty value means "none": an empty variable (the marketplace writes the declared ones
# even when left blank) leaves the panel's value instead of erasing it.
EMPTY_IS_UNSET = ("motd", "password", "seed")


def effective(settings, env_name, key, default=None):
    if key in NEW_WORLD_KEYS and key in settings:
        return settings[key], "panel"
    if env_name in os.environ and not (key in EMPTY_IS_UNSET and not str(os.environ[env_name]).strip()):
        return os.environ[env_name], "env"
    if key in settings:
        return settings[key], "panel"
    return default, "default"


# ------------------------------------------------------------------------------------------------
# tModLoader builds (GitHub releases)
# ------------------------------------------------------------------------------------------------

def http_get(url, timeout=30, data=None, tries=3):
    """GET (or POST with data). Steam's community pages answer 429 to bursts, for a while: those
    are retried, 5 then 15 seconds later."""
    import urllib.error
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "runonflux/tmodloader-server-flux", "Accept": "*/*"})
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


def latest_release_tag(wanted):
    try:
        return pick_release(json.loads(http_get(RELEASES_API)), wanted)
    except Exception:
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
            if have:
                log(f"WARN could not install {tag} ({e}): starting the installed {have[0]}")
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
        log(f"WARN steamcmd attempt {attempt + 1}/3: {len(missing)} item(s) missing")
        time.sleep(5)
    return False


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
    stem = world_stem(world) if world else world_stem(name)
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
    write_json(STATE_FILE, state, indent=None)


# ------------------------------------------------------------------------------------------------
# Updates
# ------------------------------------------------------------------------------------------------

def cmd_updates():
    wanted = os.environ.get("TML_VERSION", "stable") or "stable"
    have = installed_versions()
    if wanted.lower() in ("stable", "preview") and have:
        tag = latest_release_tag(wanted)
        if tag and parse_version(tag) > parse_version(have[0]):
            print(f"tml {have[0]} {tag}")
    state = read_json(MODS_STATE_FILE, {}) or {}
    ids = [m["id"] for m in state.get("mods", []) if m.get("id")]
    if not ids:
        return
    form = "&".join([f"itemcount={len(ids)}"] + [f"publishedfileids%5B{i}%5D={x}" for i, x in enumerate(ids)])
    try:
        details = json.loads(http_get(DETAILS_API, data=form.encode()))["response"]["publishedfiledetails"]
    except Exception:
        return
    acf = os.path.join(WORKSHOP, "steamapps", "workshop", f"appworkshop_{APP_ID}.acf")
    try:
        text = open(acf, encoding="utf-8", errors="replace").read()
    except OSError:
        return
    for d in details:
        m = re.search(r'"%s"\s*\{[^}]*?"timeupdated"\s+"(\d+)"' % re.escape(str(d.get("publishedfileid"))), text)
        if m and int(d.get("time_updated") or 0) > int(m.group(1)):
            print(f"mod {d['publishedfileid']} {d.get('title', '')}")


# ------------------------------------------------------------------------------------------------
# State, status, console
# ------------------------------------------------------------------------------------------------

def cmd_state(state, message=None):
    data = read_json(STATE_FILE, {}) or {}
    if data.get("state") != state:
        data["since"] = int(time.time())
    data.update({"state": state, "message": message or None, "image": os.environ.get("FLUX_IMAGE_VERSION", "dev")})
    write_json(STATE_FILE, data, indent=None)


def console(command, timeout=8):
    try:
        r = subprocess.run(["flux-console", command], capture_output=True, text=True, timeout=timeout + 4)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


PLAYER_LINE = re.compile(r"^:?\s*(.+?) \((\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]+)(?::\d+)?\)\s*$")


def parse_playing(text):
    """`playing`: ": No players connected." or one line per player, "Name (IP:port)", then
    ": N player(s) connected." (Terraria's server)."""
    players = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "connected" in line.lower():
            continue
        m = PLAYER_LINE.match(line)
        if m:
            players.append({"name": m.group(1).lstrip(": ").strip(), "ip": m.group(2)})
    return players


def changes_pending(state):
    for path in (SETTINGS_FILE, MODS_FILE):
        try:
            if os.path.getmtime(path) > (state.get("applied") or 0):
                return True
        except OSError:
            pass
    return False


def cmd_status():
    state = read_json(STATE_FILE, {}) or {}
    running = state.get("state") == "running"
    players = parse_playing(console("playing")) if running else []
    version = None
    for line in read_tail(GAME_LOG, 400):
        m = re.search(r"tModLoader v(\d{4}\.\d+\.\d+\.\d+)", line)
        if m:
            version = m.group(1)
    mods = read_json(MODS_STATE_FILE, {}) or {}
    settings = load_settings()
    players_max, _ = effective(settings, "TML_MAX_PLAYERS", "maxPlayers", 8)
    out({
        "state": state.get("state", "stopped"),
        "message": state.get("message"),
        "since": state.get("since"),
        "image": state.get("image"),
        "version": version or current_tml_version(),
        "terraria": "1.4.4.9",
        "world": state.get("world"),
        "players": players,
        "max_players": int(players_max) if str(players_max).isdigit() else None,
        "mods": [m["name"] for m in mods.get("mods", []) if m.get("enabled")],
        "pending": changes_pending(state),
    })


def read_tail(path, n):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
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
            value = world_stem(value)
            if not value:
                errors.append("world must be a world's name")
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
    stem = world_stem(name)
    state = read_json(STATE_FILE, {}) or {}
    if not stem or stem == state.get("world"):
        fail("that is the active world: switch to another one first", 2)
    removed = []
    for fname in os.listdir(WORLDS_DIR) if os.path.isdir(WORLDS_DIR) else []:
        if fname == stem + ".wld" or fname == stem + ".twld" or fname.startswith(stem + ".wld.") or fname.startswith(stem + ".twld."):
            os.remove(os.path.join(WORLDS_DIR, fname))
            removed.append(fname)
    if not removed:
        fail("no such world", 2)
    out({"ok": True, "removed": removed})


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
    out({"ok": True, "file": fname, "appliesOn": "restart"})


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


def cmd_restart():
    pids = subprocess.run(["pgrep", "-f", r"^dotnet tModLoader\.dll "], capture_output=True, text=True).stdout.split()
    if not pids:
        fail("the server is not running", 3)
    with open(os.environ.get("FLUX_RESTART_MARKER", "/tmp/flux-restart-requested"), "w") as f:
        f.write("requested: from the panel\n")
    console("exit", timeout=2)
    out({"ok": True})


def main(argv):
    commands = {
        "install": (cmd_install, 0), "sync-mods": (cmd_sync_mods, 0), "apply": (cmd_apply, 0),
        "state": (cmd_state, (1, 2)), "updates": (cmd_updates, 0), "status": (cmd_status, 0),
        "settings": (cmd_settings, 0), "set-settings": (cmd_set_settings, 1), "worlds": (cmd_worlds, 0),
        "delete-world": (cmd_delete_world, 1), "mods": (cmd_mods, 0), "set-mods": (cmd_set_mods, 1),
        "mod-search-deps": (cmd_mod_search_deps, 1), "modconfigs": (cmd_modconfigs, 0),
        "set-modconfig": (cmd_set_modconfig, 2), "bans": (cmd_bans, 0), "unban": (cmd_unban, 1),
        "restart": (cmd_restart, 0),
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
