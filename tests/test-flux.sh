#!/bin/bash
# Unit tests: the pure decisions in flux-lib.sh and fluxtml.py's file handling. No Docker, no
# Steam, no network, no game. Run from the repository root.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# shellcheck source=scripts/flux-lib.sh
FLUX_LOG="" source scripts/flux-lib.sh

pass=0
fail=0
check() { # description, expected, actual
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL %s\n  expected: [%s]\n  actual:   [%s]\n' "$1" "$2" "$3"
  fi
}
status() { "$@" >/dev/null 2>&1; echo $?; }
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# --- Inputs ------------------------------------------------------------------------------------
check "7777 is a port" "7777" "$(flux_valid_port 7777)"
check "unset is 7777" "7777" "$(flux_valid_port '')"
check "port 80 is refused" "1" "$(status flux_valid_port 80)"
check "a word is not a port" "1" "$(status flux_valid_port abc)"
check "autosave default" "10" "$(flux_minutes '' 10)"
check "autosave off" "0" "$(flux_minutes 0 10)"
check "autosave capped" "120" "$(flux_minutes 999 10)"
check "autosave words fall back" "10" "$(flux_minutes soon 10)"
check "restart time" "05:30" "$(flux_restart_time 5:30)"
check "restart time off" "" "$(flux_restart_time '')"
check "restart time 25:00 refused" "1" "$(status flux_restart_time 25:00)"
check "next daily after noon UTC is tomorrow" "$((1791720000 + 61200))" "$(TZ=UTC flux_next_daily 1791720000 05:00)"
check "a crash counts" "0" "$(status flux_restart_counts 'the server ended on its own (exit 1)')"
check "an update does not count" "1" "$(status flux_restart_counts 'planned: update (tml a b)')"
check "console lines lose control characters" "say hi there" "$(flux_console_line $'say hi\nthere\x03')"
check "console answers lose the prompt" "No players connected." "$(printf ': No players connected.\r\n' | flux_console_answer)"

# --- fluxtml.py --------------------------------------------------------------------------------
data="${tmp}/data"
tml() {
  local vars=()
  while [[ "${1:-}" == *=* ]]; do vars+=("$1"); shift; done
  env -i PATH="${PATH}" FLUX_DATA_DIR="${data}" FLUX_SERVER_DIR="${tmp}/server" "${vars[@]}" python3 scripts/fluxtml.py "$@"
}
py() { PYTHONPATH=scripts python3 -c "import fluxtml as t; $1"; }
cfgval() { sed -n "s/^$1=//p" "${data}/flux/serverconfig.txt"; }

tml TML_WORLD_NAME="My World!" TML_WORLD_SIZE=large TML_DIFFICULTY=journey TML_SEED="not the bees" TML_MAX_PLAYERS=16 apply >/dev/null
check "the world file is the name, cleaned" "${data}/Worlds/My World.wld" "$(cfgval world)"
check "a new world is autocreated large" "3" "$(cfgval autocreate)"
check "journey is difficulty 3" "3" "$(cfgval difficulty)"
check "the seed is passed as text" "not the bees" "$(cfgval seed)"
check "max players" "16" "$(cfgval maxplayers)"
check "the ban list is on the data volume" "${data}/banlist.txt" "$(cfgval banlist)"
check "no password line without one" "" "$(cfgval password)"
check "upnp is off" "0" "$(cfgval upnp)"
mkdir -p "${data}/Worlds" && echo x >"${data}/Worlds/My World.wld"
tml TML_WORLD_NAME="My World" apply >/dev/null
check "an existing world is not autocreated" "" "$(cfgval autocreate)"
tml TML_MAX_PLAYERS=999 apply >/dev/null
check "players capped at 255" "255" "$(cfgval maxplayers)"
tml TML_MAX_PLAYERS=lots apply >/dev/null
check "a bad player count is 8" "8" "$(cfgval maxplayers)"

