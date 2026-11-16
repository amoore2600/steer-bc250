#!/usr/bin/env bash
# gowin_syn.sh <outdir> <top> <file>... -- GowinSynthesis only (run syn, no place-and-route) for GW5AST-LV138PG484AC1/I0
# device version B (GOWIN_PART and GOWIN_DEVICE_VERSION override them).  Runs in a heavy lane.
# GOWIN_HOME is the Gowin IDE directory (default ~/gowin/IDE); GOWIN_SH overrides the gw_sh binary (tests use a fake).
# Prints one JSON line: {"syn_pass":bool,"syn_rc":int,"syn_warnings":int,"syn_errors":int,"ex2478":int,"ex3988":int,"syn_detail":str,
#                        "resources":{"dsp":int,"bsram":int,"prims":{name:int}}|null}
# resources: the top module's totals (T_<name> attributes, "total(own)") of GowinSynthesis's *_syn_rsc.xml; dsp is the
# sum of the DSP primitives (MULT*, ALU54*, PADD*), prims every non-logic primitive by name. null without that file.
set -u
OUT="$1"; TOP="$2"; shift 2
LIB="$(cd "$(dirname "$0")" && pwd)"
rm -rf "$OUT"; mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
{
    echo "create_project -name syn -dir $OUT -pn ${GOWIN_PART:-GW5AST-LV138PG484AC1/I0} -device_version ${GOWIN_DEVICE_VERSION:-B} -force"
    for f in "$@"; do echo "add_file $(readlink -f "$f")"; done
    echo "set_option -top_module $TOP"
    echo "set_option -verilog_std sysv2017"
    echo "set_option -vhdl_std vhd2008"
    echo "set_option -looplimit 4096"
    echo "run syn"
} > "$OUT/syn.tcl"
cd "$OUT"
GH=${GOWIN_HOME:-$HOME/gowin/IDE}
LD_LIBRARY_PATH="$GH/lib" LD_PRELOAD=/lib/x86_64-linux-gnu/libfreetype.so.6 QT_QPA_PLATFORM=offscreen \
    "$LIB/heavy.sh" "${SYN_TIMEOUT:-900}" "${GOWIN_SH:-$GH/bin/gw_sh}" "$OUT/syn.tcl" > "$OUT/gw_sh.log" 2>&1
RC=$?
python3 - "$OUT" "$RC" <<'PY'
import json, re, sys, glob, os
out, rc = sys.argv[1], int(sys.argv[2])
txt = open(os.path.join(out, "gw_sh.log"), errors="replace").read()
for f in glob.glob(os.path.join(out, "syn", "impl", "gwsynthesis", "*.log")):
    txt += "\n" + open(f, errors="replace").read()
warn = sorted(set(re.findall(r"^WARN.*$", txt, re.M)))
err = sorted(set(re.findall(r"^ERROR.*$", txt, re.M)))
netlist = glob.glob(os.path.join(out, "syn", "impl", "gwsynthesis", "*.vg"))
ok = rc == 0 and not err and bool(netlist)
det = "ok" if ok else ("rc=%d " % rc + (err[0] if err else ("no netlist" if not netlist else "")))[:300]
rsc = None
for f in sorted(glob.glob(os.path.join(out, "syn", "impl", "gwsynthesis", "*_syn_rsc.xml"))):
    m = re.search(r"<Module\b([^>]*)>", open(f, errors="replace").read())
    if m:
        tot = {k: int(v) for k, v in re.findall(r'\bT_(\w+)="(\d+)', m.group(1))}
        prims = {k: v for k, v in tot.items() if k not in ("Register", "Alu", "Lut")}
        rsc = {"dsp": sum(v for k, v in prims.items() if re.match(r"(MULT|ALU54|PADD)", k)),
               "bsram": prims.get("Bsram", 0), "prims": prims}
    break
print(json.dumps({"syn_pass": ok, "syn_rc": rc, "syn_warnings": len(warn), "syn_errors": len(err),
                  "ex2478": sum("EX2478" in w for w in warn), "ex3988": sum("EX3988" in l for l in warn + err),
                  "syn_detail": det, "resources": rsc}))
PY
