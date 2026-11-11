#!/usr/bin/env python3
"""validate_selfcheck.py [task_id ...] -- prove every generated self-check testbench (lib/gen_selfcheck.py) is sound.

Per rebuild / bugfix task:
  (a) the golden file prints PASS under the self-check (direct iverilog/vvp or ghdl commands, workspace layout),
  (b) every start file (buggy file / empty body) does not,
  (c) how many mutants it catches out of the total (reported, not required),
  (d) the self-check seed is not a grader seed; the input traces of the self-check seed and of every grader seed are
      compared over the self-check window (fraction of sample points with identical inputs),
  (e) hdl-check.sh --tb, exactly as TASK.md tells the agent, run from a workspace built by run_agent.build_workspace
      (provide_tests=True) with the golden in place: RESULT: PASS and the literal PASS line; and on the untouched start
      workspace: RESULT: FAIL.
Writes ~/steer-bc250/tasks/SELFCHECK-VALIDATION.md and <scratch>/selfcheck_validation.json.  Nothing in the task
directories is modified.  Scratch: $SELFCHECK_SCRATCH (default: a temp dir).
"""
import glob, json, os, re, shutil, subprocess, sys, tempfile, time

LIB = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(LIB)
sys.path.insert(0, LIB)
sys.path.insert(0, BASE)
import gen_selfcheck as G  # noqa: E402
import run_agent  # noqa: E402  (build_workspace; importing it contacts nothing)
import run_api  # noqa: E402  (load_tasks)

LIGHT = os.path.join(LIB, "light.sh")
GHDL = ["--std=08", "-fsynopsys", "-frelaxed"]
SIMLIB = os.path.expanduser("~/gowin/IDE/simlib/gw5a")
SCRATCH = os.environ.get("SELFCHECK_SCRATCH") or tempfile.mkdtemp(prefix="selfcheck_val_")
BAD = re.compile(r"fail|error|mismatch", re.I)


def run(cmd, timeout, cwd=None, env=None):
    t0 = time.time()
    p = subprocess.run([LIGHT, str(timeout)] + cmd, cwd=cwd, capture_output=True, text=True, errors="replace", env=env)
    return p.returncode, p.stdout + p.stderr, time.time() - t0


def verdict(rc, out):
    lines = [l.strip() for l in out.splitlines()]
    fails = [l for l in lines if l.startswith("FAIL:")]
    if rc in (124, 137):
        return "timeout", fails
    if fails:
        return "FAIL", fails
    if "PASS" in lines and rc == 0:
        return "PASS", fails
    return "no_verdict", fails


def selfcheck_sim(cfg, td, sc, dut, R):
    """Run the self-check on one DUT file with the direct commands, in a fresh workspace-like directory R."""
    shutil.rmtree(R, ignore_errors=True)
    os.makedirs(R)
    for f in sc["files"]:
        shutil.copy(os.path.join(td, "selfcheck", f), os.path.join(R, f))
    gname = cfg["golden_files"][0]
    shutil.copy(dut, os.path.join(R, gname))
    ctx = [os.path.basename(c) for c in cfg.get("context_files", [])]
    for c in cfg.get("context_files", []):
        shutil.copy(os.path.join(td, c), os.path.join(R, os.path.basename(c)))
    t0 = time.time()
    if cfg["language"] == "vhdl":
        pre = []
        if cfg.get("gowin"):
            shutil.copy(os.path.join(SIMLIB, "prim_sim.vhd"), os.path.join(R, "prim_sim.vhd"))
            pre = ["prim_sim.vhd"]
        for f in pre + ctx + [gname, "tb_selfcheck_vectors.vhd", "tb_selfcheck.vhd"]:
            rc, out, _ = run(["ghdl", "-a"] + GHDL + [f], 300, cwd=R)
            if rc != 0:
                return {"verdict": "compile_error", "detail": "ghdl -a %s: %s" % (f, " | ".join(out.strip().splitlines()[:2]))[:300], "s": round(time.time() - t0, 1)}
        rc, out, _ = run(["ghdl", "-e"] + GHDL + ["tb_selfcheck"], 300, cwd=R)
        if rc != 0:
            return {"verdict": "compile_error", "detail": "ghdl -e: " + " | ".join(out.strip().splitlines()[:2])[:300], "s": round(time.time() - t0, 1)}
        rc, out, _ = run(["ghdl", "-r"] + GHDL + ["tb_selfcheck"], 300, cwd=R)
    else:
        pre = []
        if cfg.get("gowin"):
            shutil.copy(os.path.join(SIMLIB, "prim_sim.v"), os.path.join(R, "prim_sim.v"))
            pre = ["prim_sim.v"]
        rc, out, _ = run(["iverilog", "-g2012", "-o", "sim.vvp", "tb_selfcheck.v", gname] + ctx + pre, 300, cwd=R)
        if rc != 0:
            return {"verdict": "compile_error", "detail": "iverilog: " + " | ".join(out.strip().splitlines()[:2])[:300], "s": round(time.time() - t0, 1)}
        rc, out, _ = run(["vvp", "-n", "sim.vvp"], 300, cwd=R)
    v, fails = verdict(rc, out)
    stray = [l.strip() for l in out.splitlines() if BAD.search(l) and not l.strip().startswith("FAIL:")]
    info = [l.strip() for l in out.splitlines() if l.strip().startswith("selfcheck:")]
    return {"verdict": v, "first_fail": fails[0] if fails else "", "summary": fails[-1] if fails else (info[0] if info else ""),
            "stray_bad_lines": stray[:3], "s": round(time.time() - t0, 1)}


