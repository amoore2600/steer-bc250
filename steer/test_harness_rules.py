#!/usr/bin/env python3
"""test_harness_rules.py -- regression tests for the harness enforcement rules, with a scripted fake LLM (no board):

  a steered session on v_rebuild_ce_frac whose scripted worker makes, in order: a reply with code and no write
  (H7), an edit whose search text has Read line prefixes (H8), a write that changes the module header (H10), a write
  with an unused constant (H10), writes with a -Wall WIDTHTRUNC warning, a non-ASCII character, a `default_nettype
  none leak and no SPDX line (lib/hygiene.py), then the correct file and the self-check. Every counter must fire
  once and the job must pass. Plus unit checks of the best ranking (H3) and of the grader's progress file (H2).
  SYN_PENDING (two coordinator runs in parallel, every synthesis lane busy): the simulation passes, the lane wait runs
  out, the job ends SYN_PENDING with no worker restart and its board is freed; (a) once a lane frees, the queued
  synthesis passes and the job is merged; (b) a SIGTERM while it waits leaves the candidate and the finishing command
  in the project log and results, and withdraws the queue ticket. grade.py --syn-only alone: PASS, Gowin ERROR lines =
  stage syn, a tool failure with no ERROR line = stage harness.
  Done gate: a task copy built with lint_wall off is switched on while the worker runs; its self-check PASSes a file
  with a -Wall warning, the auto-done is withheld with the warning sent, and done comes only after a clean file.
  Edit lock: after a failed edit of the graded file, the next edit (which would break the simulation) is rejected,
  the file restored and the turn redirected with "write the whole file with Write now"; the job passes.
  Repeated first failure: the worker runs the self-check twice on the same failing file, then writes a debug side
  file and runs it again: exactly one harness note (a redirect, not a steer) quoting the self-check's first FAIL line,
  counted as repeat_first_fail; the job then passes.
  Dead code: a write with a constant-only unused sink is counted as dead_code by the pre-gate.
  Write guard: after a passing self-check, a write with no module (a 2-line fragment) and then a write of the header
  alone (under half of the compiling file) are each undone at once: the turn is stopped, the file restored, the
  redirect names `git show best:ce_frac.v`; write_guard counts 2, no pre-gate note on the fragments, the job passes.
  Debug leftovers: a write with `$display` is counted as debug_left by the pre-gate.
  Steer queue (mailbox engine, a responder thread answers the first request with a steer while the worker sleeps):
  the queued steer goes out as a redirect within --steer-queue-max, and the wait is logged.
Private lanes, a private lib/queue_run.sh queue directory, a fake gw_sh and private board locks only."""
import glob, json, os, re, shutil, signal, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("STEER_BC250_BASE") or os.path.dirname(HERE)
sys.path[:0] = [HERE, BASE]
TASK = "v_rebuild_ce_frac"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


FAKE_GW_SH = """#!/bin/bash
echo "fake gw_sh $*"
if [ "${FAKE_GW_FAIL:-0}" = 1 ]; then echo "ERROR (EX0000) : fake synthesis error"; exit 1; fi
if [ "${FAKE_GW_CRASH:-0}" = 1 ]; then echo "fake tool crash"; exit 1; fi
mkdir -p syn/impl/gwsynthesis && echo "// fake netlist" > syn/impl/gwsynthesis/fake.vg
"""


def syn_env(tmp):
    """Private synthesis lanes (both busy), a private lib/queue_run.sh queue directory, a fake gw_sh."""
    os.makedirs(tmp)
    fake = os.path.join(tmp, "fake_gw_sh")
    open(fake, "w").write(FAKE_GW_SH)
    os.chmod(fake, 0o755)
    hl = os.path.join(tmp, "hl")
    for l in (hl, hl + ".2"):
        os.mkdir(l)
    return {"STEER_HEAVY_LOCKS": "%s,%s.2" % (hl, hl), "HEAVY_QUEUE_DIR": os.path.join(tmp, "queue"),
            "QUEUE_POLL_S": "1", "GOWIN_SH": fake, "STEER_BOARD_LOCK_DIR": tmp,
            "STEER_GRADE_SCRATCH": os.path.join(tmp, "gs")}


