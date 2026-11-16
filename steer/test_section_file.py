#!/usr/bin/env python3
"""test_section_file.py -- section-file mode (a section job's worker writes only section_s<n>.v):

  A. workspace + self-check, no LLM: v_section_pwm@s2 -- section_s2.v holds the marker line and the body,
     context/pwm_gen.v is read-only with a placeholder, no full module file at the root; run_selfcheck.sh splices
     and PASSes the golden body, FAILs a section mutant, FAILs a whole module written into the section file, and maps
     a syntax error to section_s2.v:<line>.
  B. merge: each of v_section_pwm's three section files (golden bodies) spliced as run_task does, graded with
     grade.sh --section=n --no-syn (must pass), merged by coordinator.Project.merge; the merged file equals the golden.
  A2. whole-file mode (--section-file off): the self-check takes only the job's section from the worker's full file,
     as the grader does: another section broken (v_section_pwm s2 with SECTION 3 broken) still PASSes, with the note "only lines inside SECTION n count; ...".
  C. one steered session per mode with a scripted fake LLM (oc_probe.py) and the stub engine: --section-file on
     (writes section_s2.v) and off (writes the whole pwm_gen.v with SECTION 3 broken: counted as
     out_of_section_edits, no lint note); both pass the hidden grader (--no-syn).
Private lanes and board locks only; no board, no Gowin.
"""
import json, os, re, shutil, signal, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("STEER_BC250_BASE") or os.path.dirname(HERE)
os.environ["STEER_BC250_BASE"] = BASE
sys.path[:0] = [HERE, BASE, os.path.join(BASE, "lib")]
results = []
TASK = "v_section_pwm"


def check(name, cond, extra=""):
    results.append(bool(cond))
    print("%-70s %s%s" % (name, "ok" if cond else "FAIL", ("  " + extra) if extra and not cond else ""))


def selfcheck(ws, out, text):
    p = os.path.join(ws, out)
    os.chmod(p, 0o644)
    open(p, "w").write(text)
    r = subprocess.run(["bash", "run_selfcheck.sh"], cwd=ws, capture_output=True, text=True, timeout=900)
    return r.returncode, r.stdout + r.stderr


def workspace(cfg, ws, section_file=True):
    import run_agent
    import steer_agent as sa
    out = run_agent.build_workspace(cfg, ws, None, provide_tests=True, section_file=section_file)
    sa.install_snapshot_wrapper(cfg, ws, out)
    return out


def test_workspace(tmp):
    import run_api, run_agent
    SEC = run_agent.SEC
    cfg = run_agent.section_cfg(run_api.load_tasks([TASK], False)[0], 2)
    ws = os.path.join(tmp, "ws2")
    out = workspace(cfg, ws)
    gold = open(os.path.join(cfg["dir"], "golden", "pwm_gen.v")).read()
    ctx = os.path.join(ws, "context", "pwm_gen.v")
    check("A: the worker's file is section_s2.v", out == "section_s2.v")
    check("A: no full module file at the workspace root", not os.path.exists(os.path.join(ws, "pwm_gen.v")))
    check("A: context/pwm_gen.v is read-only with the placeholder",
          os.path.exists(ctx) and not os.access(ctx, os.W_OK) and "<<< your section goes here, see section_s2.v >>>" in open(ctx).read())
    start = open(os.path.join(ws, out)).read()
    check("A: section_s2.v starts with the SECTION 2 marker line", start.split("\n", 1)[0].strip().startswith("// SECTION 2:"))
    task_md = open(os.path.join(ws, "TASK.md")).read()
    check("A: TASK.md explains the section file and the context", "section_s2.v` is YOUR file" in task_md and "context/pwm_gen.v" in task_md)
    rc, o = selfcheck(ws, out, start)
    check("A: start body (empty section) FAILs the self-check", rc != 0 and "RESULT: FAIL" in o)
    rc, o = selfcheck(ws, out, SEC.section_file_text(gold, 2))
    check("A: golden body PASSes the self-check", rc == 0 and re.search(r"(?m)^RESULT: PASS", o), o[-300:])
    mut = open(os.path.join(cfg["dir"], "mutants", "s2_m3.v")).read()
    rc, o = selfcheck(ws, out, SEC.section_file_text(mut, 2))
    check("A: mutant s2_m3 body FAILs with a mismatch count", rc != 0 and re.search(r"FAIL: \d+ mismatches", o))
    rc, o = selfcheck(ws, out, gold)
    check("A: a whole module in the section file FAILs (section file)", rc != 0 and "RESULT: FAIL (section file)" in o)
    lines = SEC.section_file_text(gold, 2).splitlines(True)
    k = next(i for i, l in enumerate(lines) if i > 2 and l.rstrip().endswith(";"))
    lines[k] = lines[k].rstrip()[:-1] + "\n"
    rc, o = selfcheck(ws, out, "".join(lines))
    m = re.search(r"%Error[^\n]*?section_s2\.v:(\d+)", o)
    check("A: a syntax error is reported in section_s2.v (line %d or %d)" % (k + 1, k + 2),
          rc != 0 and m and int(m.group(1)) in (k + 1, k + 2), o[:400])
    check("A: no assembled path leaks into the output", ".assembled/" not in o.replace("`.assembled/", ""))
    check("A: .sc snapshots are of the section file", open(os.path.join(ws, ".sc", "0001")).read() == SEC.section_file_text(gold, 2))