def input_overlap(cfg, td, sc, W):
    """Fraction of sample points whose (non-clock) inputs are identical between the self-check seed and each grader
    seed, over the self-check window (recorder testbench on the golden; nothing written to the task)."""
    os.makedirs(W, exist_ok=True)
    cyc = sc["cycles"]
    if cfg["language"] == "vhdl":
        G.vh_record(cfg, td, W, sc["seed"], cyc)
        runf = G.vh_record_run
    else:
        G.v_record(cfg, td, W, sc["seed"], cyc)
        runf = G.v_record_run

    def expand_trace(iv, n):
        out, j, cur = [], 0, None
        for k in range(n):
            while j < len(iv) and iv[j][0] <= k:
                cur = iv[j][1]
                j += 1
            out.append(cur)
        return out
    ev0, iv0, n0 = runf(W, sc["seed"], cyc)
    if not iv0:
        return {}
    t0 = expand_trace(iv0, n0)
    res = {}
    for s in G.grader_seeds(cfg["equiv"]):
        _, iv, n = runf(W, s, cyc)
        t = expand_trace(iv, n)
        m = min(len(t), len(t0))
        res[s] = {"same_points": sum(1 for a, b in zip(t[:m], t0[:m]) if a == b), "points": m, "identical_trace": t[:m] == t0[:m]}
    return res


def hdlcheck(cfg, td, sc, root):
    """Build the real agent workspace with provide_tests=True, then run TASK.md's command on the start file and on
    the golden.  Returns (result_start, result_golden, files, task_md_paragraph)."""
    tcfg = run_api.load_tasks([cfg["id"]], True)[0]
    ws = os.path.join(root, cfg["id"], "work")
    shutil.rmtree(os.path.join(root, cfg["id"]), ignore_errors=True)
    run_agent.build_workspace(tcfg, ws, None, provide_tests=True)
    files = sorted(os.listdir(ws))
    md = open(os.path.join(ws, "TASK.md")).read()
    para = md[md.index("## Self-check testbench"):md.index("## Original task statement")] if "## Self-check testbench" in md else ""
    tmp = os.path.join(root, "tmp")
    os.makedirs(tmp, exist_ok=True)
    env = dict(os.environ, TMPDIR=tmp)
    res = {}
    for which in ("start", "golden"):
        if which == "golden":
            shutil.copy(os.path.join(td, "golden", cfg["golden_files"][0]), os.path.join(ws, cfg["golden_files"][0]))
        rc, out, dt = run(["bash", "-c", sc["hdl_check_cmd"]], 600, cwd=ws, env=env)
        rl = [l for l in out.splitlines() if l.startswith("RESULT:")]
        res[which] = {"rc": rc, "result": rl[-1] if rl else "(no RESULT line, rc=%d)" % rc,
                      "pass_line": any(l.strip() == "PASS" for l in out.splitlines()),
                      "sim_lines": [l.strip() for l in out.splitlines() if l.strip().startswith(("FAIL:", "PASS", "selfcheck:"))][:3],
                      "bad_lines": sorted({l.strip() for l in out.splitlines() if "ERROR" in l})[:3],
                      "s": round(dt, 1)}
        open(os.path.join(root, cfg["id"], "hdlcheck_%s.txt" % which), "w").write(out)
    return res, files, para


