#!/bin/bash
# Shared helpers for the tModLoader image.
#
# SOURCING THIS FILE MUST STAY SIDE-EFFECT FREE: tests/test-flux.sh sources it directly and
# calls the functions with made-up inputs. Everything that decides something takes its inputs
# as arguments, so the decision can be tested without a server, Steam or Docker.

# ---------------------------------------------------------------------------------------------
# Paths. Two volumes: the install (tModLoader builds and the Workshop downloads; disposable,
# local to the node) and the data (the save directory: Worlds/, Mods/, ModConfigs/, our flux/).
# ---------------------------------------------------------------------------------------------
FLUX_SERVER_DIR="${FLUX_SERVER_DIR:-/mnt/tml/server}"
FLUX_DATA_DIR="${FLUX_DATA_DIR:-/mnt/tml/data}"
FLUX_STATE_DIR="${FLUX_STATE_DIR:-${FLUX_DATA_DIR}/flux}"
FLUX_LOG="${FLUX_LOG:-${FLUX_STATE_DIR}/flux.log}"
FLUX_LOG_MAX_LINES="${FLUX_LOG_MAX_LINES:-2000}"
# The game's console logs: on the INSTALL volume (local to the node), not the synced data volume,
# where a log that grows all day kept Syncthing busy. Each one is capped at
# FLUX_GAME_LOG_MAX_BYTES (copied aside and truncated) and the newest FLUX_GAME_LOG_KEEP kept.
FLUX_GAME_LOG_DIR="${FLUX_GAME_LOG_DIR:-${FLUX_SERVER_DIR}/flux-logs}"
FLUX_GAME_LOG_KEEP="${FLUX_GAME_LOG_KEEP:-10}"
FLUX_GAME_LOG_MAX_BYTES="${FLUX_GAME_LOG_MAX_BYTES:-20000000}"
FLUX_BACKUP_DIR="${FLUX_BACKUP_DIR:-${FLUX_STATE_DIR}/backups}"
FLUX_RESTART_MARKER="${FLUX_RESTART_MARKER:-/tmp/flux-restart-requested}"
# The server's console is its stdin: a FIFO the supervisor holds open; flux-console writes one
# command into it and prints the answer from the log.
FLUX_CONSOLE_FIFO="${FLUX_CONSOLE_FIFO:-/tmp/flux-console}"
FLUX_CONSOLE_LOCK="${FLUX_CONSOLE_LOCK:-/tmp/flux-console.lock}"

flux_log() {
  local line
  line="$(date -u '+%Y-%m-%dT%H:%M:%SZ') [flux] $*"
  printf '%s\n' "${line}"
  if [ -n "${FLUX_LOG}" ] && mkdir -p "$(dirname "${FLUX_LOG}")" 2>/dev/null; then
    printf '%s\n' "${line}" >>"${FLUX_LOG}" 2>/dev/null || return 0
    flux_trim_file "${FLUX_LOG}" "${FLUX_LOG_MAX_LINES}"
  fi
}

# Keeps the last $2 lines of $1. Checked cheaply first: most calls change nothing.
flux_trim_file() {
  local file="$1" max="$2" lines
  lines="$(wc -l <"${file}" 2>/dev/null || echo 0)"
  if [ "${lines}" -gt $((max + max / 10)) ]; then
    tail -n "${max}" "${file}" >"${file}.tmp" 2>/dev/null && mv "${file}.tmp" "${file}"
  fi
}