def syn_only_classification(tmp):
    """grade.py --syn-only on a free private lane: PASS; Gowin ERROR lines -> stage syn (the worker's failure); a tool
    failure without an ERROR line -> stage harness (not charged to the worker)."""
    env = dict(os.environ, STEER_BC250_BASE=BASE, **syn_env(tmp))
    os.rmdir(os.path.join(tmp, "hl.2"))
    td = os.path.join(BASE, "tasks", TASK)
    ok = True
    for extra, want in (({}, ("done", True)), ({"FAKE_GW_FAIL": "1"}, ("syn", False)), ({"FAKE_GW_CRASH": "1"}, ("harness", False))):
        p = subprocess.run([sys.executable, os.path.join(BASE, "lib", "grade.py"), td, os.path.join(td, "golden", "ce_frac.v"),
                            "--syn-only", "--who=rulestest"], env=dict(env, **extra), capture_output=True, text=True, timeout=300)
        try:
            g = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            g = {}
        r = (g.get("stage"), g.get("pass")) == want and (g.get("syn_queue") or {}).get("who") == "rulestest"
        print("--syn-only %s -> stage %s pass %s: %s" % (",".join(extra) or "clean", g.get("stage"), g.get("pass"), r))
        ok &= r
    return ok


def syn_pending_start(tmp, stop):
    """Start a one-job coordinator run whose synthesis lanes are all busy. Returns (proc, llm, run_id, env)."""
    env = dict(os.environ, STEER_BC250_BASE=BASE, **syn_env(tmp))
    gold = open(os.path.join(BASE, "tasks", TASK, "golden", "ce_frac.v")).read()
    json.dump([{"tool": "write", "args": {"path": "ce_frac.v", "content": gold}},
               {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}}, {"text": "Done."}],
              open(os.path.join(tmp, "s.json"), "w"))
    pdir = os.path.join(tmp, "proj")
    os.makedirs(pdir)
    json.dump({"name": "synpend_test", "goal": "syn pending test", "jobs": [{"id": "ce", "task": TASK, "summary": "ce"}]},
              open(os.path.join(pdir, "project.json"), "w"))
    port = free_port()
    llm = subprocess.Popen([sys.executable, os.path.join(HERE, "oc_probe.py"), "--serve-tcp", str(port), "--out",
                            os.path.join(tmp, "llm"), "--script", os.path.join(tmp, "s.json")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    run_id = "synpend%s_%d" % ("stop" if stop else "", os.getpid())
    # --grade-timeout 1260: the grader's synthesis lane wait (HEAVY_WAIT_S = grade timeout - 1200) is 60 s
    p = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "coordinator.py"), "--project", pdir, "--run-id", run_id,
                          "--engine", "stub", "--provide-tests", "--auto-done", "--timeout", "400", "--max-steers", "2",
                          "--max-attempts", "2", "--grade-timeout", "1260", "--boards", "b0=127.0.0.1:%d" % port],
                         env=env, stdout=open(os.path.join(tmp, "coord.log"), "w"), stderr=subprocess.STDOUT,
                         start_new_session=True)
    return p, llm, run_id, env


def rows_of(rd):
    f = os.path.join(rd, "results.jsonl")
    return [json.loads(l) for l in open(f) if l.strip()] if os.path.exists(f) else []


