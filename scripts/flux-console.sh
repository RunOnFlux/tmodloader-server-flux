#!/bin/bash
# flux-console <command>: types one command into the tModLoader server's console and prints its
# answer (the lines the server printed in the next FLUX_CONSOLE_WAIT seconds). One command at a
# time: a lock is held over the command and its answer, so two answers never mix.
set -uo pipefail
# shellcheck source=scripts/flux-lib.sh
source /opt/flux/flux-lib.sh

[ $# -ge 1 ] || { echo "usage: flux-console <command>" >&2; exit 64; }
wait_s="${FLUX_CONSOLE_WAIT:-1}"
log="$(flux_game_log)"

exec 9>"${FLUX_CONSOLE_LOCK}"
flock -w 15 9 || { echo "the console is busy" >&2; exit 75; }
start="$(stat -c %s "${log}" 2>/dev/null || echo 0)"
flux_console "$*" || { echo "the server is not running" >&2; exit 3; }
sleep "${wait_s}"
tail -c +"$((start + 1))" "${log}" 2>/dev/null | flux_console_answer