def main():
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    tds = sorted(glob.glob(os.path.join(BASE, "tasks", "*", "task.json")))
    rows = []
    for tj in tds:
        td = os.path.dirname(tj)
        cfg = json.load(open(tj))
        if names and cfg["id"] not in names:
            continue
        if cfg["type"] not in ("rebuild", "bugfix"):
            continue
        row = {"task": cfg["id"], "language": cfg["language"], "type": cfg["type"], "gowin": bool(cfg.get("gowin"))}
        scj = os.path.join(td, "selfcheck", "selfcheck.json")
        if not os.path.exists(scj):
            row["excluded"] = "no selfcheck/ generated"
            rows.append(row)
            continue
        sc = json.load(open(scj))
        row.update(seed=sc["seed"], cycles=sc["cycles"], points=sc["sample_points"], events=sc["output_events"],
                   grader_seeds=sc["grader_seeds"], files=sc["files"],
                   vec_bytes=os.path.getsize(os.path.join(td, "selfcheck", sc["files"][1])))
        W = os.path.join(SCRATCH, "val", cfg["id"])
        shutil.rmtree(W, ignore_errors=True)
        os.makedirs(W)
        row["golden"] = selfcheck_sim(cfg, td, sc, os.path.join(td, "golden", cfg["golden_files"][0]), os.path.join(W, "golden"))
        row["start"] = []
        for f in sorted(glob.glob(os.path.join(td, "start", "*"))):
            r = selfcheck_sim(cfg, td, sc, f, os.path.join(W, "start"))
            row["start"].append({"file": os.path.basename(f), **r})
        row["mutants"] = []
        for f in sorted(glob.glob(os.path.join(td, "mutants", "*"))):
            r = selfcheck_sim(cfg, td, sc, f, os.path.join(W, "mut"))
            row["mutants"].append({"file": os.path.basename(f), **r})
        gs = G.grader_seeds(cfg["equiv"])
        row["seed_disjoint"] = sc["seed"] not in gs
        row["input_overlap"] = input_overlap(cfg, td, sc, os.path.join(W, "rec"))
        row["hdlcheck"], row["ws_files"], row["task_md_para"] = hdlcheck(cfg, td, sc, os.path.join(SCRATCH, "ws"))
        a = row["golden"]["verdict"] == "PASS" and not row["golden"]["stray_bad_lines"]
        b = all(s["verdict"] != "PASS" for s in row["start"]) and bool(row["start"])
        e = row["hdlcheck"]["golden"]["result"].startswith("RESULT: PASS") and row["hdlcheck"]["golden"]["pass_line"]
        row["a_ok"], row["b_ok"], row["e_ok"] = a, b, e
        if not (a and b and row["seed_disjoint"]):
            row["excluded"] = "; ".join(x for x, ok in (("golden does not PASS cleanly", a), ("start file passes", b),
                                                       ("seed collides with a grader seed", row["seed_disjoint"])) if not ok)
        rows.append(row)
        print("%-26s golden=%s start=%s mutants=%d/%d seed=%d grader=%s hdlcheck(golden)=%s pass_line=%s start=%s" % (
            cfg["id"], row["golden"]["verdict"], ",".join(s["verdict"] for s in row["start"]),
            sum(m["verdict"] != "PASS" for m in row["mutants"]), len(row["mutants"]), sc["seed"], gs,
            row["hdlcheck"]["golden"]["result"], row["hdlcheck"]["golden"]["pass_line"], row["hdlcheck"]["start"]["result"]), flush=True)
    json.dump(rows, open(os.path.join(SCRATCH, "selfcheck_validation.json"), "w"), indent=1)
    if not names:
        write_report(rows)


def fmt_seeds(gs):
    return ", ".join(str(s) if not isinstance(s, list) else "%d (%d cyc)" % (s[0], s[1]) for s in gs)


