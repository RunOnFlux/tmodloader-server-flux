#!/bin/bash
# PID 1, and the supervisor of the tModLoader server.
#
# The game runs as a CHILD, never exec, so a stop reaches us first and becomes a save, and a
# server that ends on its own is followed by the next one in place:
#
#   - every start: a restore the panel asked for (fluxtml.py do-restore), the tModLoader build
#     TML_VERSION names (GitHub releases, no Steam login), the Workshop mods and every mod they
#     require (SteamCMD, anonymous), Mods/enabled.json, a backup of the worlds (skipped when the
#     newest is under an hour old), serverconfig.txt from the environment and the panel, then the
#     server, with its console on a FIFO the supervisor holds open
#   - an update that could not be installed is not tried again for FLUX_UPDATE_RETRY_HOURS
#   - while it runs: `save` every TML_AUTOSAVE_MINUTES; a newer tModLoader (stable/preview) or a
#     mod updated on the Workshop restarts it onto the new files, players warned in chat first
#   - TML_RESTART_TIME: a daily restart at that time, players warned in chat first
#   - SIGTERM/SIGINT from Docker: `exit` on the console, which saves and quits (tModLoader also
#     saves on SIGTERM, which is the fallback)
#   - the server ending on its own: a new generation in the same container;
#     FLUX_RESTART_MAX_ATTEMPTS unplanned ones inside FLUX_RESTART_WINDOW seconds end it with 42
#
# Same shape as runonflux/corekeeper-server-flux and runonflux/unturned-server-flux.

set -uo pipefail
# shellcheck source=scripts/flux-lib.sh
source /opt/flux/flux-lib.sh

FLUX_RESTART_MAX_ATTEMPTS="${FLUX_RESTART_MAX_ATTEMPTS:-5}"
FLUX_RESTART_WINDOW="${FLUX_RESTART_WINDOW:-3600}"
FLUX_RESTART_BACKOFF="${FLUX_RESTART_BACKOFF:-10}"
FLUX_STOP_GRACE="${FLUX_STOP_GRACE:-90}"
FLUX_BACKUP_HOURS="${FLUX_BACKUP_HOURS:-6}"
FLUX_BACKUP_KEEP="${FLUX_BACKUP_KEEP:-10}"
export FLUX_BACKUP_KEEP
FLUX_UPDATE_CHECK_MINUTES="${FLUX_UPDATE_CHECK_MINUTES:-60}"
FLUX_WARN_MINUTES="${FLUX_WARN_MINUTES:-5}"

tml() { python3 /opt/flux/fluxtml.py "$@"; }

rm -f "${FLUX_RESTART_MARKER}"
mkdir -p "${FLUX_SERVER_DIR}" "${FLUX_DATA_DIR}" "${FLUX_STATE_DIR}" "${FLUX_GAME_LOG_DIR}"
# Image 1.0.1 kept the game's logs on the synced data volume: gone, they are on the install now.
rm -rf "${FLUX_STATE_DIR}/logs" "${FLUX_STATE_DIR}/current.log"

if ! port="$(flux_valid_port "${TML_PORT:-7777}")"; then
  flux_log "ERROR TML_PORT='${TML_PORT:-}' is not a port from 1024 to 65535; refusing to start"
  tml state crashed "TML_PORT is not a usable port"
  exit 42
fi
export TML_PORT="${port}"
autosave="$(flux_minutes "${TML_AUTOSAVE_MINUTES:-}" 10)"
if ! restart_at="$(flux_restart_time "${TML_RESTART_TIME:-}")"; then
  flux_log "WARN TML_RESTART_TIME='${TML_RESTART_TIME}' is not HH:MM; no daily restart"
  restart_at=""
fi
flux_log "flux-entrypoint starting (image ${FLUX_IMAGE_VERSION:-dev}, tModLoader ${TML_VERSION:-stable}, port ${port}/tcp, autosave ${autosave} min, daily restart ${restart_at:-off})"

game_pid=""
tail_pid=""
watch_pid=""
saver_pid=""
backup_pid=""
terminating=0
generation=0
restart_history=()

rm -f "${FLUX_CONSOLE_FIFO}"
mkfifo -m 600 "${FLUX_CONSOLE_FIFO}"
exec 3<>"${FLUX_CONSOLE_FIFO}"

# Saves and quits: `exit` on the console. A server that does not honour it is ended by
# wait_for_generation after FLUX_STOP_GRACE.
stop_server() {
  [ -n "${game_pid}" ] || return 0
  printf 'exit\n' >&3
}

# shellcheck disable=SC2329  # invoked by the trap below
term_handler() {
  terminating=1
  flux_log "stop requested: saving the world and shutting down, no restart will follow"
  stop_server
}
trap term_handler TERM INT

