#!/bin/bash
# shellcheck disable=SC2016  # wait_for conditions are single-quoted and eval-ed later, on purpose
# Runs INSIDE the image (tests/test-image.sh). The repository's tests/ folder is mounted at /t
# and scripts/ at /s.
set -uo pipefail
export LC_ALL=C

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
wait_for() {
  local _
  for _ in $(seq 1 $(($2 * 5))); do
    eval "$1" && return 0
    sleep 0.2
  done
  return 1
}

# --- What the scripts need is in the image -----------------------------------------------------
for tool in steamcmd python3 dotnet pgrep pkill timeout tail tar flock mkfifo; do
  check "${tool} is installed" "0" "$(status command -v "${tool}")"
done
check "the .NET 8 runtime" "1" "$(dotnet --list-runtimes | grep -c 'Microsoft.NETCore.App 8\.')"
check "flux-tml is on the PATH" "0" "$(status command -v flux-tml)"
check "flux-console is on the PATH" "0" "$(status command -v flux-console)"
check "flux-tml starts (usage)" "64" "$(status flux-tml)"
check "the image's scripts are the repository's" "0" "$(status diff -r --exclude=__pycache__ /s /opt/flux)"
check "the unit tests pass with the image's Python" "0" "$(status bash -c 'cd /tmp && rm -rf repo && mkdir repo && cp -r /s repo/scripts && cp -r /t repo/tests && cd repo && ./tests/test-flux.sh')"

# --- The supervisor, around a stand-in tModLoader ----------------------------------------------
# An installed "tModLoader" whose dll is a script run by a stand-in dotnet: it creates the world,
# prints the server's lines, answers `playing` and `save`, and saves and quits on `exit`.
server=/mnt/tml/server
data=/mnt/tml/data
mkdir -p "${server}/tml/v2026.08.3.0"
# The "dll" IS the stand-in server (a bash script); the stand-in dotnet runs it as
# `dotnet tModLoader.dll ...`, the command line the supervisor and flux-tml look for.
cat >"${server}/tml/v2026.08.3.0/tModLoader.dll" <<'GAME'
cfg="" save=""
while [ $# -gt 0 ]; do
  case "$1" in -config) cfg="$2"; shift ;; -tmlsavedirectory) save="$2"; shift ;; esac
  shift
done
world="$(sed -n 's/^world=//p' "${cfg}")"
mkdir -p "$(dirname "${world}")"
[ -f "${world}" ] || echo created >"${world}"
echo "Terraria Server v1.4.4.9 - tModLoader v2026.8.3.0"
echo "Listening on port 7777"
while IFS= read -r line; do
  case "${line}" in
    playing) echo ": No players connected." ;;
    save) echo saved >>"${world}"; echo ": Saving world data" ;;
    exit) echo ": Saving before exit..."; echo saved-on-exit >>"${world}"; exit 0 ;;
  esac
done
GAME
mv /usr/bin/dotnet /usr/bin/dotnet.real 2>/dev/null || true
cat >/usr/bin/dotnet <<'STUB'
#!/bin/bash
[ "$1" = "--list-runtimes" ] && exec /usr/bin/dotnet.real "$@"
exec -a dotnet bash "$@"
STUB
chmod +x /usr/bin/dotnet

export FLUX_RELEASES_API="http://127.0.0.1:9/" FLUX_RELEASES_LATEST="http://127.0.0.1:9/latest" TML_VERSION=stable FLUX_RESTART_BACKOFF=1 FLUX_UPDATE_CHECK_MINUTES=0 TML_WORLD_NAME="Image Test"
/opt/flux/flux-entrypoint.sh >/tmp/entry.log 2>&1 &
entry=$!
check "the server comes up" "0" "$(status wait_for 'grep -q "\"running\"" ${data}/flux/state.json 2>/dev/null' 30)"
check "on the installed build when GitHub is unreachable" "1" "$(grep -c 'starting the installed v2026.08.3.0' /tmp/entry.log)"
check "the world was created" "0" "$(status test -f "${data}/Worlds/Image Test.wld")"
check "enabled.json is written" "[]" "$(tr -d ' \n' <"${data}/Mods/enabled.json")"
check "the console answers" "No players connected." "$(flux-console playing)"
check "status reads the console" "[]" "$(flux-tml status | python3 -c 'import json,sys;print(json.load(sys.stdin)["players"])')"
check "the game's log is on the install volume, not the synced data" "0" "$(status test -s /mnt/tml/server/flux-logs/current.log)"
check "and none on the data volume" "1" "$(status test -e "${data}/flux/current.log")"
check "the status has the generation" "1" "$(flux-tml status | python3 -c 'import json,sys;print(json.load(sys.stdin)["generation"])')"

flux-tml set-settings '{"world": "Second"}' >/dev/null
flux-tml restart >/dev/null
check "a panel restart saves through exit" "0" "$(status wait_for 'grep -q saved-on-exit "${data}/Worlds/Image Test.wld"' 20)"
check "and starts the panel's world" "0" "$(status wait_for 'test -f ${data}/Worlds/Second.wld && grep -q "\"running\"" ${data}/flux/state.json' 30)"
check "the restart was not counted as a crash" "0" "$(grep -c 'restarts within' /tmp/entry.log)"
check "a backup on the second start" "0" "$(status wait_for 'ls ${data}/flux/backups/*-tmodloader.tar.gz' 5)"
check "a new generation" "2" "$(flux-tml status | python3 -c 'import json,sys;print(json.load(sys.stdin)["generation"])')"

# Restore, through the supervisor: the server is stopped, the backup put back, started again.
backup="$(flux-tml backups | python3 -c 'import json,sys;print(json.load(sys.stdin)["backups"][0]["name"])')"
printf x >"${data}/Worlds/Made Later.wld"
check "a restore is applied now" "now" "$(flux-tml restore "${backup}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["appliesOn"])')"
check "the server comes back after the restore" "0" "$(status wait_for 'grep -q "\"generation\": 3" ${data}/flux/state.json && grep -q "\"running\"" ${data}/flux/state.json' 40)"
check "the backup's worlds are back (a world made later is gone)" "1" "$(status test -f "${data}/Worlds/Made Later.wld")"
check "with a pre-restore backup of what was there" "1" "$(flux-tml backups | python3 -c 'import json,sys;print(sum(1 for b in json.load(sys.stdin)["backups"] if b["why"]=="pre-restore"))')"
check "the restore is not counted as a crash" "0" "$(grep -c 'restarts within' /tmp/entry.log)"

kill -TERM "${entry}"
check "a stop ends the supervisor" "0" "$(status wait_for '! kill -0 ${entry} 2>/dev/null' 20)"
check "and saves the world" "0" "$(status grep -q saved-on-exit "${data}/Worlds/Second.wld")"
check "and says stopped" "stopped" "$(python3 -c 'import json;print(json.load(open("'"${data}"'/flux/state.json"))["state"])')"

printf '\n%d passed, %d failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ] || { echo '--- entrypoint log'; cat /tmp/entry.log; exit 1; }
