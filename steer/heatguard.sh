#!/bin/bash
# heatguard.sh <run log> <run pgid> <results dir> -- staged thermal protection for the inference boards of a run.
#   Hot (GPU edge >= HOT_EDGE (79) C or Tctl >= HOT_TCTL (92) C): lower that board's GPU cap one step (its STEPS list),
#     then wait 2 min before the next step.
#   Dangerous, 3 readings in a row (edge >= DANGER_EDGE (85) / Tctl >= DANGER_TCTL (97), or at the lowest step and still
#     edge >= FLOOR_EDGE (82) / Tctl >= FLOOR_TCTL (95)): stop the run (its process group), stop every worker process
#     group it started (workers.jsonl), unload UNLOAD_MODEL on every board URL, exit.
#   Ends when the run log ends with "done", or when the run's process group no longer exists (checked every loop).
# A guard started inside the run's process group (e.g. `run & heatguard.sh ... &` from a non-job-control shell) moves to
# a session of its own first, so it never keeps that group alive itself.
#
# Configuration: the bash file $HEATGUARD_CONF (default steer/heatguard.conf; start from heatguard.conf.example). It
# sets BOARDS (names), STEPS[<board>] (GPU cap steps in MHz, highest first), URL[<board>] (the llama.cpp server),
# UNLOAD_MODEL, and the functions read_temps <board> (prints one CSV line time,tctl,edge,...,cap with the cap in
# field 9) and set_cap <board> <mhz>. Without a configuration the guard refuses to start (exit 2).
# Test seams: HEATGUARD_LOG, HEATGUARD_INTERVAL (s), HEATGUARD_BOARDS (overrides BOARDS), HEATGUARD_CSV_DIR (read
# <dir>/<board>.csv instead of calling read_temps).
L=$1; PG=$(echo "$2" | tr -d '[:space:]'); RD=$3
if ! [[ "$PG" =~ ^[0-9]+$ ]] || [ "$PG" -le 1 ]; then
  echo "usage: heatguard.sh <run log> <run pgid> <results dir> (the pgid must be a number > 1, got '$2')" >&2; exit 2
fi
CONF=${HEATGUARD_CONF:-$(dirname "$(readlink -f "$0")")/heatguard.conf}
if [ ! -r "$CONF" ]; then
  echo "heatguard.sh: no configuration at $CONF (copy steer/heatguard.conf.example and edit it, or set HEATGUARD_CONF)" >&2
  exit 2
fi
if [ -z "$HEATGUARD_DETACHED" ] && [ "$(ps -o pgid= -p $$ | tr -d '[:space:]')" = "$PG" ]; then
  HEATGUARD_DETACHED=1 exec setsid bash "$0" "$@"
fi
declare -A STEPS URL LAST HOT3
BOARDS=""; UNLOAD_MODEL=""
read_temps() { return 1; }
set_cap() { return 1; }
# shellcheck source=/dev/null
. "$CONF"
BOARDS=${HEATGUARD_BOARDS:-$BOARDS}
HOT_EDGE=${HOT_EDGE:-79}; HOT_TCTL=${HOT_TCTL:-92}; DANGER_EDGE=${DANGER_EDGE:-85}; DANGER_TCTL=${DANGER_TCTL:-97}
FLOOR_EDGE=${FLOOR_EDGE:-82}; FLOOR_TCTL=${FLOOR_TCTL:-95}
LOG=${HEATGUARD_LOG:-$HOME/.local/state/heatguard.log}; mkdir -p "$(dirname "$LOG")"
INTERVAL=${HEATGUARD_INTERVAL:-10}
for b in $BOARDS; do LAST[$b]=0; HOT3[$b]=0; done
say() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
group_alive() { pgrep -g "$PG" >/dev/null 2>&1 || kill -0 -- -"$PG" 2>/dev/null; }
reading() {
  if [ -n "$HEATGUARD_CSV_DIR" ]; then tail -1 "$HEATGUARD_CSV_DIR/$1.csv" 2>/dev/null
  else read_temps "$1" 2>/dev/null; fi
}
worker_groups() {
  python3 -c "import json,sys; print(' '.join(sorted({str(json.loads(l)['pgid']) for l in open(sys.argv[1]) if 'pgid' in l})))" \
    "$RD/workers.jsonl" 2>/dev/null
}
say "guard start: run $L, run pgid $PG, boards: ${BOARDS:-none}"
while :; do
  group_alive || { say "run process group $PG is gone; guard off"; exit 0; }
  for b in $BOARDS; do
    l=$(reading "$b") || continue
    t=$(echo "$l" | cut -d, -f2); e=$(echo "$l" | cut -d, -f3); cap=$(echo "$l" | cut -d, -f9)
    [ -n "$t" ] && [ -n "$e" ] && [ -n "$cap" ] || continue
    steps=${STEPS[$b]:-}; floor=${steps##* }; floor=${floor:-0}
    if [ "$e" -ge "$DANGER_EDGE" ] || [ "$t" -ge "$DANGER_TCTL" ] || \
       { [ "$cap" -le "$floor" ] && { [ "$e" -ge "$FLOOR_EDGE" ] || [ "$t" -ge "$FLOOR_TCTL" ]; }; }; then
      HOT3[$b]=$(( ${HOT3[$b]} + 1 )); else HOT3[$b]=0; fi
    if [ "${HOT3[$b]}" -ge 3 ]; then
      # the run's exit handler stops its guard with SIGTERM: finish the stop sequence first
      trap '' TERM HUP INT
      say "DANGER $b ($l): stopping the run"
      kill -TERM -- -"$PG" 2>/dev/null
      for wp in $(worker_groups); do kill -TERM -- -"$wp" 2>/dev/null; done
      sleep 5; kill -KILL -- -"$PG" 2>/dev/null
      for wp in $(worker_groups); do kill -KILL -- -"$wp" 2>/dev/null; done
      if [ -n "$UNLOAD_MODEL" ]; then
        for u in "${URL[@]}"; do
          curl -s -m 30 -X POST "$u/models/unload" -H 'Content-Type: application/json' \
            -d "{\"model\":\"$UNLOAD_MODEL\"}" >> "$LOG"
        done
      fi
      echo "DANGER on $b: run STOPPED, models unloaded: $l"; exit 0
    fi
    if { [ "$e" -ge "$HOT_EDGE" ] || [ "$t" -ge "$HOT_TCTL" ]; } && [ $(( $(date +%s) - ${LAST[$b]} )) -ge 120 ]; then
      next=""; for s in $steps; do [ "$s" -lt "$cap" ] && { next=$s; break; }; done
      if [ -n "$next" ]; then set_cap "$b" "$next" && say "STEP $b: cap $cap -> $next ($l)"
      else say "AT FLOOR $b: cap $cap, still hot ($l)"; fi
      LAST[$b]=$(date +%s)
    fi
  done
  [ "$(tail -1 "$L" 2>/dev/null)" = "done" ] && { say "run finished; guard off"; echo "run finished; guard off"; exit 0; }
  sleep "$INTERVAL"
done
