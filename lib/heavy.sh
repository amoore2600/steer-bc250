#!/usr/bin/env bash
# heavy.sh <timeout_s> <cmd...> -- run a heavy job (gw_sh, Verilator build) in one of the shared heavy lanes
# (/tmp/hdl_heavy.lock, .2, .3: mkdir wins), memory-capped at 4G with no swap, with a wall-clock timeout.
# The lane is released on any exit.  Waits up to HEAVY_WAIT_S (default 7200 s) for a free lane.
set -u
T="$1"; shift
LANE=""
cleanup() { [ -n "$LANE" ] && rmdir "$LANE" 2>/dev/null; }
trap cleanup EXIT
trap 'exit 130' INT TERM HUP
waited=0
while :; do
    for l in /tmp/hdl_heavy.lock /tmp/hdl_heavy.lock.2 /tmp/hdl_heavy.lock.3; do
        if mkdir "$l" 2>/dev/null; then LANE="$l"; break; fi
    done
    [ -n "$LANE" ] && break
    if [ "$waited" -ge "${HEAVY_WAIT_S:-7200}" ]; then echo "heavy.sh: no free lane after ${waited}s" >&2; exit 75; fi
    sleep 5; waited=$((waited + 5))
done
systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0 timeout -k 10 "$T" "$@"
rc=$?
[ $rc -eq 137 ] && echo "heavy.sh: job killed (exit 137: memory cap or timeout kill)" >&2
exit $rc