def write_report(rows):
    L = ["# Self-check testbench validation", "",
         "Generated %s by `lib/validate_selfcheck.py` for the testbenches made by `lib/gen_selfcheck.py` "
         "(tasks/<id>/selfcheck/). Testbench-type tasks (tb_*) have no self-check: the "
         "testbench is their deliverable." % time.strftime("%Y-%m-%d %H:%M"), "",
         "Columns: **golden** = the golden file run under the self-check with the direct commands (`iverilog -g2012 tb_selfcheck.v "
         "<dut> [ctx] [prim_sim.v]` + `vvp -n`, or `ghdl -a/-e/-r --std=08 -fsynopsys -frelaxed`) prints `PASS` and no other line "
         "containing fail/error/mismatch; **start** = verdict of each start file (must not be PASS); **mutants** = mutants not "
         "passing / total; **seed** = self-check seed vs grader seeds (grade.py runs task.json equiv.seeds); **same inputs** = "
         "largest fraction of sample points at which a grader seed drives exactly the same (non-clock) input values as the "
         "self-check seed, over the self-check window; **hdl-check** = `RESULT` of the TASK.md command run in a workspace built "
         "by `run_agent.build_workspace(..., provide_tests=True)`, on the golden (with the literal PASS line seen) / on the untouched "
         "start workspace.", "",
         "| task | lang | golden | start | mutants caught | self-check seed | grader seeds | same inputs | cycles / sample points | hdl-check golden / start |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "golden" not in r:
            L.append("| %s | %s | - | - | - | - | - | - | - | excluded: %s |" % (r["task"], r["language"], r["excluded"]))
            continue
        ov = r["input_overlap"]
        mx = max((v["same_points"] / v["points"] for v in ov.values()), default=0.0)
        ident = any(v["identical_trace"] for v in ov.values())
        hg, hs = r["hdlcheck"]["golden"], r["hdlcheck"]["start"]
        L.append("| %s | %s%s | %s | %s | %d/%d | %d | %s | %.1f%%%s | %d / %d | %s%s / %s |" % (
            r["task"], r["language"], " (Gowin)" if r["gowin"] else "", r["golden"]["verdict"],
            ", ".join("%s" % s["verdict"] for s in r["start"]),
            sum(m["verdict"] != "PASS" for m in r["mutants"]), len(r["mutants"]), r["seed"], fmt_seeds(r["grader_seeds"]),
            100.0 * mx, " (IDENTICAL)" if ident else "", r["cycles"], r["points"],
            hg["result"].split(" (")[0].replace("RESULT: ", ""), "" if hg["pass_line"] else " (no PASS line)",
            hs["result"].split(" (")[0].replace("RESULT: ", "")))
    exc = [r for r in rows if r.get("excluded")]
    L += ["", "## Excluded tasks", ""]
    L += ["- %s: %s" % (r["task"], r["excluded"]) for r in exc] or ["None: every rebuild / bugfix task meets (a) golden PASS and (b) start not PASS, with a non-grader seed."]
    notes = [r for r in rows if "golden" in r and not r["e_ok"]]
    L += ["", "## hdl-check caveats (not exclusions)", ""]
    L += ["- %s: on the golden the self-check prints the PASS line, but hdl-check ends with `%s` (non-simulation stage); "
          "see the hdl-check log line(s): %s" % (r["task"], r["hdlcheck"]["golden"]["result"], "; ".join("`%s`" % x for x in r["hdlcheck"]["golden"].get("bad_lines", [])))
          for r in notes] or ["None."]
    L += ["", "## Details", ""]
    for r in rows:
        if "golden" not in r:
            continue
        L.append("### %s" % r["task"])
        L.append("")
        L.append("- golden: %s (%s, %.1f s)" % (r["golden"]["verdict"], r["golden"]["summary"], r["golden"]["s"]))
        for s in r["start"]:
            L.append("- start `%s`: %s%s" % (s["file"], s["verdict"], (" (first line `%s`, last line `%s`)" % (s.get("first_fail"), s.get("summary"))
                                                                      if s["verdict"] == "FAIL" else " (%s)" % s.get("detail", "")) if s["verdict"] != "PASS" else ""))
        for m in r["mutants"]:
            L.append("- mutant `%s`: %s%s" % (m["file"], "caught" if m["verdict"] != "PASS" else "NOT caught",
                                              (" (%s, first line `%s`)" % (m["verdict"], m.get("first_fail") or m.get("detail") or "")) if m["verdict"] != "PASS" else ""))
        L.append("- seeds: self-check %d, grader %s, disjoint: %s; input overlap per grader seed: %s" % (
            r["seed"], fmt_seeds(r["grader_seeds"]), r["seed_disjoint"],
            ", ".join("seed %s %d/%d points%s" % (s, v["same_points"], v["points"], " IDENTICAL" if v["identical_trace"] else "")
                      for s, v in r["input_overlap"].items())))
        L.append("- vectors: %d output change events, %d bytes; workspace files: %s" % (r["events"], r["vec_bytes"], ", ".join(r["ws_files"])))
        L.append("- hdl-check golden: `%s`, sim lines %s (%.1f s); start: `%s`, sim lines %s" % (
            r["hdlcheck"]["golden"]["result"], r["hdlcheck"]["golden"]["sim_lines"], r["hdlcheck"]["golden"]["s"],
            r["hdlcheck"]["start"]["result"], r["hdlcheck"]["start"]["sim_lines"]))
        L.append("")
    open(os.path.join(BASE, "tasks", "SELFCHECK-VALIDATION.md"), "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