# A .tar.gz of the worlds, the mods' configs, the panel's settings and the ban list
# (fluxtml.py backup: the rotation that never loses the last day's and the last week's).
backup_world() {
  local line
  while IFS= read -r line; do
    flux_log "${line#\# }"
  done < <(tml backup "$1" 2>&1)
}

# shellcheck disable=SC2329  # run in the background below
backupper() {
  [ "${FLUX_BACKUP_HOURS}" -gt 0 ] 2>/dev/null || exit 0
  while true; do
    sleep $((FLUX_BACKUP_HOURS * 3600))
    flux-console save >/dev/null 2>&1 || continue
    sleep 5
    backup_world "every ${FLUX_BACKUP_HOURS}h"
  done
}

# shellcheck disable=SC2329  # run in the background below
autosaver() {
  [ "${autosave}" -gt 0 ] || exit 0
  while true; do
    sleep $((autosave * 60))
    flux-console save >/dev/null 2>&1 && flux_log "autosave"
  done
}

# How many players are on the server, from `playing`. "" when it cannot tell.
player_count() {
  flux-console playing 2>/dev/null | awk '
    /No players connected/ { n = 0 }
    /players? connected\./ { if (match($0, /[0-9]+/)) n = substr($0, RSTART, RLENGTH) }
    END { if (n != "") print n }'
}

# Warns the players in chat for FLUX_WARN_MINUTES (skipped on an empty server), then restarts.
restart_with_warning() {
  local why="$1" left="${FLUX_WARN_MINUTES}"
  while [ "${left}" -gt 0 ] && [ "$(player_count)" != "0" ]; do
    flux-console "say [Server] Restarting in ${left} minute$([ "${left}" = 1 ] || echo s): ${2:-maintenance}. The world is saved first." >/dev/null 2>&1
    sleep 60
    left=$((left - 1))
  done
  printf '%s\n' "${why}" >"${FLUX_RESTART_MARKER}"
  flux_log "${why}: restarting the server"
  flux-console exit >/dev/null 2>&1 || kill -TERM "$(flux_game_pid)" 2>/dev/null
}

# shellcheck disable=SC2329  # run in the background below
# Marks the server running once it listens, then follows tModLoader and Workshop updates and
# keeps the daily restart.
watcher() {
  local line last_check now next_restart="" updates
  while IFS= read -r line; do
    if [[ "${line}" == *"Listening on port"* ]]; then
      tml state running
      flux_log "the server is up (generation ${generation})"
      break
    fi
  done < <(tail -n +1 -F "$(flux_game_log)" 2>/dev/null)
  last_check="$(date -u +%s)"
  [ -n "${restart_at}" ] && next_restart="$(flux_next_daily "$(date -u +%s)" "${restart_at}")"
  while true; do
    sleep 60
    flux_cap_log "$(flux_game_log)"
    now="$(date -u +%s)"
    if [ "${FLUX_UPDATE_CHECK_MINUTES}" -gt 0 ] 2>/dev/null && [ $((now - last_check)) -ge $((FLUX_UPDATE_CHECK_MINUTES * 60)) ]; then
      last_check="${now}"
      updates="$(tml updates 2>/dev/null)"
      if [ -n "${updates}" ]; then
        flux_log "updates: $(tr '\n' ';' <<<"${updates}")"
        restart_with_warning "planned: update ($(head -1 <<<"${updates}"))" "an update for the server or its mods"
        exit 0
      fi
    fi
    if [ -n "${next_restart}" ] && [ "${now}" -ge "${next_restart}" ]; then
      restart_with_warning "planned: the daily restart (${restart_at})" "the daily restart"
      exit 0
    fi
  done
}

