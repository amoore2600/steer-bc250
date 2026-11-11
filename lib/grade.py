#!/usr/bin/env python3
"""grade.py <taskdir> <candidate> [--keep] [--no-syn]

Grades one candidate file for one task.  Every tool run goes through light.sh (memory cap + timeout) or, for
gw_sh, gowin_syn.sh (heavy lane + memory cap + timeout).  Prints a one-line JSON summary as the last line:
  {"pass": bool, "stage": "struct|lint|compile|sim|tb|syn|done", "detail": str, ...}
and exits 0 on PASS, 1 on FAIL.
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

LIB = os.path.dirname(os.path.abspath(__file__))
LIGHT = os.path.join(LIB, "light.sh")
GHDL_FLAGS = ["--std=08", "-fsynopsys", "-frelaxed"]
PRIM_SIM = os.path.expanduser("~/gowin/IDE/simlib/gw5a/prim_sim.v")


def run(cmd, timeout, cwd=None):
    t0 = time.time()
    p = subprocess.run([LIGHT, str(timeout)] + cmd, cwd=cwd, capture_output=True, text=True, errors="replace")
    if p.returncode != 0 and re.search(r"Failed to (start transient scope|connect to bus|create bus connection)", p.stderr):
        raise RuntimeError("systemd-run could not create the capped scope: " + p.stderr.strip()[:200])
    return p.returncode, p.stdout + p.stderr, time.time() - t0


def expand(p, td):
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.join(td, p)


def strip_comments(s):
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    s = re.sub(r"//[^\n]*", " ", s)
    s = re.sub(r"--[^\n]*", " ", s)
    return s


class Result(dict):
    def fail(self, stage, detail):
        self.update(passed=False, stage=stage, detail=detail[:400])
        return self


def struct_check(cfg, cand, res):
    src = strip_comments(open(cand, errors="replace").read())
    for rx in cfg.get("require_regex", []):
        if not re.search(rx, src, re.I | re.M):
            res.fail("struct", "required construct missing: /%s/" % rx)
            return False
    return True


def gowin(cfg, td, W, cand, res, no_syn):
    if no_syn or not cfg.get("gowin_syn", False):
        res["syn"] = None
        return True
    files = [expand(f, td) for f in cfg.get("syn_extra", [])] + [expand(f, td) for f in cfg.get("context_files", [])] + [cand]
    p = subprocess.run([os.path.join(LIB, "gowin_syn.sh"), os.path.join(W, "syn"), cfg["top"]] + files,
                       capture_output=True, text=True, errors="replace")
    try:
        syn = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        syn = {"syn_pass": False, "syn_detail": "gowin_syn.sh produced no summary: " + p.stdout[-200:] + p.stderr[-200:]}
    res["syn"] = syn
    if not syn.get("syn_pass"):
        res.fail("syn", "Gowin synthesis failed: " + syn.get("syn_detail", ""))
        return False
    return True


def grade_verilog_equiv(cfg, td, W, cand, res, no_syn):
    eq = cfg["equiv"]
    top = cfg["top"]
    ext = ".sv" if cfg.get("language") == "systemverilog" else ".v"
    c = os.path.join(W, "candidate" + ext)
    shutil.copy(cand, c)
    if not struct_check(cfg, c, res):
        return res
    ctx = [expand(f, td) for f in cfg.get("context_files", [])]
    lint_extra = [expand(f, td) for f in eq.get("lint_extra", [])]
    rc, out, _ = run(["verilator", "--lint-only", "-Wall", "-Wno-fatal", "--top-module", top] + lint_extra + ctx + [c], 120)
    cname = os.path.basename(c)
    errs = [l for l in out.splitlines() if l.startswith("%Error")]
    res["lint_warnings"] = sum(1 for l in out.splitlines() if l.startswith("%Warning") and cname in l)
    if rc != 0 or errs:
        return res.fail("lint", "verilator: " + (errs[0] if errs else "rc=%d %s" % (rc, out[-200:])))
    sim_extra = [expand(f, td) for f in eq.get("sim_extra", [])]
    vvp = os.path.join(W, "sim.vvp")
    rc, out, _ = run(["iverilog", "-g2012", "-o", vvp, os.path.join(td, "tb_equiv.v"), os.path.join(td, "golden_renamed.v")]
                     + sim_extra + ctx + [c], 300)
    if rc != 0:
        return res.fail("compile", "iverilog: " + " | ".join(out.strip().splitlines()[:3]))
    res["sim_seeds"] = []
    simfail = None
    for s in eq.get("seeds", [1, 2, 3]):
        s, ncyc = (s if isinstance(s, list) else [s, None])
        rc, out, dt = run(["vvp", "-n", vvp, "+seed=%d" % s] + (["+cycles=%d" % ncyc] if ncyc else []), eq.get("sim_timeout", 120), cwd=W)
        m = re.search(r"^EQUIV_PASS.*$", out, re.M)
        res["sim_seeds"].append({"seed": s, "ok": bool(m), "s": round(dt, 1)})
        if not m:
            mm = re.findall(r"^MISMATCH.*$", out, re.M)
            simfail = "seed %d: " % s + (mm[0] if mm else ("timeout" if rc in (124, 137) else "no EQUIV_PASS rc=%d %s" % (rc, out[-200:])))
            break
        res["known_bits"] = res.get("known_bits", 0) + int(re.search(r"known_bits=(\d+)", m.group(0)).group(1))
    syn_ok = gowin(cfg, td, W, c, res, no_syn)
    if simfail:
        return res.fail("sim", simfail)
    if not syn_ok:
        return res
    res.update(passed=True, stage="done", detail="equivalent on %d seeds" % len(res["sim_seeds"]))
    return res


def grade_vhdl_equiv(cfg, td, W, cand, res, no_syn):
    eq = cfg["equiv"]
    c = os.path.join(W, "candidate.vhd")
    shutil.copy(cand, c)
    if not struct_check(cfg, c, res):
        return res
    wd = "--workdir=" + W
    GF = GHDL_FLAGS + ["-P" + W]
    pre = [expand(f, td) for f in eq.get("vhdl_pre", [])]           # e.g. Gowin prim_sim.vhd, both libraries
    ctx = [expand(f, td) for f in cfg.get("context_files", [])]
    gold = [os.path.join(td, "golden", f) for f in cfg["golden_files"]]
    for f in pre + ctx + gold:
        rc, out, _ = run(["ghdl", "-a"] + GF + ["--work=gold", wd, f], 300)
        if rc != 0:
            return res.fail("harness", "golden analysis failed: " + out[-300:])
    for f in pre + ctx:
        rc, out, _ = run(["ghdl", "-a"] + GF + [wd, f], 300)
        if rc != 0:
            return res.fail("harness", "context analysis failed: " + out[-300:])
    rc, out, _ = run(["ghdl", "-s"] + GF + [wd, c], 120)
    res["lint_warnings"] = len(re.findall(r":warning:", out))
    if rc != 0:
        return res.fail("lint", "ghdl -s: " + " | ".join(l for l in out.strip().splitlines() if "error" in l)[:300])
    rc, out, _ = run(["ghdl", "-a"] + GF + [wd, c], 120)
    if rc != 0:
        return res.fail("compile", "ghdl -a: " + " | ".join(out.strip().splitlines()[:3]))
    rc, out, _ = run(["ghdl", "-a"] + GF + [wd, os.path.join(td, "tb_equiv.vhd")], 120)
    if rc != 0:
        return res.fail("compile", "testbench analysis (port/type mismatch?): " + " | ".join(out.strip().splitlines()[:3]))
    rc, out, _ = run(["ghdl", "-e"] + GF + [wd, "tb_equiv"], 300, cwd=W)
    if rc != 0:
        return res.fail("compile", "ghdl -e: " + " | ".join(out.strip().splitlines()[:3]))
    res["sim_seeds"] = []
    for s in eq.get("seeds", [1, 2, 3]):
        s, ncyc = (s if isinstance(s, list) else [s, None])
        rc, out, dt = run(["ghdl", "-r"] + GF + [wd, "tb_equiv", "-gSEED=%d" % s, "--ieee-asserts=disable"] + (["-gCYCLES=%d" % ncyc] if ncyc else []),
                          eq.get("sim_timeout", 120), cwd=W)
        m = re.search(r"EQUIV_PASS.*known_bits=(\d+)", out)
        res["sim_seeds"].append({"seed": s, "ok": bool(m), "s": round(dt, 1)})
        if not m:
            mm = re.findall(r"MISMATCH[^\n]*", out)
            return res.fail("sim", "seed %d: " % s + (mm[0] if mm else ("timeout" if rc in (124, 137) else "no EQUIV_PASS rc=%d %s" % (rc, out[-200:]))))
        res["known_bits"] = res.get("known_bits", 0) + int(m.group(1))
    if not gowin(cfg, td, W, c, res, no_syn):
        return res
    res.update(passed=True, stage="done", detail="equivalent on %d seeds" % len(res["sim_seeds"]))
    return res


def tb_run(W, tb, dut_files, tag, timeout):
    vvp = os.path.join(W, tag + ".vvp")
    rc, out, _ = run(["iverilog", "-g2012", "-o", vvp, tb] + dut_files, 120)
    if rc != 0:
        return "compile_error", out
    rc, out, _ = run(["vvp", "-n", vvp], timeout, cwd=W)
    lines = [l.strip() for l in out.splitlines()]
    if rc in (124, 137):
        return "timeout", out
    if any("TB_FAIL" in l for l in lines):
        return "tb_fail", out
    if "TB_PASS" in lines:
        return "tb_pass", out
    return "no_verdict", out


def grade_testbench(cfg, td, W, cand, res, no_syn):
    tbc = cfg["tbtask"]
    c = os.path.join(W, "candidate_tb.v")
    shutil.copy(cand, c)
    gold = [os.path.join(td, "golden", f) for f in cfg["golden_files"]]
    rc, out, _ = run(["verilator", "--lint-only", "-Wall", "-Wno-fatal", "--timing"] + gold + [c], 120)
    res["lint_warnings"] = sum(1 for l in out.splitlines() if l.startswith("%Warning") and "candidate_tb" in l)
    res["lint_errors"] = sum(1 for l in out.splitlines() if l.startswith("%Error") and "candidate_tb" in l)
    v, out = tb_run(W, c, gold, "golden", tbc.get("timeout", 60))
    if v == "compile_error":
        return res.fail("compile", "iverilog: " + " | ".join(out.strip().splitlines()[:3]))
    res["golden_verdict"] = v
    if v != "tb_pass":
        return res.fail("tb", "testbench does not print TB_PASS on the golden module (%s): %s" % (v, out.strip()[-200:]))
    caught = []
    for m in tbc["mutants"]:
        mv, _ = tb_run(W, c, [os.path.join(td, m)], "mut", tbc.get("timeout", 60))
        caught.append({"mutant": m, "verdict": mv, "caught": mv != "tb_pass"})
    res["mutants"] = caught
    n = sum(x["caught"] for x in caught)
    if n < tbc.get("min_caught", 2):
        return res.fail("tb", "caught %d of %d mutants (need %d)" % (n, len(caught), tbc.get("min_caught", 2)))
    res.update(passed=True, stage="done", detail="golden TB_PASS, caught %d of %d mutants" % (n, len(caught)))
    return res


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    td, cand = os.path.abspath(args[0]), os.path.abspath(args[1])
    keep = "--keep" in sys.argv
    no_syn = "--no-syn" in sys.argv
    cfg = json.load(open(os.path.join(td, "task.json")))
    base = os.path.expanduser("~/steer-bc250/scratch/grade")
    os.makedirs(base, exist_ok=True)
    W = tempfile.mkdtemp(prefix=cfg["id"] + "_", dir=base)
    res = Result(task=cfg["id"], passed=False, stage="none", detail="")
    t0 = time.time()
    try:
        if not os.path.exists(cand) or os.path.getsize(cand) == 0:
            res.fail("extract", "empty candidate")
        elif cfg["type"] == "testbench" and "--equiv" not in sys.argv:
            grade_testbench(cfg, td, W, cand, res, no_syn)
        elif cfg["language"] == "vhdl":
            grade_vhdl_equiv(cfg, td, W, cand, res, no_syn)
        else:
            grade_verilog_equiv(cfg, td, W, cand, res, no_syn)
    except Exception as e:  # harness error, reported as such
        res.fail("harness", "%s: %s" % (type(e).__name__, e))
    res["grade_s"] = round(time.time() - t0, 1)
    if not keep:
        shutil.rmtree(W, ignore_errors=True)
    else:
        res["workdir"] = W
    out = dict(res)
    out["pass"] = out.pop("passed")
    print(json.dumps(out))
    sys.exit(0 if out["pass"] else 1)


if __name__ == "__main__":
    main()