BROKEN = "    this is not verilog;\n"


def test_whole(tmp):
    import run_api, run_agent
    SEC = run_agent.SEC
    cfg = run_agent.section_cfg(run_api.load_tasks([TASK], False)[0], 2)
    ws = os.path.join(tmp, "wh2")
    out = workspace(cfg, ws, section_file=False)
    gold = open(os.path.join(cfg["dir"], "golden", "pwm_gen.v")).read()
    check("A2: whole mode keeps the full module file", out == "pwm_gen.v")
    rc, o = selfcheck(ws, out, SEC.replace(gold, 3, BROKEN))
    check("A2: another section broken -> PASS (only SECTION 2 counts)", rc == 0 and re.search(r"(?m)^RESULT: PASS", o), o[-400:])
    check("A2: ... with the out-of-section note",
          "NOTE: only lines inside SECTION 2 count; your changes to SECTION 3 are ignored" in o, o[:300])
    start = open(os.path.join(ws, ".section_base", out)).read()
    rc, o = selfcheck(ws, out, SEC.replace(start, 3, BROKEN))
    check("A2: own section empty -> FAIL", rc != 0 and "RESULT: FAIL" in o)


def test_merge(tmp):
    import run_api, run_agent
    import coordinator as C
    SEC = run_agent.SEC
    pdir = os.path.join(tmp, "proj")
    os.makedirs(pdir)
    json.dump({"name": "secfile", "goal": "merge test", "jobs": [{"id": "s%d" % n, "task": TASK, "section": n} for n in (3, 1, 2)]},
              open(os.path.join(pdir, "project.json"), "w"))
    rd = os.path.join(tmp, "rd")
    os.makedirs(os.path.join(rd, "jobs"))
    P = C.Project(pdir, rd)
    gold = open(os.path.join(BASE, "tasks", TASK, "golden", "pwm_gen.v")).read()
    for jid in ("s3", "s1", "s2"):
        cfg = P.task_cfg[jid]
        n = int(cfg["section"])
        ws = os.path.join(tmp, "m_" + jid)
        out = run_agent.build_workspace(cfg, ws, None, provide_tests=False, section_file=True)
        open(os.path.join(ws, out), "w").write(SEC.section_file_text(gold, n))
        ctx = open(os.path.join(ws, ".git", "steer_context_pwm_gen.v")).read()
        full, prob = SEC.splice_file(ctx, n, open(os.path.join(ws, out)).read())       # what run_task hands the grader
        cand = os.path.join(tmp, "cand_%s.v" % jid)
        open(cand, "w").write(full or "")
        g = subprocess.run([os.path.join(cfg["dir"], "grade.sh"), cand, "--section=%d" % n, "--no-syn"],
                           capture_output=True, text=True, timeout=1200)
        gr = json.loads(g.stdout.strip().splitlines()[-1])
        check("B: %s spliced candidate passes grade.sh --section=%d" % (jid, n), prob is None and gr.get("pass"), str(gr)[:300])
        P.jobs[jid]["board"] = "test"
        P.merge(jid, cand, {})
    merged = open(os.path.join(P.repo, "pwm_gen.v")).read()
    check("B: the coordinator's merged pwm_gen.v equals the golden", merged == gold)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_session(tmp, mode):
    import run_agent
    SEC = run_agent.SEC
    gold = open(os.path.join(BASE, "tasks", TASK, "golden", "pwm_gen.v")).read()
    path, content = ("section_s2.v", SEC.section_file_text(gold, 2)) if mode == "on" else \
        ("pwm_gen.v", SEC.replace(gold, 3, BROKEN))
    steps = [{"tool": "write", "args": {"path": path, "content": content}},
             {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
             {"text": "Done."}]
    d = os.path.join(tmp, "sess_" + mode)
    os.makedirs(d)
    json.dump(steps, open(os.path.join(d, "s.json"), "w"))
    port = free_port()
    llm = subprocess.Popen([sys.executable, os.path.join(HERE, "oc_probe.py"), "--serve-tcp", str(port), "--out",
                            os.path.join(d, "llm"), "--script", os.path.join(d, "s.json")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    run_id = "secfiletest_%s_%d" % (mode, os.getpid())
    try:
        p = subprocess.run([sys.executable, os.path.join(HERE, "steer_agent.py"), "--run-id", run_id, "--engine", "stub",
                            "--tasks", TASK + "@s2", "--provide-tests", "--auto-done", "--timeout", "400", "--max-steers", "2",
                            "--section-file", mode, "--grade-args=--no-syn"],
                           env=dict(os.environ, BAKEOFF_INFER_HOST="127.0.0.1", BAKEOFF_INFER_PORT=str(port),
                                    STEER_HEAVY_LOCKS=os.path.join(d, "l0"), STEER_BOARD_LOCK_DIR=d),
                           capture_output=True, text=True, timeout=1200)
    finally:
        os.killpg(llm.pid, signal.SIGTERM)
    rd = os.path.join(BASE, "results", "steer_" + run_id)
    try:
        rec = json.loads(open(os.path.join(rd, "results.jsonl")).readline())
    except (OSError, ValueError):
        rec = {}
    check("C: --section-file %s: session recorded section_file=%s" % (mode, mode == "on"), rec.get("section_file") == (mode == "on"),
          (p.stdout + p.stderr)[-800:])
    check("C: --section-file %s: worker output file is %s" % (mode, path), rec.get("output_file") == path)
    check("C: --section-file %s: job passes the hidden grader" % mode, rec.get("pass"), str(rec.get("detail"))[:300])
    m = rec.get("mistakes") or {}
    check("C: --section-file %s: no false pre-gate notes (lint, header, section file)" % mode,
          not any(m.get(k) for k in ("lint_errors", "header_violations", "section_file_errors", "block_declarations")), str(m))
    check("C: --section-file %s: out_of_section_edits %s" % (mode, "0" if mode == "on" else ">= 1"),
          (m.get("out_of_section_edits", 0) >= 1) == (mode == "off"), str(m))
    cand = rec.get("candidate")
    body = SEC.body(open(cand).read(), 2) if cand and os.path.exists(cand) else None
    check("C: --section-file %s: graded candidate is the full module with the golden section" % mode,
          body == SEC.body(gold, 2) and "endmodule" in open(cand).read())
    shutil.rmtree(rd, ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "agent_ws", "steer_" + run_id), ignore_errors=True)


def main():
    tmp = tempfile.mkdtemp(prefix="steer_secfile_")
    only = sys.argv[1:] or ["A", "B", "C"]
    try:
        if "A" in only:
            test_workspace(tmp)
        if "A" in only or "A2" in only:
            test_whole(tmp)
        if "B" in only:
            test_merge(tmp)
        if "C" in only:
            test_session(tmp, "on")
            test_session(tmp, "off")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("SECTION FILE TEST: %s (%d/%d)" % ("PASS" if all(results) else "FAIL", sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
