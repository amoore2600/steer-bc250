#!/usr/bin/env bash
# queue_run.sh -- a local first-come-first-served queue for heavy jobs (Gowin synthesis, long simulations). It is the
# default queue runner of `grade.py --syn-only`, of the coordinator's queued synthesis and of the kat-sections
# integration gate; STEER_HEAVY_RUN=<path> replaces it with any runner that has the same command line and output.
#
#   queue_run.sh [--who <name>] [--kind gowin|sim] [--mem 4G] [--timeout <s>] [--desktop-only] [--env NAME=VALUE]...
#                -- <cmd...>
#   queue_run.sh --status
#   queue_run.sh --cancel <ticket> [--who <name>]
#
# A job writes a ticket file t-<time>-<ticket> into $HEAVY_QUEUE_DIR (default /tmp/hdl-queue) and polls every
# QUEUE_POLL_S seconds (default 5) until it holds the oldest live ticket and one of the heavy lanes is free. The lanes
# are the directories lib/heavy.sh uses ($STEER_HEAVY_LOCKS, default /tmp/hdl_heavy.lock, .2, .3; mkdir wins), so the
# queue and direct heavy.sh jobs share one limit. The job then drops its ticket, writes an owner file
# ("<who> pid <pid> <HH:MM:SS>: <kind>") into the lane and runs the command in the current directory with the --env
# values and HEAVY_LANE_HELD=1 (lib/heavy.sh inside it takes no second lane), memory-capped at --mem with no swap and
# a wall-clock --timeout (default 3600 s). Every job runs on this machine: --desktop-only is accepted and recorded.
#
# Output (stderr): "[queue] <ticket> waiting ..." when queued, "[queue] <ticket> dispatched to <lane> (<who>)" when the
# job starts. Callers parse the ticket id (q<digits>) and the word "dispatched".
# Exit codes: the job's own; 2 usage; 3 the ticket was cancelled; 129/130/143 interrupted while waiting.
set -u
QDIR=${HEAVY_QUEUE_DIR:-/tmp/hdl-queue}
POLL=${QUEUE_POLL_S:-5}
IFS=, read -r -a LANES <<< "${STEER_HEAVY_LOCKS:-/tmp/hdl_heavy.lock,/tmp/hdl_heavy.lock.2,/tmp/hdl_heavy.lock.3}"
say() { echo "[queue] $*" >&2; }
usage() { sed -n '6,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

WHO=${USER:-job}; KIND=gowin; MEM=4G; T=3600; DESK=0; ENVS=(); CANCEL=""; STATUS=0
while [ $# -gt 0 ]; do
    case "$1" in
        --who) WHO="$2"; shift 2 ;;
        --kind) KIND="$2"; shift 2 ;;
        --mem) MEM="$2"; shift 2 ;;
        --timeout) T="$2"; shift 2 ;;
        --desktop-only) DESK=1; shift ;;
        --env) ENVS+=("$2"); shift 2 ;;
        --cancel) CANCEL="$2"; shift 2 ;;
        --status) STATUS=1; shift ;;
        --) shift; break ;;
        *) usage ;;
    esac
done
mkdir -p "$QDIR"

if [ "$STATUS" = 1 ]; then
    for f in "$QDIR"/t-*; do [ -e "$f" ] && echo "waiting $(basename "$f"): $(cat "$f")"; done
    for l in "${LANES[@]}"; do
        [ -d "$l" ] && echo "lane $l: $(cat "$l/owner" 2>/dev/null || echo '(no owner file)')"
    done
    exit 0
fi
if [ -n "$CANCEL" ]; then
    [[ "$CANCEL" =~ ^q[0-9]+$ ]] || usage
    rm -f "$QDIR"/t-*-"$CANCEL"
    say "$CANCEL cancelled by $WHO"
    exit 0
fi
[ $# -gt 0 ] || usage
[[ "$T" =~ ^[0-9]+$ ]] || usage

ID="q$$"
TICKET="$QDIR/t-$(date +%s%N)-$ID"
LANE=""
cleanup() {
    rm -f "$TICKET"
    if [ -n "$LANE" ]; then rm -f "$LANE/owner"; rmdir "$LANE" 2>/dev/null; fi
}
CH=""
stop() { [ -n "$CH" ] && kill -TERM "$CH" 2>/dev/null; exit "$1"; }
trap cleanup EXIT
trap 'stop 143' TERM
trap 'stop 130' INT
trap 'stop 129' HUP
echo "$WHO pid $$ kind $KIND$([ "$DESK" = 1 ] && echo ' desktop-only')" > "$TICKET"
say "$ID waiting (who $WHO, kind $KIND)"

oldest_live() {
    # the oldest ticket whose process still runs; stale tickets (process gone) are removed
    local f pid
    for f in $(ls "$QDIR" 2>/dev/null | grep '^t-' | sort); do
        pid=${f##*-q}
        if kill -0 "$pid" 2>/dev/null; then echo "$f"; return; fi
        rm -f "$QDIR/$f"
    done
}
while :; do
    [ -e "$TICKET" ] || { say "$ID: ticket cancelled"; exit 3; }
    if [ "$(oldest_live)" = "$(basename "$TICKET")" ]; then
        for l in "${LANES[@]}"; do
            if mkdir "$l" 2>/dev/null; then LANE="$l"; break; fi
        done
        [ -n "$LANE" ] && break
    fi
    sleep "$POLL"
done
rm -f "$TICKET"
echo "$WHO pid $$ $(date +%H:%M:%S): $KIND" > "$LANE/owner"
say "$ID dispatched to $LANE ($WHO)"
systemd-run --user --scope -q -p MemoryMax="$MEM" -p MemorySwapMax=0 \
    timeout -k 10 "$T" env HEAVY_LANE_HELD=1 "${ENVS[@]}" "$@" &
CH=$!
wait $CH
rc=$?
[ $rc -eq 137 ] && echo "queue_run.sh: job killed (exit 137: memory cap or timeout kill)" >&2
exit $rc