flux_truthy() {
  case "${1,,}" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

# $1 = TML_PORT as given. Prints the port, or returns 1 for a value that is not one.
flux_valid_port() {
  local value="${1:-7777}"
  [[ "${value}" =~ ^[0-9]{1,5}$ ]] || return 1
  value=$((10#${value}))
  [ "${value}" -ge 1024 ] && [ "${value}" -le 65535 ] || return 1
  printf '%s' "${value}"
}

# $1 = minutes as given, $2 = the default. Prints 0 (off) or 1 to 120.
flux_minutes() {
  local value="${1:-$2}"
  [[ "${value}" =~ ^[0-9]+$ ]] || value="$2"
  value=$((10#${value}))
  [ "${value}" -gt 120 ] && value=120
  printf '%s' "${value}"
}

# $1 = TML_RESTART_TIME as given. Prints HH:MM, or "" when off. Returns 1 for a value that is not
# a time.
flux_restart_time() {
  local value="${1-}" h m
  case "${value,,}" in
    "" | off | none | false | 0) printf ''; return 0 ;;
  esac
  [[ "${value}" =~ ^([0-9]{1,2}):([0-9]{2})$ ]] || return 1
  h=$((10#${BASH_REMATCH[1]})) m=$((10#${BASH_REMATCH[2]}))
  [ "${h}" -le 23 ] && [ "${m}" -le 59 ] || return 1
  printf '%02d:%02d' "${h}" "${m}"
}

# $1 = now (epoch seconds), $2 = HH:MM in the container's time zone (TZ; UTC when unset). Prints
# the epoch of the next time it is $2: today when that is still ahead, tomorrow otherwise.
flux_next_daily() {
  local now="$1" at
  at="$(date -d "$(date -d "@${now}" +%F) $2" +%s 2>/dev/null)" || return 1
  if [ "${at}" -le "${now}" ]; then
    at="$(date -d "$(date -d "@$((now + 86400))" +%F) $2" +%s 2>/dev/null)" || return 1
  fi
  printf '%s' "${at}"
}

# ---------------------------------------------------------------------------------------------
# The supervisor's restart budget.
# ---------------------------------------------------------------------------------------------

# $1 = now (epoch seconds), $2 = window in seconds, $3.. = the times of earlier restarts.
flux_restarts_in_window() {
  local now="$1" window="$2" t kept=()
  shift 2
  for t in "$@"; do
    [ "${t}" -gt $((now - window)) ] && kept+=("${t}")
  done
  printf '%s' "${kept[*]}"
}

# $1 = why the last generation ended. A restart the supervisor planned or the panel asked for is
# the plan working, not a crash.
flux_restart_counts() {
  case "$1" in
    planned* | requested*) return 1 ;;
    *) return 0 ;;
  esac
}

# $1 = number to keep, then the files NEWEST FIRST. Prints the ones to delete.
flux_files_to_prune() {
  local keep="$1" i=0 f
  shift
  for f in "$@"; do
    i=$((i + 1))
    [ "${i}" -gt "${keep}" ] && printf '%s\n' "${f}"
  done
  return 0
}

# $1 = directory, $2 = glob, $3 = number to keep.
flux_prune() {
  local f files=()
  # shellcheck disable=SC2012,SC2086  # $2 is a glob on purpose; names are ours, no spaces
  mapfile -t files < <(ls -t "$1"/$2 2>/dev/null)
  while IFS= read -r f; do
    [ -n "${f}" ] && rm -f "${f}"
  done < <(flux_files_to_prune "$3" "${files[@]}")
}

# ---------------------------------------------------------------------------------------------
# The running server.
# ---------------------------------------------------------------------------------------------

flux_game_log() {
  printf '%s' "${FLUX_GAME_LOG_DIR}/current.log"
}

# $1 = the current game log. Past FLUX_GAME_LOG_MAX_BYTES it is copied aside and truncated in
# place: the server writes it with O_APPEND (>>), so it goes on at the start of the emptied file.
flux_cap_log() {
  local log="$1" size
  size="$(stat -c %s "${log}" 2>/dev/null || echo 0)"
  [ "${size}" -gt "${FLUX_GAME_LOG_MAX_BYTES}" ] || return 0
  cp "${log}" "${FLUX_GAME_LOG_DIR}/$(date -u +%Y%m%d-%H%M%S)-server.log" 2>/dev/null
  : >"${log}"
  flux_prune "${FLUX_GAME_LOG_DIR}" '*-server.log' "${FLUX_GAME_LOG_KEEP}"
}

flux_game_pid() {
  pgrep -f '^dotnet tModLoader\.dll ' 2>/dev/null | head -1
}

# $1 = a console command as given. One line, no control characters.
flux_console_line() {
  local cmd="${1//[$'\r\n']/ }"
  printf '%s' "${cmd}" | LC_ALL=C tr -d '\000-\037\177'
}

# $1 = one console command, written into the server's stdin; returns 1 when no server is
# reading it, without blocking.
flux_console() {
  local cmd
  [ -p "${FLUX_CONSOLE_FIFO}" ] || return 1
  [ -n "$(flux_game_pid)" ] || return 1
  cmd="$(flux_console_line "$1")"
  # shellcheck disable=SC2016  # expanded by the inner bash, from its own arguments
  timeout 2 bash -c 'printf "%s\n" "$1" >"$2"' _ "${cmd}" "${FLUX_CONSOLE_FIFO}"
}

# stdin = the console's lines after a command. Prints its answer: the server prefixes every
# console answer with ": " and echoes nothing else, so the lines up to the next prompt are it.
flux_console_answer() {
  sed -e 's/\r$//' -e 's/^: //'
}
