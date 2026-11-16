#!/usr/bin/env python3
"""gen_kat.py -- helpers for "kat" tasks: the worker writes a module that is graded by a known-answer checker, not by
equivalence with a Claude-written golden.

Two checker modes (task.json "kat"):
  testbench  a supplied self-checking testbench ("tb", "tb_top") compiled with the candidate and any "support_files";
             its verdict line ("pass_regex", default `^KAT <top>: PASS`) decides. Vector files the bench reads at
             compile time go in support_files too.
  reference  an EXTERNAL reference module ("ref_files", "ref_top"; e.g. an upstream GHDL netlist) run in lockstep with
             the candidate by a testbench generated here (kat_ref_tb.v): the task's clocks/init/decls/stim drive the
             candidate's inputs, "ref_inputs" maps every reference input to an expression of those signals,
             "compare" lists {name, cand, ref, width, mask} pairs (c_<port> = candidate output, r_<port> = reference
             output) checked whenever "enable" is true, and "extra_checks" lists {name, ok} conditions on the same
             signals. Prints the first mismatch in the self-check's format and `KAT <top>: PASS|FAIL`.

Licence: a reference (or support file, or mutant directory) may be marked private ("private": true): its paths must
resolve outside this repository, it is never copied into tasks/ or a workspace, never named in prompts, and it is only
read at grading time, in a work directory outside the repository.
"""
import hashlib, json, os, re, subprocess, sys

LIB = os.path.dirname(os.path.abspath(__file__))
REPO_ROOTS = [os.path.realpath(os.path.dirname(LIB))]
if os.environ.get("STEER_BC250_BASE"):
    REPO_ROOTS.append(os.path.realpath(os.environ["STEER_BC250_BASE"]))


def expand(p, td):
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.join(td, p)