start_generation() {
  local line folder current
  generation=$((generation + 1))
  rm -f "${FLUX_RESTART_MARKER}"

  export FLUX_GENERATION="${generation}"
  while IFS= read -r line; do
    flux_log "${line#\# }"
  done < <(tml do-restore 2>&1)
  tml state updating "Installing tModLoader and the mods"
  folder=""
  while IFS= read -r line; do
    case "${line}" in
      "# "*) flux_log "install: ${line#\# }" ;;
      "{"*) flux_log "ERROR ${line}" ;;
      /*) folder="${line}" ;;
    esac
  done < <(tml install 2>&1)
  if [ -z "${folder}" ] || [ ! -f "${folder}/tModLoader.dll" ]; then
    flux_log "ERROR tModLoader could not be installed"
    return 1
  fi
  [ "${terminating}" = "1" ] && return 2
  while IFS= read -r line; do
    flux_log "mods: ${line#\# }"
  done < <(tml sync-mods 2>&1)
  [ "${terminating}" = "1" ] && return 2
  backup_world "start"
  while IFS= read -r line; do
    flux_log "config: ${line#\# }"
  done < <(tml apply 2>&1)

  current="$(flux_game_log)"
  if [ -s "${current}" ]; then
    mv "${current}" "${FLUX_GAME_LOG_DIR}/$(date -u +%Y%m%d-%H%M%S)-server.log"
  fi
  : >"${current}"
  flux_prune "${FLUX_GAME_LOG_DIR}" '*-server.log' "${FLUX_GAME_LOG_KEEP}"
  [ "${terminating}" = "1" ] && return 2

  tml state starting "Loading the mods and the world"
  flux_log "starting tModLoader $(basename "${folder}") (generation ${generation})"
  tail -n 0 -F "${current}" 2>/dev/null &
  tail_pid=$!

  (
    cd "${folder}" || exit 1
    # -nosteam: a rented server has no Steam client to make lobbies with; players join by IP.
    # The Workshop folder is our download of the mods (SteamCMD), outside the save directory.
    exec dotnet tModLoader.dll -server -nosteam -config "${FLUX_STATE_DIR}/serverconfig.txt" \
      -tmlsavedirectory "${FLUX_DATA_DIR}" \
      -steamworkshopfolder "${FLUX_SERVER_DIR}/workshop/steamapps/workshop" \
      <"${FLUX_CONSOLE_FIFO}" >>"${current}" 2>&1
  ) &
  game_pid=$!

  watcher &
  watch_pid=$!
  autosaver &
  saver_pid=$!
  backupper &
  backup_pid=$!
  return 0
}

sweep_generation() {
  local pid
  for pid in "${tail_pid}" "${watch_pid}" "${saver_pid}" "${backup_pid}"; do
    [ -n "${pid}" ] && kill "${pid}" 2>/dev/null
  done
  tail_pid="" watch_pid="" saver_pid="" backup_pid=""
  pkill -f "tail -n [0+1]* -F $(flux_game_log)" 2>/dev/null
  pkill -KILL -f '^dotnet tModLoader\.dll ' 2>/dev/null
  game_pid=""
}

wait_for_generation() {
  local asked_at=0 now
  while kill -0 "${game_pid}" 2>/dev/null; do
    sleep 1 &
    wait $!
    if [ "${terminating}" = "1" ] || [ -f "${FLUX_RESTART_MARKER}" ]; then
      now="$(date -u +%s)"
      [ "${asked_at}" = "0" ] && asked_at="${now}"
      if [ $((now - asked_at)) -ge "${FLUX_STOP_GRACE}" ]; then
        flux_log "WARN the server was asked to stop ${FLUX_STOP_GRACE}s ago and has not; ending it"
        kill -KILL "${game_pid}" 2>/dev/null
        break
      fi
    else
      asked_at=0
    fi
  done
  wait "${game_pid}" 2>/dev/null
}

while true; do
  if [ "${terminating}" = "1" ]; then
    tml state stopped
    exit 0
  fi
  start_generation
  started=$?
  if [ "${started}" = "2" ]; then
    flux_log "stopped on request before the server started"
    tml state stopped
    exit 0
  elif [ "${started}" != "0" ]; then
    flux_log "ERROR the server could not be prepared; ending the container (42)"
    tml state crashed "tModLoader could not be installed or started"
    exit 42
  fi
  wait_for_generation
  rc=$?

  reason=""
  [ -f "${FLUX_RESTART_MARKER}" ] && reason="$(cat "${FLUX_RESTART_MARKER}" 2>/dev/null)"
  sweep_generation
  rm -f "${FLUX_RESTART_MARKER}"

  if [ "${terminating}" = "1" ]; then
    flux_log "stopped on request (server exited ${rc})"
    tml state stopped
    exit 0
  fi
  [ -z "${reason}" ] && reason="the server ended on its own (exit ${rc})"

  if flux_restart_counts "${reason}"; then
    now="$(date -u +%s)"
    read -r -a restart_history <<<"$(flux_restarts_in_window "${now}" "${FLUX_RESTART_WINDOW}" "${restart_history[@]}")"
    restart_history+=("${now}")
    if [ "${#restart_history[@]}" -gt "${FLUX_RESTART_MAX_ATTEMPTS}" ]; then
      flux_log "${reason}: ${#restart_history[@]} restarts within ${FLUX_RESTART_WINDOW}s; ending the container (42)"
      tml state crashed "The server keeps stopping: ${reason}"
      exit 42
    fi
  fi

  tml state restarting "${reason}"
  flux_log "${reason}: restarting the server in place in ${FLUX_RESTART_BACKOFF}s"
  sleep "${FLUX_RESTART_BACKOFF}" &
  wait $!
done