def syn_pending_finish(tmp, p, llm, run_id, stop):
    rd = os.path.join(BASE, "results", "proj_" + run_id)
    ok, t0 = True, time.time()
    try:
        while time.time() - t0 < 600 and p.poll() is None and not any(r.get("status") == "SYN_PENDING" for r in rows_of(rd)):
            time.sleep(2)
        rows = rows_of(rd)
        pend = [r for r in rows if r.get("status") == "SYN_PENDING"]
        name = "SYN_PENDING%s: " % (" (stop)" if stop else "")
        r = len(pend) == 1 and pend[0]["grade"].get("sim_pass") and pend[0].get("best_snapshot") and \
            os.path.exists(pend[0]["best_snapshot"])
        print(name + "simulation passed, lane wait ran out -> SYN_PENDING with the candidate as best snapshot: %s" % bool(r))
        ok &= bool(r)
        time.sleep(8)                                     # the coordinator's loop frees the board within 5 s
        r = not os.path.exists(os.path.join(tmp, "steer_board_b0.job"))
        print(name + "board lock freed while synthesis is pending: %s" % r)
        ok &= r
        if stop:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(120)
            time.sleep(3)
            log = open(os.path.join(rd, "project_log.md")).read()
            st = [r for r in rows_of(rd) if r.get("stopped")]
            r = "STOPPED with job ce SYN_PENDING" in log and pend[0]["candidate"] in log and len(st) == 1 and \
                st[0]["candidate"] == pend[0]["candidate"] and "--syn-only" in st[0]["syn_finish_cmd"]
            print(name + "SIGTERM leaves candidate + finishing command in project log and results: %s" % r)
            ok &= r
            q = os.path.join(tmp, "queue")
            r = not [f for f in os.listdir(q) if f.startswith("t-")] if os.path.isdir(q) else True
            print(name + "queued synthesis ticket withdrawn: %s" % r)
            ok &= r
        else:
            os.rmdir(os.path.join(tmp, "hl.2"))           # a lane frees: the queued synthesis runs
            p.wait(600)
            rows = rows_of(rd)
            fin = [r for r in rows if r.get("syn_deferred")]
            r = len(fin) == 1 and fin[0]["status"] == "PASS" and (fin[0]["grade"].get("syn") or {}).get("syn_pass")
            print(name + "a lane frees -> queued synthesis PASS row: %s" % bool(r))
            ok &= bool(r)
            gl = subprocess.run(["git", "-C", os.path.join(rd, "repo"), "log", "--oneline"], capture_output=True, text=True).stdout
            r = "merge ce_frac.v (ce" in gl
            print(name + "job merged after the synthesis passed: %s" % r)
            ok &= r
            starts = [l for l in open(os.path.join(rd, "workers.jsonl")) if '"what": "start"' in l]
            r = len(starts) == 1 and len(rows) == 2
            print(name + "no worker restart (worker starts %d, results rows %d): %s" % (len(starts), len(rows), r))
            ok &= r
            sm = json.load(open(os.path.join(rd, "mistakes_summary.json")))
            r = sm["sessions"] == 1 and sm["passes"] == 1
            print(name + "run summary: 1 session, 1 pass (%s/%s): %s" % (sm["sessions"], sm["passes"], r))
            ok &= r
            res = json.load(open(os.path.join(rd, "project_result.json")))
            r = res["jobs"]["ce"]["state"] == "merged" and res["jobs"]["ce"]["attempts"] == 1
            print(name + "project result: merged on attempt 1: %s" % r)
            ok &= r
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
        os.killpg(llm.pid, signal.SIGTERM)
    if not ok:
        print(open(os.path.join(tmp, "coord.log")).read()[-3000:])
    shutil.rmtree(rd, ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "agent_ws", "proj_" + run_id), ignore_errors=True)
    return ok


def run_session(tmp, name, task, steps, during=None, extra=()):
    """One stub-engine steer_agent session of task against a scripted fake LLM. during(rd) is called once the worker
    has started. Returns (results row, decisions, rd); the caller removes rd."""
    os.makedirs(tmp, exist_ok=True)
    json.dump(steps, open(os.path.join(tmp, name + ".json"), "w"))
    port = free_port()
    llm = subprocess.Popen([sys.executable, os.path.join(HERE, "oc_probe.py"), "--serve-tcp", str(port), "--out",
                            os.path.join(tmp, name + "_llm"), "--script", os.path.join(tmp, name + ".json")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    run_id = "%s_%d" % (name, os.getpid())
    rd = os.path.join(BASE, "results", "steer_" + run_id)
    env = dict(os.environ, STEER_BC250_BASE=BASE, BAKEOFF_INFER_HOST="127.0.0.1", BAKEOFF_INFER_PORT=str(port),
               STEER_HEAVY_LOCKS=os.path.join(tmp, name + "_l0"), STEER_BOARD_LOCK_DIR=tmp)
    log = open(os.path.join(tmp, name + ".log"), "w")
    try:
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "steer_agent.py"), "--run-id", run_id, "--engine", "stub",
                              "--tasks", task, "--provide-tests", "--auto-done", "--timeout", "400", "--max-steers", "4",
                              "--grade-args=--no-syn"] + list(extra), env=env, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
        if during:
            t0 = time.time()
            wl = os.path.join(rd, "workers.jsonl")
            while time.time() - t0 < 120 and p.poll() is None and not (
                    os.path.exists(wl) and '"what": "start"' in open(wl).read()):
                time.sleep(0.5)
            during(rd)
        p.wait(900)
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
        os.killpg(llm.pid, signal.SIGTERM)
    rows = rows_of(rd)
    dec = []
    for f in glob.glob(os.path.join(rd, "decisions", "*.jsonl")):
        dec += [json.loads(l) for l in open(f) if l.strip()]
    return (rows[0] if rows else {}), dec, rd