check "a world name with a path is refused" "2" "$(status tml set-settings '{"world": "../Other World.wld"}')"
check "the panel takes a new world" "0" "$(status tml set-settings '{"world": "Other World", "password": "pw", "motd": "hi"}')"
tml apply >/dev/null
check "the panel's new world" "${data}/Worlds/Other World.wld" "$(cfgval world)"
check "the panel's password" "pw" "$(cfgval password)"
check "the motd" "hi" "$(cfgval motd)"
tml TML_PASSWORD="" apply >/dev/null
check "an EMPTY env password leaves the panel's (empty counts as unset)" "pw" "$(cfgval password)"
tml TML_PASSWORD="envpw" apply >/dev/null
check "a set env password wins over the panel" "envpw" "$(cfgval password)"
check "an empty env MOTD leaves the panel's" "hi" "$(tml TML_MOTD="" apply >/dev/null; cfgval motd)"
rm -f "${data}/Worlds/Other World.wld"
tml set-settings '{"worldSize": "small", "difficulty": "master"}' >/dev/null
tml TML_WORLD_SIZE=large TML_DIFFICULTY=classic apply >/dev/null
check "a new world from the panel keeps its own size and difficulty" "1 2" "$(cfgval autocreate) $(cfgval difficulty)"
tml set-settings '{"worldSize": null, "difficulty": null}' >/dev/null
check "a bad size is refused" "2" "$(status tml set-settings '{"worldSize": "huge"}')"
check "a bad difficulty is refused" "2" "$(status tml set-settings '{"difficulty": "nightmare"}')"
check "an unknown setting is refused" "2" "$(status tml set-settings '{"pvp": 1}')"
check "settings hide the env password" "(set)" "$(env TML_PASSWORD=secret FLUX_DATA_DIR="${data}" python3 scripts/fluxtml.py settings | python3 -c 'import json,sys;print(json.load(sys.stdin)["env"]["TML_PASSWORD"])')"
tml set-settings '{"journeyPermissions": {"time_setfrozen": 0, "../x": 2}}' >/dev/null
tml apply >/dev/null
check "journey permissions are written" "0" "$(cfgval journeypermission_time_setfrozen)"
check "a strange power name is not" "0" "$(grep -c 'journeypermission_\.\./x' "${data}/flux/serverconfig.txt")"

# Releases.
check "stable skips pre-releases" "v2026.08.3.0" "$(py 'print(t.pick_release([{"tag_name":"v2026.09.2.1","prerelease":True},{"tag_name":"v2026.08.3.0","prerelease":False},{"tag_name":"v2026.07.1.0","prerelease":False}], "stable"))')"
check "preview takes the newest" "v2026.09.2.1" "$(py 'print(t.pick_release([{"tag_name":"v2026.09.2.1","prerelease":True},{"tag_name":"v2026.08.3.0","prerelease":False}], "preview"))')"
check "an exact version" "v2026.07.1.0" "$(py 'print(t.pick_release([{"tag_name":"v2026.08.3.0","prerelease":False},{"tag_name":"v2026.07.1.0","prerelease":False}], "2026.07.1.0"))')"
check "an unknown version is None" "None" "$(py 'print(t.pick_release([{"tag_name":"v2026.08.3.0","prerelease":False}], "v2020.01.1.1"))')"

# Workshop IDs.
check "ids from links and spaces" "['2824688072', '2563309347']" "$(py 'print(t.workshop_ids("https://steamcommunity.com/sharedfiles/filedetails/?id=2824688072 2563309347, 2824688072"))')"
check "required items from a Workshop page" "[('2824688266', 'Calamity Mod Music')]" "$(py 'print(t.REQUIRED_RE.findall("<a href=\"https://steamcommunity.com/workshop/filedetails/?id=2824688266\" target=\"_blank\" data-subscribed=\"0\"> <div class=\"requiredItem\"> Calamity Mod Music </div> </a>"))')"
check "dependencies come first, once" "['3', '2', '1']" "$(py 'cache={"1":{"deps":[{"id":"2"},{"id":"3"}],"at":9e12},"2":{"deps":[{"id":"3"}],"at":9e12},"3":{"deps":[],"at":9e12}}; print(t.resolve(["1"], cache)[0])')"

# A .tmod, built the way tModLoader writes one.
python3 - "${tmp}/Fake.tmod" <<'EOF'
import struct, sys, zlib
def s(x):
    b = x.encode(); out = bytearray(); n = len(b)
    while True:
        if n < 0x80: out.append(n); break
        out.append((n & 0x7f) | 0x80); n >>= 7
    return bytes(out) + b
