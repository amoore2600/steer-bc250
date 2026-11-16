#!/bin/bash
# kat_check.sh -- lint, compile and run the known-answer checker tb_debounce.v against debounce.v. Do not edit.
cd "$(dirname "$0")" || exit 2
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
echo "== lint (verilator -Wall)"
verilator --lint-only -Wall -Wno-fatal -Wno-DECLFILENAME --top-module debounce debounce.v > "$W/lint.txt" 2>&1
grep -E "^%(Error|Warning)" "$W/lint.txt" | head -15
NE=$(grep -c "^%Error" "$W/lint.txt")
echo "== compile (iverilog -g2012)"
if ! iverilog -g2012 -o "$W/sim" -s tb_debounce tb_debounce.v debounce.v  > "$W/comp.txt" 2>&1; then
    head -20 "$W/comp.txt"; echo "RESULT: FAIL (compile)"; exit 1
fi
echo "== known-answer checker (tb_debounce.v)"
timeout 300 vvp -n "$W/sim" > "$W/run.txt" 2>&1
python3 - "$W/run.txt" <<'PYEOF'
# the first failing lines in full, then one count per kind of failure (the text in [...] or before the first ':')
import re, sys, collections
lines = [l.rstrip() for l in open(sys.argv[1], errors="replace")]
bad = [l for l in lines if re.search(r"FAIL|ERROR|MISMATCH", l) and not l.startswith("KAT ")]
for l in bad[:4]:
    print(l[:300] if l.startswith("FAIL") else "FAIL: " + l[:300])
kinds = collections.OrderedDict()
for l in bad:
    m = re.search(r"\[([^\]]+)\]", l)
    k = m.group(1) if m else re.sub(r"\d+", "N", l.split(":")[0])[:60]
    kinds[k] = kinds.get(k, 0) + 1
for k, n in list(kinds.items())[:8]:
    print("FAIL:   %d line(s): %s" % (n, k))
if len(kinds) > 8:
    print("FAIL:   (%d more kinds)" % (len(kinds) - 8))
for l in lines:
    if l.startswith("KAT "):
        print(l)
PYEOF
if grep -qE '^KAT debounce: PASS' "$W/run.txt" && [ "$NE" = 0 ]; then echo "RESULT: PASS"; exit 0; fi
N=$(grep -oE 'FAIL \(([0-9]+) (errors|mismatches)\)' "$W/run.txt" | head -1 | grep -oE '[0-9]+' | head -1)
[ -z "$N" ] && N=$(grep -cE "FAIL|ERROR|MISMATCH" "$W/run.txt")
[ "$N" = 0 ] && N=1
echo "FAIL: $N mismatches"
if [ "$NE" != 0 ]; then echo "RESULT: FAIL (lint: $NE errors)"; else echo "RESULT: FAIL (checker)"; fi
exit 1
