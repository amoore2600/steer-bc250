#!/usr/bin/env python3
"""prim_stubs.py <out.v> <module>... -- port/parameter-only stubs of Gowin GW5A primitives from prim_sim.v,
for Verilator lint (Verilator cannot parse prim_sim.v itself)."""
import os, re, sys
src = open(os.path.expanduser("~/gowin/IDE/simlib/gw5a/prim_sim.v"), errors="replace").read()
out = ["// Port/parameter stubs of Gowin GW5A primitives, for lint only.", "/* verilator lint_off UNUSEDSIGNAL */",
       "/* verilator lint_off UNUSEDPARAM */", "/* verilator lint_off UNDRIVEN */", "/* verilator lint_off DECLFILENAME */"]
for m in sys.argv[2:]:
    mm = re.search(r"^module\s+%s\s*\(.*?^endmodule" % re.escape(m), src, re.S | re.M)
    body = mm.group(0)
    hdr = body[:body.index(";") + 1]
    decls = []
    for st in re.findall(r"^\s*((?:input|output|inout|parameter)\b[^;]*;)", body, re.M):
        st = re.sub(r"\s+", " ", st)
        st = re.sub(r"^output reg\b", "output", st)
        decls.append("    " + st)
    out += ["(* blackbox *)", hdr] + decls + ["endmodule", ""]
open(sys.argv[1], "w").write("\n".join(out) + "\n")