info = s("modReferences") + s("NeededLib@1.0") + s("") + s("displayName") + s("Fake Mod") + s("side") + s("Both") + s("")
comp = zlib.compress(info)[2:-4]
body = s("FakeMod") + s("1.2.3") + struct.pack("<i", 1) + s("Info") + struct.pack("<ii", len(info), len(comp)) + comp
open(sys.argv[1], "wb").write(b"TMOD" + s("2026.8.3.0") + b"\0" * 276 + struct.pack("<i", len(body)) + body)
EOF
check "a .tmod's name and references" "FakeMod 1.2.3 Fake Mod ['NeededLib']" "$(py 'm=t.read_tmod("'"${tmp}"'/Fake.tmod"); print(m["name"], m["version"], m["displayName"], m["modReferences"])')"
mkdir -p "${tmp}/item/2025.7" "${tmp}/item/2026.6" "${tmp}/item/2026.9"
touch "${tmp}/item/2025.7/A.tmod" "${tmp}/item/2026.6/A.tmod" "${tmp}/item/2026.9/A.tmod"
check "the newest build not newer than ours" "${tmp}/item/2026.6/A.tmod" "$(py 'print(t.best_tmod("'"${tmp}"'/item", (2026, 8)))')"

# sync-mods with an uploaded mod and no Workshop items (no network needed).
mkdir -p "${data}/Mods" && cp "${tmp}/Fake.tmod" "${data}/Mods/FakeMod.tmod"
tml sync-mods >"${tmp}/sync"
check "an uploaded mod is enabled" '["FakeMod"]' "$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1]))))' "${data}/Mods/enabled.json")"
check "and its missing dependency is said" "1" "$(grep -c 'FakeMod needs NeededLib' "${tmp}/sync")"
tml set-mods '{"disabled": ["FakeMod", "bad/name"]}' >/dev/null
tml sync-mods >/dev/null
check "a mod switched off is not in enabled.json" "[]" "$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1]))))' "${data}/Mods/enabled.json")"
check "a strange name is not kept" '["FakeMod"]' "$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1]))["disabled"]))' "${data}/flux/mods.json")"

# Players and bans.
check "playing, nobody" "[]" "$(py 'print(t.parse_playing(": No players connected."))')"
check "playing, two players" "Alice,Bob Smith" "$(py 'print(",".join(p["name"] for p in t.parse_playing(": Alice (1.2.3.4:5555)\n: Bob Smith (10.0.0.2:6000)\n: 2 players connected.")))')"
printf '//Griefer\n1.2.3.4\n//Other\n5.6.7.8\n' >"${data}/banlist.txt"
check "bans are read in pairs" "Griefer 1.2.3.4" "$(tml bans | python3 -c 'import json,sys;b=json.load(sys.stdin)["bans"][0];print(b["name"],b["ip"])')"
tml unban Griefer >/dev/null
check "unban keeps the others" "//Other 5.6.7.8" "$(tr '\n' ' ' <"${data}/banlist.txt" | sed 's/ $//')"
check "unban of nobody" "2" "$(status tml unban Nobody)"

# Worlds.
echo x >"${data}/Worlds/Other World.wld"; echo x >"${data}/Worlds/Other World.twld"; echo x >"${data}/Worlds/Other World.wld.bak"
check "the worlds list" "My World,Other World" "$(tml worlds | python3 -c 'import json,sys;print(",".join(w["name"] for w in json.load(sys.stdin)["worlds"]))')"
check "the active world cannot be deleted" "2" "$(status tml delete-world 'Other World')"
check "another one can" "0" "$(status tml delete-world 'My World')"
check "a missing world" "2" "$(status tml delete-world 'Nope')"

# Mod configs.
check "a mod config is written" "0" "$(status tml set-modconfig 'CalamityMod_Server.json' '{"a": 1}')"
check "and listed" "CalamityMod" "$(tml modconfigs | python3 -c 'import json,sys;print(json.load(sys.stdin)["configs"][0]["mod"])')"
check "a path is refused" "2" "$(status tml set-modconfig '../x.json' '{}')"

