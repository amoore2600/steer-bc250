#!/usr/bin/env python3
"""grade.py <taskdir> <candidate> [--keep] [--no-syn] [--no-resources] [--section=N] [--syn-only [--who=<name>]]

Grades one candidate file for one task.  Every tool run goes through light.sh (memory cap + timeout) or, for
gw_sh, gowin_syn.sh (heavy lane + memory cap + timeout).  Prints a one-line JSON summary as the last line:
  {"pass": bool|null, "stage": "struct|lint|compile|sim|tb|lint_wall|hygiene|pinned|syn|resources|syn_pending|done", ...}
and exits 0 on PASS, 1 on FAIL, 3 on syn_pending.

After a passing simulation / known-answer run (Verilog modules; lib/hygiene.py): lint_wall = Verilator -Wall
warnings in the candidate (a section job: in its section; task.json "lint_wall": false skips it), hygiene = licence and
copyright header, `default_nettype leak, non-ASCII, initial blocks (task.json "allow_initial"), pinned = task.json
"pinned_names" missing or of the wrong width. --no-gate (kat sections): skip the integration gate of the last section.

resources (after a passing synthesis; also on --syn-only): res["resources"] = {candidate, golden, dsp_limit,
limit_from, checked}, each count {dsp, bsram, prims} from GowinSynthesis's *_syn_rsc.xml (lib/gowin_syn.sh). FAIL at
stage resources when the candidate uses more DSP blocks (MULT*, ALU54*, PADD*) than the golden's count recorded in
validation.json, or than task.json "max_mult": N when set; "allow_more_dsp": true records the counts only; unchecked
when no limit is known. --no-resources: record only (validate.py grading the golden). BSRAM is recorded, not gated.

lint_warnings_raw (informational, never graded): every %Warning line the first Verilator -Wall lint prints for the
candidate file, DECLFILENAME included. An equivalence candidate is linted as candidate.v, so a clean module still
counts 1 there (DECLFILENAME); a kat candidate keeps the golden's file name and counts 0; in a section job lines
outside the section count too. VHDL: ghdl -s warnings; testbench tasks: warnings in candidate_tb.v. The graded
count is lint_wall.warnings (DECLFILENAME and waived lines excluded, a section job's own lines only).

syn_pending (pass null, sim_pass true): the simulation passed and no heavy lane was free for Gowin synthesis within
HEAVY_WAIT_S. It is neither a FAIL nor a harness error; the caller finishes the grade with --syn-only.

--syn-only: the same candidate preparation (section splice, candidate file name), then ONLY the task's Gowin
synthesis, queued through the queue runner $STEER_HEAVY_RUN (default lib/queue_run.sh; --kind gowin, --who from --who= or
STEER_SYN_WHO) with no deadline. The synthesis inputs are staged in a small self-contained git repository, so a
queue runner may run the job on another machine; tasks with private paths, task.json "syn_desktop_only": true, or
STEER_SYN_DESKTOP_ONLY=1 pass --desktop-only (this machine only). PASS = synthesis passed (the simulation passed
earlier); FAIL stage syn = Gowin reported errors; stage harness = no synthesis verdict (queue or lane failure).
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
import sections as SEC  # noqa: E402
import gen_kat as K  # noqa: E402
import hygiene as H  # noqa: E402
LIGHT = os.path.join(LIB, "light.sh")
GHDL_FLAGS = ["--std=08", "-fsynopsys", "-frelaxed"]
PRIM_SIM = os.path.join(os.path.expanduser(os.environ.get("GOWIN_HOME", "~/gowin/IDE")), "simlib", "gw5a", "prim_sim.v")


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


def progress(res, stage):
    """The verdict so far, written before the slow synthesis step (STEER_GRADE_PROGRESS=<file>), so a caller that times
    out still knows the simulation passed. Stage "syn_running": only the synthesis was left (lane wait or gw_sh)."""
    p = os.environ.get("STEER_GRADE_PROGRESS")
    if p:
        out = dict(res, stage=stage if stage == "syn_running" else stage + "_passed",
                   detail="simulation passed; synthesis not finished")
        out["pass"] = None
        out.pop("passed", None)
        open(p, "w").write(json.dumps(out) + "\n")


SYN_ONLY = "--syn-only" in sys.argv


def gowin(cfg, td, W, cand, res, no_syn):
    if no_syn or not cfg.get("gowin_syn", False):
        res["syn"] = None
        return True
    files = [expand(f, td) for f in cfg.get("syn_extra", [])] + [expand(f, td) for f in cfg.get("context_files", [])] + [cand]
    if SYN_ONLY:
        return queued_syn(cfg, td, W, files, res)
    progress(res, "syn_running")
    p = subprocess.run([os.path.join(LIB, "gowin_syn.sh"), os.path.join(W, "syn"), cfg["top"]] + files,
                       capture_output=True, text=True, errors="replace")
    try:
        syn = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        syn = {"syn_pass": False, "syn_detail": "gowin_syn.sh produced no summary: " + p.stdout[-200:] + p.stderr[-200:]}
    res["syn"] = syn
    if syn.get("syn_rc") == 75:                  # lib/heavy.sh: no free lane within HEAVY_WAIT_S
        res.update(passed=None, stage="syn_pending", sim_pass=True,
                   detail="simulation passed; Gowin synthesis pending (no free heavy lane within the grading time)")
        return False
    if not syn.get("syn_pass"):
        res.fail("syn", "Gowin synthesis failed: " + syn.get("syn_detail", ""))
        return False
    return resources(cfg, td, res)


def resources(cfg, td, res):
    """Stage resources, after a passing synthesis: the candidate may use at most as many DSP blocks as the golden
    (validation.json), or task.json "max_mult": N; "allow_more_dsp": true only records the counts. Unchecked when
    neither limit is known, or with --no-resources (validate.py grading the golden itself). Fills res["resources"]."""
    cand = (res.get("syn") or {}).get("resources")
    gold = H.golden_resources(td)
    limit, src = H.dsp_limit(cfg, td)
    on = cand is not None and limit is not None and not cfg.get("allow_more_dsp") and "--no-resources" not in sys.argv
    res["resources"] = {"candidate": cand, "golden": gold, "dsp_limit": limit, "limit_from": src, "checked": on}
    if on and cand["dsp"] > limit:
        prims = ", ".join("%s x%d" % (k, v) for k, v in sorted(cand["prims"].items()) if re.match(r"(MULT|ALU54|PADD)", k))
        res.fail("resources", "Gowin synthesis uses %d DSP block(s) (%s), %s %d%s: share the multiplier(s) as the "
                 "golden design does instead of one per use" % (
                     cand["dsp"], prims, "the golden uses" if src == "golden" else "task.json max_mult allows", limit,
                     " (golden %d)" % gold["dsp"] if src == "max_mult" and gold else ""))
        return False
    return True


def arg_value(name):
    for a in sys.argv[1:]:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def syn_bundle(cfg, td, W, files):
    """A self-contained git repository holding gowin_syn.sh, heavy.sh and the synthesis inputs (task files at their
    task-relative paths, so `include` lookups next to them still resolve), with run_syn.sh printing the summary as
    `STEER_SYN_RESULT <json>`. A queue runner may run it on another machine: only this repository is copied there."""
    B = os.path.join(W, "synq")
    os.makedirs(os.path.join(B, "lib"))
    for f in ("gowin_syn.sh", "heavy.sh"):
        shutil.copy(os.path.join(LIB, f), os.path.join(B, "lib", f))
    rtd, rW = os.path.realpath(td), os.path.realpath(W)
    rel = []
    for i, f in enumerate(files):
        rf = os.path.realpath(f)
        if rf.startswith(rtd + os.sep):
            dst = os.path.join("task", os.path.relpath(rf, rtd))
        elif rf.startswith(rW + os.sep):
            dst = os.path.join("work", os.path.relpath(rf, rW))
        else:
            dst = os.path.join("ext", "%02d_%s" % (i, os.path.basename(rf)))
        os.makedirs(os.path.dirname(os.path.join(B, dst)), exist_ok=True)
        shutil.copy(rf, os.path.join(B, dst))
        rel.append(dst)
    for root, _, names in os.walk(rtd):                  # header files a task file may include
        for n in names:
            if n.endswith((".vh", ".svh", ".h", ".inc")):
                dst = os.path.join(B, "task", os.path.relpath(os.path.join(root, n), rtd))
                if not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy(os.path.join(root, n), dst)
    q = lambda x: "'" + x.replace("'", "'\\''") + "'"
    open(os.path.join(B, "run_syn.sh"), "w").write(
        "#!/usr/bin/env bash\n# Gowin synthesis of one steer candidate, run inside a queue runner's lane (HEAVY_LANE_HELD=1)\n"
        "cd \"$(dirname \"$(readlink -f \"$0\")\")\" || exit 2\n"
        "r=$(HEAVY_LANE_HELD=1 bash lib/gowin_syn.sh out %s %s | tail -n 1)\n"
        "echo \"STEER_SYN_RESULT $r\"\n" % (q(cfg["top"]), " ".join(q(x) for x in rel)))
    open(os.path.join(B, ".gitignore"), "w").write("out/\n")
    g = ["git", "-C", B, "-c", "user.name=steer", "-c", "user.email=steer@localhost", "-c", "commit.gpgsign=false"]
    for c in (["init", "-q"], ["add", "-A", "-f", "."], ["commit", "-q", "-m", "steer synthesis bundle"]):
        p = subprocess.run(g + c, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError("synthesis bundle: git %s: %s" % (c[0], p.stderr.strip()[:200]))
    return B


def queued_syn(cfg, td, W, files, res):
    """--syn-only: the synthesis through the queue runner, no deadline. A SIGTERM (sys.exit from the
    handler in main) cancels the queued ticket and stops the job."""
    hr = os.environ.get("STEER_HEAVY_RUN") or os.path.join(LIB, "queue_run.sh")
    who = arg_value("--who") or os.environ.get("STEER_SYN_WHO") or "steer %s" % cfg["id"]
    desk = bool(cfg.get("syn_desktop_only") or K.private_paths(cfg) or os.environ.get("STEER_SYN_DESKTOP_ONLY") == "1")
    B = syn_bundle(cfg, td, W, files)
    cmd = [hr, "--who", who, "--kind", "gowin", "--mem", os.environ.get("STEER_SYN_MEM", "4G"),
           "--timeout", str(int(os.environ.get("SYN_TIMEOUT", "900")) + 600)] + (["--desktop-only"] if desk else []) + \
          (["--env", "SYN_TIMEOUT=" + os.environ["SYN_TIMEOUT"]] if os.environ.get("SYN_TIMEOUT") else []) + \
          (["--env", "GOWIN_SH=" + os.environ["GOWIN_SH"]] if os.environ.get("GOWIN_SH") else []) + ["--", "bash", "run_syn.sh"]
    res["syn_queue"] = {"who": who, "desktop_only": desk}
    proc, ticket, lines = None, {}, []
    t0 = time.time()
    try:
        proc = subprocess.Popen(cmd, cwd=B, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                start_new_session=True)
        for l in proc.stdout:
            lines.append(l)
            m = re.search(r"\[queue\]\s+(q\d+)", l)
            if m and "id" not in ticket:
                ticket["id"] = m.group(1)
            m = re.search(r"dispatched to (.*?) \(", l)
            if m:
                ticket["running"] = True
                res["syn_queue"]["placement"] = m.group(1)
                res["syn_queue"]["wait_s"] = round(time.time() - t0, 1)
        proc.wait()
    finally:
        if proc is not None and proc.poll() is None:
            if ticket.get("id") and not ticket.get("running"):
                subprocess.run([hr, "--cancel", ticket["id"], "--who", who], capture_output=True, timeout=60)
            try:
                os.killpg(proc.pid, 15)
                proc.wait(60)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    pass
    res["syn_queue"].update(ticket=ticket.get("id"), rc=proc.returncode, total_s=round(time.time() - t0, 1))
    out = "".join(lines)
    got = [l.split("STEER_SYN_RESULT", 1)[1].strip() for l in lines if l.startswith("STEER_SYN_RESULT")]
    try:
        syn = json.loads(got[-1])
    except Exception:
        syn = None
    if syn is None:
        res.fail("harness", "queued synthesis gave no verdict (queue runner rc=%s%s): %s" % (
            proc.returncode, ", ticket cancelled" if proc.returncode == 3 else "", out.strip()[-200:].replace("\n", " | ")))
        return False
    res["syn"] = syn
    if syn.get("syn_pass"):
        return resources(cfg, td, res)
    if syn.get("syn_errors"):
        res.fail("syn", "Gowin synthesis failed: " + syn.get("syn_detail", ""))
    else:
        # no Gowin ERROR line: a lane, timeout or tool failure, not the design's
        res.fail("harness", "synthesis did not run to a verdict (%s; queue runner rc=%s)" % (syn.get("syn_detail"), proc.returncode))
    return False


def grade_syn_only(cfg, td, W, cand, res, no_syn):
    """--syn-only: the candidate under the same file name the full grade gives it, then only gowin()."""
    if cfg["type"] == "testbench" or not cfg.get("gowin_syn", False) or no_syn:
        res.update(passed=True, stage="done", detail="no Gowin synthesis for this task", syn=None)
        return res
    if cfg["type"] == "kat":
        c = os.path.join(W, cfg["golden_files"][0])
    elif cfg["language"] == "vhdl":
        c = os.path.join(W, "candidate.vhd")
    else:
        c = os.path.join(W, "candidate" + (".sv" if cfg.get("language") == "systemverilog" else ".v"))
    shutil.copy(cand, c)
    if gowin(cfg, td, W, c, res, no_syn):
        res.update(passed=True, stage="done", detail="Gowin synthesis passed (the simulation passed earlier)")
    return res


def cone(cfg, td, W, cand, res, ctx):
    """Optional structural gate (task.json "cone_check": {"script": ..., "args": [...]}): the script gets the context files,
    the candidate and --baseline <golden file>, and prints CONE_PASS or CONE_FAIL <reason> as its last line."""
    cc = cfg.get("cone_check")
    if not cc:
        return True
    gold = os.path.join(td, "golden", cfg["golden_files"][0])
    rc, out, _ = run([sys.executable, expand(cc["script"], td)] + [expand(a, td) for a in cc.get("args", [])] + ctx + [cand,
                     "--baseline", gold], cc.get("timeout", 600))
    lines = out.strip().splitlines()
    res["cone"] = {"report": [l for l in lines if re.match(r"^[A-Z_]+ ", l) and not l.startswith("CONE_")][:8],
                   "verdict": lines[-1][:300] if lines else "no output"}
    if not lines or not lines[-1].startswith("CONE_PASS"):
        res.fail("cone", "structural check: " + (lines[-1] if lines else "no output rc=%d" % rc))
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
    lint_lines = out.splitlines()
    cname = os.path.basename(c)
    errs = [l for l in out.splitlines() if l.startswith("%Error")]
    res["lint_warnings_raw"] = sum(1 for l in out.splitlines() if l.startswith("%Warning") and cname in l)
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
    seeds = eq.get("final_seeds") if ("--final" in sys.argv and eq.get("final_seeds")) else eq.get("seeds", [1, 2, 3])
    res["seed_set"] = "final" if seeds is not eq.get("seeds", [1, 2, 3]) else "default"
    for s in seeds:
        s, ncyc = (s if isinstance(s, list) else [s, None])
        rc, out, dt = run(["vvp", "-n", vvp, "+seed=%d" % s] + (["+cycles=%d" % ncyc] if ncyc else []), eq.get("sim_timeout", 120), cwd=W)
        m = re.search(r"^EQUIV_PASS.*$", out, re.M)
        res["sim_seeds"].append({"seed": s, "ok": bool(m), "s": round(dt, 1)})
        if not m:
            mm = re.findall(r"^MISMATCH .*$", out, re.M)
            mi = re.findall(r"^MISMATCH_INPUTS .*$", out, re.M)
            simfail = "seed %d: " % s + (mm[0] if mm else ("timeout" if rc in (124, 137) else "no EQUIV_PASS rc=%d %s" % (rc, out[-200:])))
            if mm and mi:
                simfail += " | " + mi[0][:200]
            break
        res["known_bits"] = res.get("known_bits", 0) + int(re.search(r"known_bits=(\d+)", m.group(0)).group(1))
    if simfail:
        return res.fail("sim", simfail)          # no structural check or synthesis after a failed simulation
    span = SEC.body_span_lines(open(c, errors="replace").read(), res["section"], cfg["language"]) \
        if res.get("section") is not None else None
    if not H.grade_checks(cfg, td, W, c, lint_lines, res, run, span, sim_extra + ctx):
        return res
    progress(res, "sim")
    if not cone(cfg, td, W, c, res, ctx):
        return res
    if not gowin(cfg, td, W, c, res, no_syn):
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
    res["lint_warnings_raw"] = len(re.findall(r":warning:", out))
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
    progress(res, "sim")
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
    res["lint_warnings_raw"] = sum(1 for l in out.splitlines() if l.startswith("%Warning") and "candidate_tb" in l)
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


def section_arg():
    for a in sys.argv[1:]:
        if a.startswith("--section="):
            return int(a.split("=", 1)[1])
    return None


def splice_section(cfg, td, W, cand, n, res):
    """Section job: the candidate's body of SECTION n replaces that section in the golden; the rest of the
    candidate file is ignored. Returns the spliced file, or None (res failed) when the markers are missing."""
    gname = cfg["golden_files"][0]
    sc = [x for x in cfg.get("sections", []) if int(x["n"]) == n]
    if not sc:
        res.fail("harness", "task has no SECTION %d" % n)
        return None
    res["section"] = n
    res["section_drives"] = [x["name"] for x in sc[0].get("drives", [])]
    body = SEC.body(open(cand, errors="replace").read(), n, cfg["language"])
    if body is None:
        res.fail("struct", "the markers `// SECTION %d: ...` and `// END SECTION %d` must each appear exactly once" % (n, n))
        return None
    gold = open(os.path.join(td, "golden", gname), errors="replace").read()
    out = os.path.join(W, "spliced_" + gname)
    open(out, "w").write(SEC.replace(gold, n, body, cfg["language"]))
    return out


def grade_kat_sections(cfg, td, W, cand, sec, res):
    """kat sections (lib/kat_sections.py): --section=N grades layers 1-2 of one section; --integration runs the
    whole-file command on the candidate (the assembled file)."""
    import kat_sections as KS
    if not os.path.exists(cand) or os.path.getsize(cand) == 0:
        return res.fail("extract", "empty candidate")
    if "--integration" in sys.argv:
        ig = (cfg.get("kat") or {}).get("integration") or {}
        if not ig.get("cmd") or ig.get("placeholder"):
            return res.fail("harness", "no integration command yet (placeholder)")
        af = os.path.join(W, cfg["golden_files"][0])
        shutil.copy(cand, af)
        ok, detail = KS.gate(cfg, td, af, W, run)
        res["integration"] = detail
        if not ok and detail.startswith("HARNESS:"):
            return res.fail("harness", detail)
        if not ok:
            h = KS.section_hint(cfg, detail)
            res["likely_section"] = h
            return res.fail("integration", detail + (" (likely SECTION %d)" % h if h else ""))
        res.update(passed=True, stage="done", detail="integration: " + detail)
        return res
    if sec is None:
        return res.fail("harness", "a kat sections task is graded per section (--section=N) or with --integration")
    if "--no-gate" in sys.argv:
        k = dict(cfg.get("kat") or {})
        k["integration"] = dict(k.get("integration") or {}, at_section=None)
        cfg = dict(cfg, kat=k)
    if KS.grade_section(cfg, td, W, cand, sec, res, run):
        res.update(passed=True, stage="done", detail="SECTION %d: lint clean against the real port lists, connected as "
                   "specified%s" % (sec, "" if not res.get("checker") else ", module checker " + (
                       res["checker"] if str(res["checker"]).startswith("pending") else "PASS")))
    return res


def grade_kat(cfg, td, W, cand, res, no_syn):
    """kat task: lint the candidate, compile it with the known-answer checker (a supplied testbench and its support
    files, or the generated lockstep testbench and the external reference), run it, and read the verdict line."""
    k = cfg["kat"]
    top = cfg["top"]
    c = os.path.join(W, cfg["golden_files"][0])
    shutil.copy(cand, c)
    if not struct_check(cfg, c, res):
        return res
    ctx = [expand(f, td) for f in cfg.get("context_files", [])]
    rc, out, _ = run(["verilator", "--lint-only", "-Wall", "-Wno-fatal", "--top-module", top] + ctx + [c], 120)
    lint_lines = out.splitlines()
    errs = [l for l in out.splitlines() if l.startswith("%Error")]
    res["lint_warnings_raw"] = sum(1 for l in out.splitlines() if l.startswith("%Warning") and os.path.basename(c) in l)
    if rc != 0 or errs:
        return res.fail("lint", "verilator: " + (errs[0] if errs else "rc=%d %s" % (rc, out[-200:])))
    if k.get("mode", "testbench") == "testbench":
        tb = expand(k.get("tb_local") or k["tb"], td)
        sup = [expand(f, td) for f in (k.get("support_local") if "support_local" in k else k.get("support_files", []))]
        if k.get("private") and k.get("support_private"):
            sup += [expand(f, td) for f in k.get("support_files", [])]
        tbtop = k["tb_top"]
        pass_rx = k.get("pass_regex") or r"^KAT %s: PASS\s*$" % re.escape(top)
    else:
        tb = os.path.join(td, "kat_ref_tb.v")
        sup = [expand(f, td) for f in k["ref_files"]]
        tbtop = "kat_ref_tb"
        pass_rx = r"^KAT %s: PASS\s*$" % re.escape(top)
    vvp = os.path.join(W, "kat.vvp")
    rc, out, _ = run(["iverilog", "-g2012", "-o", vvp, "-s", tbtop, tb, c] + ctx + sup, 300)
    if rc != 0:
        return res.fail("compile", "iverilog: " + " | ".join(out.strip().splitlines()[:3]))
    res["sim_seeds"] = []
    seeds = k.get("seeds", [1]) if k.get("mode") == "reference" else [None]
    for s in seeds:
        rc, out, dt = run(["vvp", "-n", vvp] + (["+seed=%d" % s] if s is not None else []), int(k.get("timeout", 600)), cwd=W)
        verdicts = [l.strip() for l in out.splitlines() if re.match(r"^\s*KAT ", l)]
        ok = bool(re.search(pass_rx, out, re.M)) and not re.search(k.get("fail_regex") or r"^KAT %s: FAIL" % re.escape(top), out, re.M)
        res["sim_seeds"].append({"seed": s, "ok": ok, "s": round(dt, 1)})
        if not ok:
            fails = [l.strip() for l in out.splitlines() if re.search(r"FAIL|ERROR|MISMATCH", l) and not l.lstrip().startswith("KAT ")]
            v = verdicts[-1] if verdicts else ("timeout" if rc in (124, 137) else "no verdict line rc=%d" % rc)
            return res.fail("tb", v + ((" | " + " | ".join(fails[:3])) if fails else ""))
    if not H.grade_checks(cfg, td, W, c, lint_lines, res, run, None, ctx):
        return res
    progress(res, "sim")
    if not gowin(cfg, td, W, c, res, no_syn):
        return res
    res.update(passed=True, stage="done", detail="known-answer checker PASS" + (" on %d seeds" % len(seeds) if seeds != [None] else ""))
    return res


def main():
    import signal
    for sg in (signal.SIGTERM, signal.SIGHUP):          # unwind (finally blocks: lane tickets, overlays) on a stop
        signal.signal(sg, lambda signum, frame: sys.exit(128 + signum))
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    td, cand = os.path.abspath(args[0]), os.path.abspath(args[1])
    keep = "--keep" in sys.argv
    no_syn = "--no-syn" in sys.argv
    cfg = json.load(open(os.path.join(td, "task.json")))
    base = os.environ.get("STEER_GRADE_SCRATCH") or os.path.join(os.environ.get("STEER_BC250_BASE") or os.path.dirname(LIB), "scratch", "grade")
    if K.private_paths(cfg):
        # a private reference is compiled only outside the repository (the simulation binary contains it)
        base = os.environ.get("STEER_PRIVATE_SCRATCH") or os.path.join(tempfile.gettempdir(), "steer_private_grade")
    os.makedirs(base, exist_ok=True)
    W = tempfile.mkdtemp(prefix=cfg["id"] + "_", dir=base)
    res = Result(task=cfg["id"], passed=False, stage="none", detail="")
    t0 = time.time()
    try:
        sec = section_arg()
        kat_sections = cfg["type"] == "kat" and (cfg.get("kat") or {}).get("mode") == "sections"
        if kat_sections:
            grade_kat_sections(cfg, td, W, cand, sec, res)
        elif sec is not None and os.path.exists(cand) and os.path.getsize(cand):
            cand = splice_section(cfg, td, W, cand, sec, res) or ""
        if res["stage"] != "none" or kat_sections:
            pass
        elif not os.path.exists(cand) or os.path.getsize(cand) == 0:
            res.fail("extract", "empty candidate")
        elif SYN_ONLY:
            grade_syn_only(cfg, td, W, cand, res, no_syn)
        elif cfg["type"] == "kat":
            grade_kat(cfg, td, W, cand, res, no_syn)
        elif cfg["type"] == "testbench" and "--equiv" not in sys.argv:
            grade_testbench(cfg, td, W, cand, res, no_syn)
        elif cfg["language"] == "vhdl":
            grade_vhdl_equiv(cfg, td, W, cand, res, no_syn)
        else:
            grade_verilog_equiv(cfg, td, W, cand, res, no_syn)
    except Exception as e:  # harness error, reported as such
        res.fail("harness", "%s: %s" % (type(e).__name__, e))
    if res.get("section") is not None and res.get("detail"):
        res["detail"] = ("section %d (drives %s): " % (res["section"], ", ".join(res.get("section_drives", [])[:12])) + res["detail"])[:400]
    res["grade_s"] = round(time.time() - t0, 1)
    if not keep:
        shutil.rmtree(W, ignore_errors=True)
    else:
        res["workdir"] = W
    out = dict(res)
    out["pass"] = out.pop("passed")
    print(json.dumps(out))
    sys.exit(0 if out["pass"] else 3 if out["stage"] == "syn_pending" else 1)


if __name__ == "__main__":
    main()
