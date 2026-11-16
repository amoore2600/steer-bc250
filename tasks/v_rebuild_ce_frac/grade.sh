#!/usr/bin/env bash
# grade.sh <candidate file>: exit 0 = PASS; last line = JSON summary
exec python3 "${STEER_BC250_BASE:-$(dirname "$(readlink -f "$0")")/../..}/lib/grade.py" "$(dirname "$(readlink -f "$0")")" "$1" "${@:2}"
