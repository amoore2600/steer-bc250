#!/usr/bin/env python3
"""gen_vtb.py <taskdir> -- build the Verilog equivalence harness of a task from its task.json "equiv" section.

Writes <taskdir>/golden_renamed.v (every module of the golden files suffixed _golden) and <taskdir>/tb_equiv.v.
The testbench instantiates <top>_golden and <top> with the same parameters, drives the inputs from the task's
stimulus snippets (one always block per clock, on that clock's falling edge, nonblocking), and compares every
output at every STEP ns (offset by half a step, never on a clock edge).  A golden bit that is X/Z is not checked.
Prints EQUIV_PASS / EQUIV_FAIL lines.
"""
import json, os, re, subprocess, sys, tempfile
import xml.etree.ElementTree as ET


def ports_of(files, top, params, incdirs):
    with tempfile.TemporaryDirectory() as d:
        x = os.path.join(d, "p.xml")
        cmd = ["verilator", "--xml-only", "--xml-output", x, "-Wno-fatal", "-Wno-lint", "-Wno-style",
               "--top-module", top] + ["-G%s=%s" % (k, v) for k, v in params.items()] + \
              ["-I" + i for i in incdirs] + files
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        r = ET.parse(x).getroot()
    types = {d.get("id"): d for d in r.iter() if d.get("id") and d.tag.endswith("dtype")}
    for m in r.iter("module"):
        if m.get("name") != top and m.get("origName") != top:
            continue
        out = []
        for v in m.findall("var"):
            dr = v.get("dir")
            if not dr:
                continue
            dt = types.get(v.get("dtype_id"))
            if dt is None or dt.tag != "basicdtype":
                raise SystemExit("unsupported port type for %s" % v.get("name"))
            l, rr = dt.get("left"), dt.get("right")
            w = 1 if l is None else abs(int(l) - int(rr)) + 1
            out.append((v.get("name"), dr, w))
        return out
    raise SystemExit("top %s not found" % top)


def rename_golden(files, dst):
    text = ""
    for f in files:
        text += open(f).read() + "\n`default_nettype wire\n"
    names = re.findall(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)", text, re.M)
    for n in sorted(set(names), key=len, reverse=True):
        text = re.sub(r"\b%s\b" % re.escape(n), n + "_golden", text)
    # simulation-only contract blocks of the golden refer to the golden's own names: unaffected
    open(dst, "w").write(text)
    return names


def main():
    td = os.path.abspath(sys.argv[1])
    cfg = json.load(open(os.path.join(td, "task.json")))
    eq = cfg["equiv"]
    top = cfg["top"]
    gfiles = [os.path.join(td, "golden", f) for f in cfg["golden_files"]]
    ctx = [os.path.join(td, f) for f in cfg.get("context_files", [])]
    params = eq.get("params", {})
    lint_extra = [os.path.expanduser(f) for f in eq.get("port_files_extra", [])]
    ports = ports_of(gfiles + ctx + lint_extra, top, params, [])
    rename_golden(gfiles, os.path.join(td, "golden_renamed.v"))
    clocks = eq["clocks"]            # [{"name":..., "half": ns, "phase": ns}]
    cnames = {c["name"] for c in clocks}
    step = eq.get("step", 1.0)
    p = []
    p.append("`timescale 1ns/10ps")
    p.append("`default_nettype wire")
    p.append("module tb_equiv;")
    p.append("    integer seed, seed0, errs, checks, known, cyc, cycles;")
    for n, d, w in ports:
        rng = "" if w == 1 else "[%d:0] " % (w - 1)
        if d == "input":
            p.append("    reg  %s%s = 0;" % (rng, n))
        else:
            p.append("    wire %sg_%s, c_%s;" % (rng, n, n))
    pm = ""
    if params:
        pm = " #(" + ", ".join(".%s(%s)" % (k, v) for k, v in params.items()) + ")"
    for inst, pre in (("g", "g_"), ("c", "c_")):
        conns = []
        for n, d, w in ports:
            conns.append(".%s(%s)" % (n, n if d == "input" else pre + n))
        mod = top + "_golden" if inst == "g" else top
        p.append("    %s%s u_%s (%s);" % (mod, pm, inst, ", ".join(conns)))
    p.append("    function [31:0] r32(input integer dummy); begin r32 = $random(seed); end endfunction")
    p.append("    function [63:0] r64(input integer dummy); begin r64 = {$random(seed), $random(seed)}; end endfunction")
    p.append("    // chance(n): true with probability n/256")
    p.append("    function chance(input integer n); reg [7:0] v; begin v = $random(seed); chance = (v < n); end endfunction")
    p.append(eq.get("decls", ""))
    for c in clocks:
        p.append("    initial begin #(%s); forever #(%s) %s = ~%s; end" % (c.get("phase", 0), c["half"], c["name"], c["name"]))
    main_clk = clocks[0]["name"]
    p.append("    always @(posedge %s) cyc <= cyc + 1;" % main_clk)
    for c in clocks:
        code = eq["stim"].get(c["name"], "")
        if code:
            p.append("    always @(negedge %s) begin\n%s\n    end" % (c["name"], code))
    p.append("    integer kn_t; reg bad_t;")
    p.append("    task automatic cmp_bits_(input [1023:0] g, input [1023:0] c, input integer w, output reg bad, output integer kn);")
    p.append("        integer i;")
    p.append("        begin bad = 0; kn = 0;")
    p.append("            if (g === c) begin if (^g[1023:0] !== 1'bx) kn = w; end")
    p.append("            else for (i = 0; i < w; i = i + 1) if (g[i] === 1'b0 || g[i] === 1'b1) begin")
    p.append("                kn = kn + 1; if (c[i] !== g[i]) bad = 1; end")
    p.append("        end")
    p.append("    endtask")
    p.append("    task check; begin checks = checks + 1;")
    for n, d, w in ports:
        if d == "input":
            continue
        p.append("        cmp_bits_(g_%s, c_%s, %d, bad_t, kn_t); known = known + kn_t;" % (n, n, w))
        p.append("        if (bad_t) begin errs = errs + 1; if (errs <= 5)")
        p.append("            $display(\"MISMATCH t=%%0t cyc=%%0d port=%s golden=%%h candidate=%%h\", $time, cyc, g_%s, c_%s); end" % (n, n, n))
    p.append("    end endtask")
    p.append("    initial begin")
    p.append("        errs = 0; checks = 0; known = 0; cyc = 0;")
    p.append("        if (!$value$plusargs(\"seed=%d\", seed)) seed = 1;")
    p.append("        seed0 = seed;")
    p.append("        if (!$value$plusargs(\"cycles=%%d\", cycles)) cycles = %d;" % eq.get("cycles", 4000))
    p.append(eq.get("init", ""))
    p.append("        #(%s);" % (step / 2.0))
    p.append("        while (cyc < cycles && errs < 20) begin check; #(%s); end" % step)
    p.append("        if (errs == 0) $display(\"EQUIV_PASS seed=%0d cycles=%0d checks=%0d known_bits=%0d\", seed0, cyc, checks, known);")
    p.append("        else $display(\"EQUIV_FAIL seed=%0d mismatching_checks=%0d first_cycle_window=%0d\", seed0, errs, cyc);")
    p.append("        $finish;")
    p.append("    end")
    p.append("endmodule")
    open(os.path.join(td, "tb_equiv.v"), "w").write("\n".join(p) + "\n")
    json.dump({"ports": ports}, open(os.path.join(td, "ports.json"), "w"))
    print("ports:", ports)


if __name__ == "__main__":
    main()