def done_gate_session(tmp):
    """Auto-done must not trust a stale self-check: the workspace is built with lint_wall off, task.json turns it on
    while the worker runs; the worker's file has a -Wall warning and its self-check PASSes. The harness's own check
    (current task.json) blocks the auto-done and sends the warning; the worker then writes a clean file and passes."""
    name = "v_rebuild_zzgate_%d" % os.getpid()
    td = os.path.join(BASE, "tasks", name)
    shutil.copytree(os.path.join(BASE, "tasks", TASK), td)
    ok = True
    try:
        tj = json.load(open(os.path.join(td, "task.json")))
        tj.update(id=name, lint_wall=False)
        json.dump(tj, open(os.path.join(td, "task.json"), "w"), indent=1)
        gold = open(os.path.join(td, "golden", "ce_frac.v")).read()
        width = gold.replace("acc <= sum[W-1:0];", "acc <= sum;")            # simulation passes, -Wall WIDTHTRUNC
        steps = [{"tool": "shell", "args": {"command": "sleep 20"}},
                 {"tool": "write", "args": {"path": "ce_frac.v", "content": width}},
                 {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
                 {"text": "Done."},
                 {"tool": "write", "args": {"path": "ce_frac.v", "content": gold}},
                 {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
                 {"text": "Done."}]

        def flip(rd):
            t = json.load(open(os.path.join(td, "task.json")))
            t["lint_wall"] = True
            json.dump(t, open(os.path.join(td, "task.json"), "w"), indent=1)
        rec, dec, rd = run_session(tmp, "gate", name, steps, during=flip)
        blocked = [d for d in dec if d["trigger"].startswith("auto-done blocked")]
        r = bool(blocked) and "lint_wall" in blocked[0]["decision"]["message"] and "WIDTHTRUNC" in blocked[0]["decision"]["message"]
        print("done gate: a stale self-check PASS does not end the task; the -Wall warning is sent: %s" % r)
        ok &= r
        r = (rec.get("mistakes") or {}).get("done_gate_blocked", 0) >= 1
        print("done gate: done_gate_blocked counted: %s" % r)
        ok &= r
        dones = [d for d in dec if d["decision"]["action"] == "done"]
        r = bool(dones) and bool(blocked) and dones[0]["t"] >= blocked[0]["t"] and rec.get("pass") is True
        print("done gate: done only after the clean file, and the grade passes: %s (%s)" % (r, rec.get("detail")))
        ok &= r
        if not ok:
            print(open(os.path.join(tmp, "gate.log")).read()[-2000:])
        shutil.rmtree(rd, ignore_errors=True)
        shutil.rmtree(os.path.join(BASE, "agent_ws", "steer_gate_%d" % os.getpid()), ignore_errors=True)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    return ok


def edit_lock_session(tmp):
    """Edit thrash: after the first failed edit of the graded file in a turn, the next edit call is rejected at once:
    the turn is stopped, the file restored to its state before that edit, and the redirect says to write the whole file."""
    gold = open(os.path.join(BASE, "tasks", TASK, "golden", "ce_frac.v")).read()
    broken = "acc <= ~sum[W-1:0];"
    td = os.path.join(BASE, "tasks", TASK)
    f = os.path.join(tmp, "broken.v")
    os.makedirs(tmp, exist_ok=True)
    open(f, "w").write(gold.replace("acc <= sum[W-1:0];", broken))
    g = subprocess.run([sys.executable, os.path.join(BASE, "lib", "grade.py"), td, f, "--no-syn"], capture_output=True,
                       text=True, env=dict(os.environ, STEER_BC250_BASE=BASE, STEER_GRADE_SCRATCH=os.path.join(tmp, "gs")))
    ok = '"pass": false' in g.stdout
    print("edit lock: the rejected edit would break the simulation (precondition): %s" % ok)
    steps = [{"tool": "write", "args": {"path": "ce_frac.v", "content": gold}},
             {"tool": "edit", "args": {"path": "ce_frac.v", "oldString": "this text is not in the file", "newString": "x"}},
             {"tool": "edit", "args": {"path": "ce_frac.v", "oldString": "acc <= sum[W-1:0];", "newString": broken}},
             {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
             {"text": "Done."}]
    rec, dec, rd = run_session(tmp, "elock", TASK, steps)
    rej = [d for d in dec if d["trigger"] == "edit call on the graded file rejected"]
    r = len(rej) >= 1 and "write the whole file with Write now" in rej[0]["decision"]["message"]
    print("edit lock: the edit after the first failed one is rejected with 'write the whole file with Write now': %s" % r)
    ok &= r
    m = rec.get("mistakes") or {}
    r = m.get("edit_rejected", 0) >= 1 and m.get("failed_edits", 0) == 1 and rec.get("edit_rejections", 0) >= 1
    print("edit lock: counters failed_edits 1, edit_rejected %s: %s" % (m.get("edit_rejected"), r))
    ok &= r
    r = rec.get("pass") is True and broken not in open(rec["candidate"]).read()
    print("edit lock: the file was restored (no broken line) and the job passes: %s (%s)" % (r, rec.get("detail")))
    ok &= r
    if not ok:
        print(open(os.path.join(tmp, "elock.log")).read()[-2000:])
    shutil.rmtree(rd, ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "agent_ws", "steer_elock_%d" % os.getpid()), ignore_errors=True)
    return ok


def repeat_fail_session(tmp):
    """The same first failure on two self-check runs with no change between them, then a side file and a third run:
    one note only (rate-limited per first failure), quoting the FAIL line, sent as an uncounted redirect."""
    gold = open(os.path.join(BASE, "tasks", TASK, "golden", "ce_frac.v")).read()
    mut = open(os.path.join(BASE, "tasks", TASK, "mutants", "m1.v")).read()
    steps = [{"tool": "write", "args": {"path": "ce_frac.v", "content": mut}},
             {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
             {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
             {"tool": "shell", "args": {"command": "sleep 5"}},           # the turn is stopped here, or it runs again
             {"tool": "write", "args": {"path": "debug_tb.v", "content": "module debug_tb; endmodule\n"}},
             {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
             {"tool": "write", "args": {"path": "ce_frac.v", "content": gold}},
             {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
             {"text": "Done."}]
    rec, dec, rd = run_session(tmp, "rfail", TASK, steps)
    ok = True
    notes = [d for d in dec if d["trigger"] == "repeated first failure"]
    first = ""
    for f in glob.glob(os.path.join(rd, "events", "*.jsonl")):
        for l in open(f):
            m = re.search(r"FAIL: first mismatch at cycle \d+ \(sample point \d+\)[^\\\n\"]*", l)
            if m and not first:
                first = m.group(0)
    r = len(notes) == 1 and notes[0]["decision"]["action"] == "redirect" and bool(first) and first in notes[0]["decision"]["message"]
    print("repeat: exactly one note, a redirect quoting '%s': %s" % (first[:60], r))
    ok &= r
    m = rec.get("mistakes") or {}
    r = m.get("repeat_first_fail") == 1 and m.get("side_files", 0) >= 1 and rec.get("steers") == 0
    print("repeat: repeat_first_fail 1, side file removed, no steer counted (%s, %s, %s): %s" % (
        m.get("repeat_first_fail"), m.get("side_files"), rec.get("steers"), r))
    ok &= r
    r = rec.get("pass") is True
    print("repeat: the job passes: %s (%s)" % (r, rec.get("detail")))
    ok &= r
    if not ok:
        print(open(os.path.join(tmp, "rfail.log")).read()[-2000:])
    shutil.rmtree(rd, ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "agent_ws", "steer_rfail_%d" % os.getpid()), ignore_errors=True)
    return ok


def write_guard_session(tmp):
    """A write that drops the module, then one that shrinks the compiling file below half: each undone at once."""
    gold = open(os.path.join(BASE, "tasks", TASK, "golden", "ce_frac.v")).read()
    frag = "    // Unused bits sink\n    wire unused = &{1'b0, acc[0]};\n"
    lines = gold.split("\n")
    head = "\n".join(lines[:2] + lines[4:gold[:gold.index(");")].count("\n") + 1])
    half = head + "\nendmodule\n"
    ok = len(half) * 2 < len(gold) and "module ce_frac" in half
    print("write guard: the header-only file is under half the golden (precondition, %d vs %d bytes): %s" % (
        len(half), len(gold), ok))
    steps = [{"tool": "write", "args": {"path": "ce_frac.v", "content": gold}},
             {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
             {"tool": "write", "args": {"path": "ce_frac.v", "content": frag}},
             {"tool": "shell", "args": {"command": "sleep 5"}},            # the turn is stopped here
             {"tool": "write", "args": {"path": "ce_frac.v", "content": half}},
             {"tool": "shell", "args": {"command": "sleep 5"}},
             {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
             {"text": "Done."}]
    rec, dec, rd = run_session(tmp, "wguard", TASK, steps)
    g = [d for d in dec if d["trigger"] == "write guard: graded file write undone"]
    r = len(g) == 2 and all(d["decision"]["action"] == "redirect" for d in g)
    print("write guard: two writes undone, each with a redirect: %s (%d)" % (r, len(g)))
    ok &= r
    r = len(g) >= 1 and "dropped the module/endmodule pair" in g[0]["decision"]["message"] and \
        "git show best:ce_frac.v" in g[0]["decision"]["message"] and "\n" not in g[0]["decision"]["message"]
    print("write guard: the first note names the dropped module and `git show best:ce_frac.v`, one line: %s" % r)
    ok &= r
    r = len(g) == 2 and "less than half of a version that compiled" in g[1]["decision"]["message"]
    print("write guard: the second note names the shrink below half: %s" % r)
    ok &= r
    m = rec.get("mistakes") or {}
    r = m.get("write_guard") == 2 and not m.get("lint_errors") and not m.get("header_violations")
    print("write guard: write_guard 2, no lint/header notes from the undone writes (%s): %s" % (
        {k: m.get(k) for k in ("write_guard", "lint_errors", "header_violations")}, r))
    ok &= r
    r = rec.get("pass") is True and open(rec["candidate"]).read() == gold and rec.get("steers") == 0
    print("write guard: the restored file is the candidate, the job passes, no steer counted: %s (%s)" % (r, rec.get("detail")))
    ok &= r
    if not ok:
        print(open(os.path.join(tmp, "wguard.log")).read()[-2500:])
    shutil.rmtree(rd, ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "agent_ws", "steer_wguard_%d" % os.getpid()), ignore_errors=True)
    return ok


def queue_session(tmp):
    """A steer queued while the worker is mid-turn (a 90 s sleep) is escalated to a redirect within --steer-queue-max."""
    gold = open(os.path.join(BASE, "tasks", TASK, "golden", "ce_frac.v")).read()
    steps = [{"tool": "write", "args": {"path": "ce_frac.v", "content": gold}},
             {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
             {"tool": "shell", "args": {"command": "sleep 90"}},
             {"text": "Done."}]
    limit = 6
    steer = ("FAIL: output ce expected 1 got 0 (wrong bits 1) at cycle 12: fix the rule for ce on line 21 of "
             "ce_frac.v, then run `bash run_selfcheck.sh` and keep fixing until it prints RESULT: PASS.")
    stop = []

    def respond(rd):
        mb = os.path.join(rd, "mailbox", "gpt-oss-20b-MXFP4__%s__s0" % TASK)
        n = 0
        while not stop:
            for req in sorted(glob.glob(os.path.join(mb, "req_*.json"))):
                res = req.replace("req_", "res_")
                if os.path.exists(res):
                    continue
                try:
                    q = json.load(open(req))
                except ValueError:
                    continue
                n += 1
                d = {"action": "steer", "message": steer, "reasoning": "test: queue a steer", "evidence": ""} if n == 1 else \
                    {"action": "wait" if q["ctx"]["worker_running"] else "done", "message": "", "reasoning": "test", "evidence": ""}
                json.dump(d, open(res + ".tmp", "w"))
                os.rename(res + ".tmp", res)
            time.sleep(0.5)

    def during(rd):
        import threading
        threading.Thread(target=respond, args=(rd,), daemon=True).start()
    try:
        rec, dec, rd = run_session(tmp, "squeue", TASK, steps, during=during,
                                   extra=("--engine", "mailbox", "--mailbox-timeout", "30", "--watchdog", "1",
                                          "--steer-queue-max", str(limit)))
    finally:
        stop.append(1)
    q = [d for d in dec if d["decision"]["action"] == "steer" and not d["decision"].get("_auto")]
    e = [d for d in dec if d["trigger"] == "queued steer escalated to redirect"]
    ok = len(q) >= 1 and len(e) == 1 and q[0]["worker_running"]
    print("steer queue: the engine's steer was queued mid-turn and escalated once: %s" % ok)
    w = rec.get("queued_steer_waits") or []
    r = ok and len(w) == 1 and w[0]["delivered"] == "redirect" and limit <= w[0]["waited_s"] <= limit + 5 and \
        e[0]["t"] - q[0]["t"] <= limit + 5 and ("queued %d s without a turn end (limit %d s)" % (w[0]["waited_s"], limit)) \
        in e[0]["decision"]["reasoning"]
    print("steer queue: redirected after %s s (limit %d s), the wait logged in the decision and results: %s" % (
        w[0]["waited_s"] if w else None, limit, r))
    ok &= r
    r = rec.get("pass") is True
    print("steer queue: the job passes: %s (%s)" % (r, rec.get("detail")))
    ok &= r
    if not ok:
        print(open(os.path.join(tmp, "squeue.log")).read()[-2500:])
    shutil.rmtree(rd, ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "agent_ws", "steer_squeue_%d" % os.getpid()), ignore_errors=True)
    return ok


def main():
    import steer_agent as sa
    ok = True
    # H3: ranking -- a later first failing cycle beats fewer mismatches; a pass beats everything
    r = [sa.rank_key(500, 1200) > sa.rank_key(10, 300), sa.rank_key(0, None) > sa.rank_key(1, 99999),
         sa.rank_key(10, 300) > sa.rank_key(20, 300)]
    print("H3 ranking by first failing cycle: %s" % all(r))
    ok &= all(r)
    td = os.path.join(BASE, "tasks", TASK)
    gold = open(os.path.join(td, "golden", "ce_frac.v")).read()
    hdr = sa.module_header(gold, "ce_frac")
    r = hdr is not None and sa.module_header(gold.replace("output reg", "output wire", 1), "ce_frac") != hdr
    print("H10 header comparison: %s" % r)
    ok &= r
    tmp = tempfile.mkdtemp(prefix="steer_rules_")
    # H2: the grader writes the simulation verdict before synthesis, and none after a failed simulation
    env = dict(os.environ, STEER_BC250_BASE=BASE)
    for f, want in ((os.path.join(td, "golden", "ce_frac.v"), True), (os.path.join(td, "mutants", "m1.v"), False)):
        pf = os.path.join(tmp, "progress.json")
        if os.path.exists(pf):
            os.remove(pf)
        subprocess.run([sys.executable, os.path.join(BASE, "lib", "grade.py"), td, f, "--no-syn"],
                       env=dict(env, STEER_GRADE_PROGRESS=pf), capture_output=True)
        r = os.path.exists(pf) == want
        print("H2 progress file %s for %s: %s" % ("written" if want else "absent", os.path.basename(f), r))
        ok &= r
    # H7, H8, H10 in a steered session
    i = gold.index("always @(posedge clk)")
    unused = gold[:i] + "    localparam [3:0] SPARE_K = 4'd3;\n" + gold[i:]
    badhdr = gold.replace("output reg", "output wire", 1) if "output reg" in gold else gold.replace("input ", "input  wire ", 1)
    width = gold.replace("acc <= sum[W-1:0];", "acc <= sum;")                     # a -Wall WIDTHTRUNC warning
    nonascii = gold.replace("fractional clock enable:", "fractional clock enable \u2014", 1)
    leak = gold.replace("module ce_frac", "`default_nettype none\nmodule ce_frac", 1)
    nohdr = gold.split("\n", 1)[1]                                                # the SPDX line removed
    dead = gold.replace("    initial ce = 1'b0;\n", "    initial ce = 1'b0;\n    wire unused = &{1'b0, 28'd0};\n")
    disp = gold.replace("            acc <= sum[W-1:0];\n", "            acc <= sum[W-1:0];\n            $display(\"acc %0d\", acc);\n")
    steps = [{"text": "Here is the design:\n```verilog\n" + gold + "```"},
             {"tool": "edit", "args": {"path": "ce_frac.v", "oldString": "12: module ce_frac", "newString": "x"}},
             {"tool": "write", "args": {"path": "ce_frac.v", "content": badhdr}},
             {"tool": "write", "args": {"path": "ce_frac.v", "content": unused}},
             {"tool": "write", "args": {"path": "ce_frac.v", "content": width}},
             {"tool": "write", "args": {"path": "ce_frac.v", "content": nonascii}},
             {"tool": "write", "args": {"path": "ce_frac.v", "content": leak}},
             {"tool": "write", "args": {"path": "ce_frac.v", "content": nohdr}},
             {"tool": "write", "args": {"path": "ce_frac.v", "content": dead}},
             {"tool": "write", "args": {"path": "ce_frac.v", "content": disp}},
             {"tool": "write", "args": {"path": "ce_frac.v", "content": gold}},
             {"tool": "shell", "args": {"command": "bash run_selfcheck.sh"}},
             {"text": "Done."}]
    json.dump(steps, open(os.path.join(tmp, "s.json"), "w"))
    port = free_port()
    llm = subprocess.Popen([sys.executable, os.path.join(HERE, "oc_probe.py"), "--serve-tcp", str(port), "--out",
                            os.path.join(tmp, "llm"), "--script", os.path.join(tmp, "s.json")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    run_id = "rulestest_%d" % os.getpid()
    try:
        p = subprocess.run([sys.executable, os.path.join(HERE, "steer_agent.py"), "--run-id", run_id, "--engine", "stub",
                            "--tasks", TASK, "--provide-tests", "--auto-done", "--timeout", "400", "--max-steers", "4",
                            "--grade-args=--no-syn"],
                           env=dict(env, BAKEOFF_INFER_HOST="127.0.0.1", BAKEOFF_INFER_PORT=str(port),
                                    STEER_HEAVY_LOCKS=os.path.join(tmp, "l0")), capture_output=True, text=True, timeout=900)
    finally:
        os.killpg(llm.pid, signal.SIGTERM)
    rd = os.path.join(BASE, "results", "steer_" + run_id)
    rec = json.loads(open(os.path.join(rd, "results.jsonl")).readline())
    m = rec.get("mistakes") or {}
    msgs = [json.loads(l) for l in open(os.path.join(rd, "workers.jsonl")) if '"what": "start"' in l]
    for k in ("reply_code_no_write", "edit_line_prefix", "header_violations", "unused_params", "lint_wall_warnings",
              "non_ascii", "nettype_leak", "missing_header", "dead_code", "debug_left"):
        r = m.get(k, 0) >= 1
        print("%s counted: %s (%s)" % (k, r, m.get(k, 0)))
        ok &= r
    r = m.get("initial_blocks", 0) == 0 and m.get("lint_wall_warnings", 0) == 1
    print("allow_initial task: no initial_blocks count; UNUSEDPARAM not counted again as lint_wall_warnings: %s (%s, %s)" % (
        r, m.get("initial_blocks", 0), m.get("lint_wall_warnings", 0)))
    ok &= r
    r = rec["pass"]
    print("job passes after the notes: %s (%s)" % (r, rec.get("detail")))
    ok &= r
    if not ok:
        print(p.stdout[-1500:])
        for x in msgs:
            print("MSG:", x["message"][:200])
    shutil.rmtree(rd, ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "agent_ws", "steer_" + run_id), ignore_errors=True)
    ok &= done_gate_session(os.path.join(tmp, "gate"))
    ok &= edit_lock_session(os.path.join(tmp, "elock"))
    ok &= repeat_fail_session(os.path.join(tmp, "rfail"))
    ok &= write_guard_session(os.path.join(tmp, "wguard"))
    ok &= queue_session(os.path.join(tmp, "squeue"))
    ok &= syn_only_classification(os.path.join(tmp, "synC"))
    # SYN_PENDING: (a) the lane frees and the job merges, (b) the coordinator is stopped while synthesis waits
    runs = [(os.path.join(tmp, "synA"), False), (os.path.join(tmp, "synB"), True)]
    started = [(d, stop) + syn_pending_start(d, stop) for d, stop in runs]
    for d, stop, sp, sllm, srid, _ in started:
        ok &= syn_pending_finish(d, sp, sllm, srid, stop)
    shutil.rmtree(tmp, ignore_errors=True)
    print("HARNESS RULES TEST: %s" % ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
