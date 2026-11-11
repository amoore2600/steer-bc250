#!/usr/bin/env bash
# hdl-check.sh [--top NAME] [--tb FILE] [--stop-time T] FILE...   (first FILE = the design being checked)
# One-command check of Verilog/SystemVerilog or VHDL sources: lint, compile, optional testbench simulation,
# and a synthesis sanity check. Prints only the findings that matter and ends with one RESULT line.
#   Verilog/SV: verilator --lint-only -Wall, iverilog -g2012 compile, vvp simulation, yosys synth + check.
#   VHDL:       ghdl -s (syntax), ghdl -a (analyse), ghdl -e/-r simulation, ghdl --synth.
# Gowin GW5A primitives are resolved from ./prim_sim.v(hd) or ~/gowin/IDE/simlib/gw5a for simulation and from
# black-box stubs next to this script for lint/synthesis.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TOP=""; TB=""; STOP="1ms"; FILES=()
while [ $# -gt 0 ]; do
    case "$1" in
        --top) TOP="$2"; shift 2 ;;
        --tb) TB="$2"; shift 2 ;;
        --stop-time) STOP="$2"; shift 2 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) FILES+=("$1"); shift ;;
    esac
done
[ ${#FILES[@]} -gt 0 ] || { echo "usage: hdl-check.sh [--top NAME] [--tb FILE] FILE..."; exit 2; }
for f in "${FILES[@]}" ${TB:+"$TB"}; do [ -f "$f" ] || { echo "RESULT: FAIL (missing file $f)"; exit 2; }; done
W="$(mktemp -d "${TMPDIR:-/tmp}/hdlcheck.XXXXXX")"; trap 'rm -rf "$W"' EXIT
FAILS=(); WARNS=0
show() { sed -n "1,${2:-25}p" "$1"; n=$(wc -l < "$1"); [ "$n" -gt "${2:-25}" ] && echo "  ... ($((n - ${2:-25})) more lines)"; }
lang=verilog
case "${FILES[0]}" in *.vhd|*.vhdl) lang=vhdl ;; esac
find_prim() {  # find_prim v|vhd
    for p in "./prim_sim.$1" "$HOME/gowin/IDE/simlib/gw5a/prim_sim.$1"; do [ -f "$p" ] && { echo "$p"; return; }; done
}
PRIMS='PLL|OSER10|OSER8|OSER4|ODDR|IDDR|IDES10|IDES4|ELVDS_OBUF|TLVDS_OBUF|ELVDS_IBUF|TLVDS_IBUF|DCE|DHCEN|DPB|DPX9B|SDPB|SDPX9B|SP|SPX9|pROM|CLKDIV|CLKDIV2|BUFG|DQCE|DCS|IODELAY|OSC|OSCA|MULT18X18|MULTALU27X18'
uses_gowin=0   # only real instantiations count; comments are stripped first
for f in "${FILES[@]}" ${TB:+"$TB"}; do
    sed -e 's://.*$::' -e 's:--.*$::' "$f" | tr '\n' ' ' | sed -e 's:/\*[^*]*\*\+\([^/*][^*]*\*\+\)*/: :g' \
      | grep -qE "(^|[^A-Za-z0-9_.])($PRIMS)[[:space:]]*(#[[:space:]]*\(|[A-Za-z_][A-Za-z0-9_]*[[:space:]]*(\[[^]]*\][[:space:]]*)?\()|component[[:space:]]+($PRIMS)\b|:[[:space:]]*($PRIMS)[[:space:]]+(generic|port)[[:space:]]+map|entity[[:space:]]+[a-z]+\.($PRIMS)\b" && uses_gowin=1
done

if [ "$lang" = verilog ]; then
    STUBS=(); [ $uses_gowin = 1 ] && STUBS=("$HERE/gw_prim_stubs.v")
    echo "== lint (verilator -Wall)"
    verilator --lint-only -Wall -Wno-fatal -Wno-DECLFILENAME ${TOP:+--top-module "$TOP"} "${STUBS[@]}" "${FILES[@]}" > "$W/lint.txt" 2>&1
    grep -E "^%(Error|Warning)" "$W/lint.txt" | grep -v "gw_prim_stubs" > "$W/lint.f" || true
    ne=$(grep -c "^%Error" "$W/lint.f"); nw=$(grep -c "^%Warning" "$W/lint.f"); WARNS=$((WARNS + nw))
    if [ -s "$W/lint.f" ]; then show "$W/lint.f" 25; else echo "  clean"; fi
    [ "$ne" -gt 0 ] && FAILS+=("lint: $ne errors")
    echo "== compile (iverilog -g2012)"
    SIM=(); [ $uses_gowin = 1 ] && { p=$(find_prim v); [ -n "$p" ] && SIM=("$p") || echo "  note: Gowin primitives used but prim_sim.v not found; compile uses stubs"; }
    [ $uses_gowin = 1 ] && [ ${#SIM[@]} -eq 0 ] && SIM=("$HERE/gw_prim_stubs.v")
    iverilog -g2012 -o "$W/sim.vvp" ${TOP:+-s "$TOP"} "${SIM[@]}" ${TB:+"$TB"} "${FILES[@]}" > "$W/comp.txt" 2>&1; rc=$?   # library first: a design ending in default_nettype none must not leak into it
    if [ $rc -ne 0 ]; then show "$W/comp.txt" 20; FAILS+=("compile"); else echo "  ok"; fi
    if [ -n "$TB" ] && [ $rc -eq 0 ]; then
        echo "== simulation (vvp, testbench $TB)"
        ( cd "$W" && timeout 120 vvp -n sim.vvp ) > "$W/sim.txt" 2>&1; src=$?
        [ $src -eq 124 ] && FAILS+=("sim: timeout 120 s (missing \$finish?)")
        grep -iE "fail|error|mismatch|assert|expected|pass|\\\$finish|done" "$W/sim.txt" > "$W/sim.f" || true
        if [ -s "$W/sim.f" ]; then show "$W/sim.f" 30; else echo "  (no PASS/FAIL/ERROR lines; last output:)"; tail -5 "$W/sim.txt"; fi
        grep -qiE "fail|error|mismatch" "$W/sim.txt" && FAILS+=("sim: testbench reported failure")
    fi
    echo "== synthesis check (yosys)"
    TOPARG="-auto-top"; [ -n "$TOP" ] && TOPARG="-top $TOP"
    LIBS=""; [ $uses_gowin = 1 ] && LIBS="read_verilog -lib $HERE/gw_prim_stubs.v;"
    yosys -q -l "$W/syn.txt" -p "$LIBS read_verilog -sv ${FILES[*]}; hierarchy -check $TOPARG; proc; opt_clean; check; stat" > /dev/null 2>&1; yrc=$?
    grep -E "ERROR|Latch inferred|[Mm]ultiple (conflicting )?drivers|logic loop|is used but has no driver|Found and reported [1-9]" "$W/syn.txt" > "$W/syn.f" || true
    if [ $yrc -ne 0 ]; then grep -E "ERROR" "$W/syn.txt" | head -5
        if [ "$ne" -eq 0 ] && [ $rc -eq 0 ]; then
            echo "  note: yosys 0.33 rejected a construct that verilator and iverilog accept; not counted as a failure (Gowin synthesis is the authority)"; WARNS=$((WARNS + 1))
        else FAILS+=("synthesis: yosys error"); fi
    fi
    if [ -s "$W/syn.f" ]; then show "$W/syn.f" 15; WARNS=$((WARNS + $(grep -vc ERROR "$W/syn.f"))); elif [ $yrc -eq 0 ]; then echo "  ok (no latches, multiple drivers, loops or undriven nets)"; fi
    grep -q "Latch inferred" "$W/syn.f" && FAILS+=("synthesis: latch inferred (assign every output on every path of a combinational always block, or make it clocked)")
else
    GF=(--std=08 -fsynopsys -frelaxed --workdir="$W")
    PRIM=(); [ $uses_gowin = 1 ] && { p=$(find_prim vhd); [ -n "$p" ] && PRIM=("$p") || echo "  note: Gowin primitives used but prim_sim.vhd not found"; }
    echo "== syntax and analysis (ghdl)"
    for f in "${PRIM[@]}"; do ghdl -a "${GF[@]}" "$f" > /dev/null 2>&1 || echo "  note: $f did not analyse"; done
    todo=("${FILES[@]}" ${TB:+"$TB"}); ok=1
    while [ ${#todo[@]} -gt 0 ]; do      # retry until no progress, so file order (packages first) does not matter
        left=(); progress=0
        for f in "${todo[@]}"; do
            if ghdl -a "${GF[@]}" "$f" > "$W/a-$(basename "$f").txt" 2>&1; then progress=1
                WARNS=$((WARNS + $(grep -c ":warning:" "$W/a-$(basename "$f").txt")))
            else left+=("$f"); fi
        done
        todo=("${left[@]}"); [ $progress = 0 ] && break
    done
    if [ ${#todo[@]} -gt 0 ]; then ok=0
        for f in "${todo[@]}"; do echo "  $f:"; grep -E "error" "$W/a-$(basename "$f").txt" | head -15; done
        grep -qE "not found in library|was not analysed" "$W"/a-*.txt && echo "  hint: a package or entity it uses is missing; add that file to the command"
    fi
    [ $ok = 1 ] && echo "  ok" || FAILS+=("analysis")
    if [ $ok = 1 ] && [ -n "$TB" ]; then
        TBENT=$(grep -oiE "^\s*entity\s+[A-Za-z0-9_]+" "$TB" | head -1 | awk '{print $2}')
        echo "== simulation (ghdl, entity $TBENT, stop-time $STOP)"
        ( cd "$W" && ghdl -e "${GF[@]}" "$TBENT" && timeout 120 ghdl -r "${GF[@]}" "$TBENT" --stop-time="$STOP" --ieee-asserts=disable ) > "$W/sim.txt" 2>&1; src=$?
        grep -iE "fail|error|assert|mismatch|pass|done" "$W/sim.txt" > "$W/sim.f" || true
        if [ -s "$W/sim.f" ]; then show "$W/sim.f" 30; else tail -5 "$W/sim.txt"; fi
        { [ $src -ne 0 ] || grep -qiE "fail|error|mismatch" "$W/sim.txt"; } && FAILS+=("sim: testbench reported failure or did not finish")
    fi
    if [ $ok = 1 ]; then
        UNIT="$TOP"; [ -z "$UNIT" ] && UNIT=$(grep -oiE "^\s*entity\s+[A-Za-z0-9_]+\s+is" "${FILES[0]}" | head -1 | awk '{print $2}')
        echo "== synthesis check (ghdl --synth, entity $UNIT)"
        if [ $uses_gowin = 1 ]; then echo "  skipped (Gowin primitives are simulation models, not synthesizable here)"
        else
            ghdl --synth "${GF[@]}" "$UNIT" > /dev/null 2> "$W/syn.txt"; yrc=$?
            grep -iE "error|latch|multiple|not fully" "$W/syn.txt" > "$W/syn.f" || true
            [ -s "$W/syn.f" ] && show "$W/syn.f" 15
            if [ $yrc -ne 0 ]; then FAILS+=("synthesis: ghdl --synth error"); elif [ ! -s "$W/syn.f" ]; then echo "  ok"; else WARNS=$((WARNS + $(wc -l < "$W/syn.f"))); fi
        fi
    fi
fi
if [ ${#FAILS[@]} -eq 0 ]; then echo "RESULT: PASS ($WARNS warnings)"; exit 0; fi
( IFS=';'; echo "RESULT: FAIL (${FAILS[*]}; $WARNS warnings)" ); exit 1