def read_source(spec, td):
    """A file's text: a path (absolute, ~, or relative to the task directory), or `git:<repo>@<rev>:<path>` for a file
    as committed (pins a source that another chat is still editing)."""
    if spec.startswith("git:"):
        repo_rev, path = spec[4:].rsplit(":", 1)
        repo, rev = repo_rev.rsplit("@", 1)
        p = subprocess.run(["git", "-C", os.path.expanduser(repo), "show", "%s:%s" % (rev, path)], capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit("cannot read %s: %s" % (spec, p.stderr.strip()[:200]))
        return p.stdout
    return open(expand(spec, td), errors="replace").read()


def source_name(spec):
    return os.path.basename(spec.rsplit(":", 1)[-1] if spec.startswith("git:") else spec)


def private_paths(cfg):
    k = cfg.get("kat") or {}
    out = []
    if k.get("private"):
        out += list(k.get("ref_files", []))
        if k.get("support_private"):
            out += list(k.get("support_files", []))
        if k.get("mutants_dir"):
            out.append(k["mutants_dir"])
    return out


def check_private(cfg, td):
    """Private references must live outside every repository root and never appear (byte-identical) in the task dir.
    Returns a list of problems (empty = fine)."""
    probs = []
    hashes = {}
    for p in private_paths(cfg):
        if not os.path.isabs(os.path.expanduser(p)):
            probs.append("private path %s must be absolute (outside the repository)" % p)
            continue
        rp = os.path.realpath(os.path.expanduser(p))
        for root in REPO_ROOTS:
            if rp == root or rp.startswith(root + os.sep):
                probs.append("private path %s resolves inside %s" % (p, root))
        if os.path.isfile(rp):
            hashes[hashlib.sha256(open(rp, "rb").read()).hexdigest()] = p
        elif not os.path.isdir(rp):
            probs.append("private path %s does not exist" % p)
    if hashes:
        for dp, _, fs in os.walk(td):
            for f in fs:
                fp = os.path.join(dp, f)
                try:
                    h = hashlib.sha256(open(fp, "rb").read()).hexdigest()
                except OSError:
                    continue
                if h in hashes:
                    probs.append("%s is a copy of the private file %s" % (fp, hashes[h]))
    return probs


def check_derived(cfg, td):
    """kat.derived_checks: [{file, from, pattern, repl, count}]: `file` must equal `from` with the regex substitution
    applied (multiline) exactly `count` times -- e.g. a simulation copy of a vendored module that differs by one
    documented edit. Returns a list of problems."""
    probs = []
    for d in (cfg.get("kat") or {}).get("derived_checks", []):
        src = read_source(d["from"], td)
        new, n = re.subn(d["pattern"], d["repl"], src, flags=re.M)
        if n != d.get("count", n):
            probs.append("%s: the edit applies %d times to %s, expected %d" % (d["file"], n, d["from"], d["count"]))
        elif read_source(d["file"], td) != new:
            probs.append("%s is not %s with the documented edit (regenerate it)" % (d["file"], d["from"]))
    return probs


def ref_tb(cfg, ports):
    """The lockstep testbench of reference mode. ports: [(name, dir, width)] of the candidate module."""
    k = cfg["kat"]
    top = cfg["top"]
    clocks = k["clocks"]
    cnames = {c["name"] for c in clocks}
    step = k.get("step", 1.0)
    ins = [(n, w) for n, d, w in ports if d == "input" and n not in cnames]
    cmp_ = k["compare"]
    p = ["`timescale 1ns/10ps", "`default_nettype wire",
         "// Generated by lib/gen_kat.py: the candidate %s in lockstep with the reference %s." % (top, k["ref_top"]),
         "module kat_ref_tb;",
         "    integer seed, errs, checks, cyc, cycles, first;"]
    for n, d, w in ports:
        rng = "" if w == 1 else "[%d:0] " % (w - 1)
        p.append(("    reg  %s%s = 0;" % (rng, n)) if d == "input" else ("    wire %sc_%s;" % (rng, n)))
    for n, w in k["ref_outputs"].items():
        p.append("    wire %sr_%s;" % ("" if w == 1 else "[%d:0] " % (w - 1), n))
    p.append("    function [31:0] r32(input integer dummy); begin r32 = $random(seed); end endfunction")
    p.append("    function chance(input integer n); reg [7:0] v; begin v = $random(seed); chance = (v < n); end endfunction")
    p.append(k.get("decls", ""))
    conns = [".%s(%s)" % (n, n if d == "input" else "c_" + n) for n, d, w in ports]
    p.append("    %s dut (%s);" % (top, ", ".join(conns)))
    rc = [".%s(%s)" % (n, e) for n, e in k["ref_inputs"].items()] + [".%s(r_%s)" % (n, n) for n in k["ref_outputs"]]
    p.append("    %s ref_u (%s);" % (k["ref_top"], ", ".join(rc)))
    for c in clocks:
        p.append("    initial begin #(%s); forever #(%s) %s = ~%s; end" % (c.get("phase", 0), c["half"], c["name"], c["name"]))
    p.append("    always @(posedge %s) cyc <= cyc + 1;" % clocks[0]["name"])
    for c in clocks:
        code = k["stim"].get(c["name"], "")
        if code:
            p.append("    always @(negedge %s) begin\n%s\n    end" % (c["name"], code))
    p.append("    integer nerr [0:%d]; integer fcyc [0:%d];" % (len(cmp_) + len(k.get("extra_checks", [])) - 1,
                                                                len(cmp_) + len(k.get("extra_checks", [])) - 1))
    p.append("    reg [%d:0] badv;" % (len(cmp_) + len(k.get("extra_checks", [])) - 1))
    names = []
    p.append("    task show_first; integer sh; begin")
    p.append("        $display(\"FAIL: first mismatch at cycle %0d (sample point %0d); every value below is from that sample point\", cyc, checks);")
    for i in range(0, min(len(ins), 18), 6):
        grp = ins[i:i + 6]
        p.append("        $display(\"FAIL:   inputs %s\", %s);" % (" ".join("%s=%%h" % n for n, _ in grp), ", ".join(n for n, _ in grp)))
    for i, c in enumerate(cmp_):
        m = c.get("mask")
        cv = "(%s) & (%s)" % (c["cand"], m) if m else "(%s)" % c["cand"]
        rv = "(%s) & (%s)" % (c["ref"], m) if m else "(%s)" % c["ref"]
        p.append("        if (badv[%d]) $display(\"FAIL:   output %s expected %%h got %%h\", %s, %s);" % (i, c["name"], rv, cv))
    for j, x in enumerate(k.get("extra_checks", [])):
        p.append("        if (badv[%d]) $display(\"FAIL:   check '%s' does not hold\");" % (len(cmp_) + j, x["name"]))
    p.append("    end endtask")
    p.append("    task check; begin")
    p.append("        badv = 0;")
    p.append("        if (%s) begin" % k.get("enable", "1"))
    for i, c in enumerate(cmp_):
        m = c.get("mask")
        cv = "((%s) & (%s))" % (c["cand"], m) if m else "(%s)" % c["cand"]
        rv = "((%s) & (%s))" % (c["ref"], m) if m else "(%s)" % c["ref"]
        p.append("            if (%s !== %s) badv[%d] = 1'b1;" % (cv, rv, i))
        names.append(c["name"])
    for j, x in enumerate(k.get("extra_checks", [])):
        p.append("            if (!(%s)) badv[%d] = 1'b1;" % (x["ok"], len(cmp_) + j))
        names.append("check '%s'" % x["name"])
    p.append("            checks = checks + 1;")
    p.append("        end")
    for i in range(len(names)):
        p.append("        if (badv[%d]) begin errs = errs + 1; if (nerr[%d] == 0) fcyc[%d] = cyc; nerr[%d] = nerr[%d] + 1; end" % (i, i, i, i, i))
    p.append("        if (badv != 0 && first == 0) begin first = 1; show_first; end")
    p.append("    end endtask")
    p.append("    initial begin : main")
    p.append("        integer i, sh;")
    p.append("        errs = 0; checks = 0; cyc = 0; first = 0;")
    p.append("        for (i = 0; i < %d; i = i + 1) begin nerr[i] = 0; fcyc[i] = -1; end" % len(names))
    p.append("        if (!$value$plusargs(\"seed=%d\", seed)) seed = 1;")
    p.append("        if (!$value$plusargs(\"cycles=%%d\", cycles)) cycles = %d;" % k.get("cycles", 4000))
    p.append(k.get("init", ""))
    p.append("        #(%s);" % (step / 2.0))
    p.append("        while (cyc < cycles) begin check; #(%s); end" % step)
    p.append("        if (errs == 0 && checks > 0) begin")
    p.append("            $display(\"kat: %0d cycles, %0d sample points compared\", cyc, checks);")
    p.append("            $display(\"KAT %s: PASS\");" % top)
    p.append("        end else begin")
    p.append("            sh = 0;")
    for i, n in enumerate(names):
        p.append("            if (nerr[%d] != 0) begin if (sh < 6) $display(\"FAIL: %s: %%0d mismatching sample points, first at cycle %%0d\", nerr[%d], fcyc[%d]); sh = sh + 1; end"
                 % (i, n.replace('"', "'"), i, i))
    p.append("            if (checks == 0) $display(\"FAIL: nothing was compared (enable never true)\");")
    p.append("            $display(\"FAIL: %0d mismatches\", errs);")
    p.append("            $display(\"KAT %s: FAIL (%%0d mismatches)\", errs);" % top)
    p.append("        end")
    p.append("        $finish;")
    p.append("    end")
    p.append("endmodule")
    return "\n".join(p) + "\n"


SUMMARIZE = r"""
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
"""

KAT_CHECK = """#!/bin/bash
# kat_check.sh -- lint, compile and run the known-answer checker @TB@ against @G@. Do not edit.
cd "$(dirname "$0")" || exit 2
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
echo "== lint (verilator -Wall)"
verilator --lint-only -Wall -Wno-fatal -Wno-DECLFILENAME --top-module @TOP@ @G@ > "$W/lint.txt" 2>&1
grep -E "^%(Error|Warning)" "$W/lint.txt" | head -15
NE=$(grep -c "^%Error" "$W/lint.txt")
echo "== compile (iverilog -g2012)"
if ! iverilog -g2012 -o "$W/sim" -s @TBTOP@ @TB@ @G@ @SUP@ > "$W/comp.txt" 2>&1; then
    head -20 "$W/comp.txt"; echo "RESULT: FAIL (compile)"; exit 1
fi
echo "== known-answer checker (@TB@)"
timeout @TO@ vvp -n "$W/sim" > "$W/run.txt" 2>&1
python3 - "$W/run.txt" <<'PYEOF'
@SUMMARIZE@
PYEOF
if grep -qE '@PRX@' "$W/run.txt" && [ "$NE" = 0 ]; then echo "RESULT: PASS"; exit 0; fi
N=$(grep -oE '@CRX@' "$W/run.txt" | head -1 | grep -oE '[0-9]+' | head -1)
[ -z "$N" ] && N=$(grep -cE "FAIL|ERROR|MISMATCH" "$W/run.txt")
[ "$N" = 0 ] && N=1
echo "FAIL: $N mismatches"
if [ "$NE" != 0 ]; then echo "RESULT: FAIL (lint: $NE errors)"; else echo "RESULT: FAIL (checker)"; fi
exit 1
"""


def kat_check_sh(cfg, files):
    """The worker's self-check for a testbench-mode kat task: lint, compile, run the bench, print the first failing
    lines and a count per kind of failure (not the bench's whole flood), and one RESULT line. `files` = the bench and
    its support files, as named in the workspace."""
    k = cfg["kat"]
    top, gname = cfg["top"], cfg["golden_files"][0]
    tb, sup = files[0], files[1:]
    pass_rx = k.get("pass_regex") or "^KAT %s: PASS" % top
    count_rx = k.get("count_regex") or r"FAIL \(([0-9]+) (errors|mismatches)\)"
    sh = KAT_CHECK
    for a, b in (("@TBTOP@", k["tb_top"]), ("@TB@", tb), ("@G@", gname), ("@TOP@", top), ("@SUP@", " ".join(sup)),
                 ("@TO@", str(int(k.get("timeout", 600)))), ("@PRX@", pass_rx.replace("'", "'\\''")),
                 ("@CRX@", count_rx.replace("'", "'\\''")), ("@SUMMARIZE@", SUMMARIZE.strip())):
        sh = sh.replace(a, b)
    return sh

if __name__ == "__main__":
    td = os.path.abspath(sys.argv[1])
    cfg = json.load(open(os.path.join(td, "task.json")))
    print("\n".join(check_private(cfg, td) + check_derived(cfg, td)) or "ok")