# Worlds by their EXACT file names (1.0.2): a world copied in by hand keeps any character.
printf x >"${data}/Worlds/Bob's World.wld"; printf x >"${data}/Worlds/Bob's World.twld"
printf x >"${data}/Worlds/Bob's World.wld.bak"; printf x >"${data}/Worlds/Bob's World 2.wld"
printf x >"${data}/Worlds/Bobs World.wld"
check "an existing world with a quote is taken as it is" "0" "$(status tml set-settings "{\"world\": \"Bob's World\"}")"
tml apply >/dev/null
check "and loaded by its exact file" "${data}/Worlds/Bob's World.wld" "$(cfgval world)"
check "with no new world made" "" "$(cfgval autocreate)"
tml set-settings '{"world": "Other World"}' >/dev/null
tml apply >/dev/null
check "deleting a world matches it exactly" '["Bob'"'"'s World.twld", "Bob'"'"'s World.wld", "Bob'"'"'s World.wld.bak"]' "$(tml delete-world "Bob's World" | python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin)["removed"]))')"
check "a world whose cleaned name is the same is left alone" "0" "$(status test -f "${data}/Worlds/Bobs World.wld")"
check "and one whose name starts the same" "0" "$(status test -f "${data}/Worlds/Bob's World 2.wld")"
check "a cleaned-up name is not a world" "2" "$(status tml delete-world 'Bobs World 2')"

# playing: only the answer, between the command and its summary.
check "chat around the answer is not a player" "Disconnected Dan" "$(py 'print(",".join(p["name"] for p in t.parse_playing("<Bob> brb (10.0.0.1)\n: Disconnected Dan (1.2.3.4:1)\n: 1 player connected.\n<Amy> hi (5.6.7.8)")))')"
check "no summary, no players" "[]" "$(py 'print(t.parse_playing("Alice (1.2.3.4:5)"))')"
check "versions from the banner" "('2026.8.3.0', '1.4.4.9')" "$(py 'print(t.game_versions(["Terraria Server v1.4.4.9 - tModLoader v2026.8.3.0"]))')"

# Pending changes: an uploaded or removed .tmod, a mod config, a mark.
tml apply >/dev/null
check "nothing pending right after a start" "False" "$(tml status | python3 -c 'import json,sys;print(json.load(sys.stdin)["pending"])')"
cp "${tmp}/Fake.tmod" "${data}/Mods/Other.tmod"
check "an uploaded .tmod is pending" "True" "$(tml status | python3 -c 'import json,sys;print(json.load(sys.stdin)["pending"])')"
tml apply >/dev/null; rm -f "${data}/Mods/Other.tmod"
check "a removed .tmod is pending" "True" "$(tml status | python3 -c 'import json,sys;print(json.load(sys.stdin)["pending"])')"
tml apply >/dev/null; tml set-modconfig 'X_Y.json' '{}' >/dev/null
check "a mod config is pending" "True" "$(tml status | python3 -c 'import json,sys;print(json.load(sys.stdin)["pending"])')"
tml apply >/dev/null; tml mark-pending 'upload' >/dev/null
check "a mark is pending" "True" "$(tml status | python3 -c 'import json,sys;print(json.load(sys.stdin)["pending"])')"
tml apply >/dev/null
check "and applied by the next start" "False" "$(tml status | python3 -c 'import json,sys;print(json.load(sys.stdin)["pending"])')"

# The same mod twice.
cp "${tmp}/Fake.tmod" "${data}/Mods/FakeMod-copy.tmod"
tml set-mods '{"disabled": []}' >/dev/null
tml sync-mods >"${tmp}/sync"
check "two copies of one mod are said" "1" "$(grep -c 'two copies of FakeMod' "${tmp}/sync")"
check "and flagged" "True True" "$(tml mods | python3 -c 'import json,sys;print(" ".join(str(m.get("duplicate")) for m in json.load(sys.stdin)["mods"] if m["name"]=="FakeMod"))')"
rm -f "${data}/Mods/FakeMod-copy.tmod"

# An empty variable is unset, for every setting.
tml set-settings '{"maxPlayers": 12}' >/dev/null
check "an empty TML_MAX_PLAYERS leaves the panel's" "12" "$(tml TML_MAX_PLAYERS= apply >/dev/null; cfgval maxplayers)"

# Backups: a start backup only when the newest is old; the rotation keeps a day's and a week's.
export FLUX_BACKUP_KEEP=3
check "a backup" "1" "$(tml backup manual | grep -c 'backup (manual)')"
check "a start backup right after it is skipped" "1" "$(tml backup start | grep -c 'skipped')"
check "the backups list" "manual" "$(tml backups | python3 -c 'import json,sys;print(json.load(sys.stdin)["backups"][0]["why"])')"
check "keep: the newest 2, plus the newest over a day and over a week old" "a b e f" "$(py 'k=t.backups_to_keep([("a",1000000),("b",999000),("c",990000),("d",950000),("e",900000),("f",300000)],2,1000000); print(" ".join(sorted(k)))')"
unset FLUX_BACKUP_KEEP

