#!/usr/bin/env bash
# light.sh <timeout_s> <cmd...> -- run a light job (iverilog/GHDL sim) in a 4G memory-capped scope with a wall-clock timeout.
# Exit codes: the command's own; 124 = timeout; 137 = killed (memory cap or timeout -k).
set -u
T="$1"; shift
exec systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0 timeout -k 10 "$T" "$@"