# Restore: put back, with a pre-restore backup of what was there.
printf before >"${data}/Worlds/Other World.wld"
name="$(tml backups | python3 -c 'import json,sys;print(json.load(sys.stdin)["backups"][0]["name"])')"
cp "${data}/flux/backups/${name}" "${tmp}/b.tgz"
printf changed >"${data}/Worlds/Other World.wld"; printf new >"${data}/Worlds/Brand New.wld"
check "a restore of an unknown backup is refused" "2" "$(status tml restore '../x.tar.gz')"
check "a restore is asked for (no server: on the next start)" "start" "$(tml restore "${name}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["appliesOn"])')"
check "and is pending" "True" "$(tml status | python3 -c 'import json,sys;print(json.load(sys.stdin)["pending"])')"
sleep 1
tml do-restore >"${tmp}/restore"
check "the world is back" "x" "$(cat "${data}/Worlds/Other World.wld")"
check "a world made since is gone with it" "1" "$(status test -f "${data}/Worlds/Brand New.wld")"
check "what was there is in a pre-restore backup" "pre-restore" "$(tml backups | python3 -c 'import json,sys;print(json.load(sys.stdin)["backups"][0]["why"])')"
check "the request is done" "1" "$(status test -f "${data}/flux/restore-request.json")"

# Releases without the API: an exact version is its tag; a cache is used; GitHub's "wait" is kept.
check "an exact version needs no list" "v2026.07.1.0" "$(env -i PATH="${PATH}" FLUX_DATA_DIR="${data}" FLUX_RELEASES_API=http://127.0.0.1:9/ PYTHONPATH=scripts python3 -c 'import fluxtml as t; print(t.latest_release_tag("2026.7.1.0"))')"
printf '{"at": %s, "releases": [{"tag_name": "v2026.08.3.0", "prerelease": false}]}' "$(date +%s)" >"${data}/flux/releases.json"
check "a fresh cache answers" "v2026.08.3.0" "$(env -i PATH="${PATH}" FLUX_DATA_DIR="${data}" FLUX_RELEASES_API=http://127.0.0.1:9/ PYTHONPATH=scripts python3 -c 'import fluxtml as t; print(t.latest_release_tag("stable"))')"
printf '{"at": 1, "backoffUntil": %s, "releases": [{"tag_name": "v2026.08.3.0", "prerelease": false}]}' "$(( $(date +%s) + 600 ))" >"${data}/flux/releases.json"
check "an old cache is used while GitHub said to wait" "v2026.08.3.0" "$(env -i PATH="${PATH}" FLUX_DATA_DIR="${data}" FLUX_RELEASES_API=http://127.0.0.1:9/ PYTHONPATH=scripts python3 -c 'import fluxtml as t; print(t.latest_release_tag("stable"))')"

# Failed updates are left alone for a while.
mkdir -p "${tmp}/server/workshop/steamapps/workshop"
printf '"AppWorkshop"\n{\n\t"WorkshopItemsInstalled"\n\t{\n\t\t"111"\n\t\t{\n\t\t\t"size"\t\t"1"\n\t\t\t"timeupdated"\t\t"100"\n\t\t}\n\t}\n}\n' >"${tmp}/server/workshop/steamapps/workshop/appworkshop_1281930.acf"
printf '{"targets": ["mod:111:200"]}' >"${data}/flux/update-wanted.json"
tml sync-mods >"${tmp}/sync"
check "an update that did not download is remembered" "1" "$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "${data}/flux/update-failed.json")"
check "and said" "1" "$(grep -c 'did not download' "${tmp}/sync")"
check "for six hours" "True" "$(env -i PATH="${PATH}" FLUX_DATA_DIR="${data}" FLUX_SERVER_DIR="${tmp}/server" PYTHONPATH=scripts python3 -c 'import fluxtml as t; print("mod:111:200" in t.failed_updates())')"
check "not after" "False" "$(env -i PATH="${PATH}" FLUX_DATA_DIR="${data}" FLUX_SERVER_DIR="${tmp}/server" PYTHONPATH=scripts python3 -c 'import fluxtml as t, time; print("mod:111:200" in t.failed_updates(time.time() + 7 * 3600))')"

printf '\n%d passed, %d failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ]
