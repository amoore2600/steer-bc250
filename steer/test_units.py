#!/usr/bin/env python3
"""test_units.py -- unit tests of the coordinator and gate rules (no board, no lanes, no LLM):
  gate: a lane wait over its budget cancels the queued ticket, kills the process group and removes the overlay;
  H9: one job per board (a second take fails, a dead holder's lock is taken over, release removes only our lock);
  holds: the first full-assembly gate failure and an INTEGRATION CONTRACT failure are held, a second ordinary one is not;
         release files retry / reopen / fail;
  H4: harness ERRORs do not count as attempts up to the limit;
  H3: a failed attempt's best snapshot becomes the next attempt's start;
  must_use: a partial select of a bus (Verilator 'Bits of signal are not used') counts as a use, an unused net fails.
  syn_pending: a queued synthesis FAIL counts as an attempt (synthesis error as feedback, resumed from the
               simulation-passing candidate); a harness result does not; a PASS merges the job.
  lint_wall / hygiene / pinned (grade.py --no-syn on scripted candidates in a copy of v_rebuild_ce_frac): a
               WIDTHTRUNC warning fails, a function argument hiding a module signal (VARHIDDEN) fails, the same line waived with a lint_off/lint_on pair passes, an unpaired lint_off
               fails, "lint_wall": false lets the warning through; a `default_nettype none leak, a non-ASCII character,
               a missing SPDX line, an initial block (allow_initial off) fail; a missing or narrower pinned name fails.
               Sections (copy of v_section_pwm): only the job's section counts. The worker's run_selfcheck.sh turns
               a simulation PASS with a -Wall warning into RESULT: FAIL with the warning as a FAIL line (section jobs:
               mapped to section_s<n>.v), and the ranking puts that run between a clean pass and a simulation failure.
  stale config: a task.json changed between two attempts reaches the second attempt's config, hygiene_check.py and job
               directory; a workspace built from the old config is reported by workspace_config_problems.
  retry feedback: a FAIL at lint_wall / hygiene / pinned / sim puts "the hidden grade failed at <stage>: <detail>" into
               the retry's first message and TASK.md; the post-simulation stages resume from the candidate.
  kat build: build_task.py on a copy of kat_debounce keeps golden/ and mutants/ byte for byte and
               writes the Repository rules paragraph (also: the paragraph follows the flags; kat sections).
  notes: the edit note says "write the whole file with Write now"; the second declaration note quotes the rule.
  dead code (lib/hygiene.py dead_code): a constant-only unused-bits sink and an empty `if (...) begin end` fail at
               stage hygiene, a real sink and `default: ;` pass, "allow_dead_code": true lets them through; every
               repository golden passes with its task's flags; the worker's self-check prints the problem.
  lint_warnings_raw: the grade record's raw Verilator warning count (DECLFILENAME of candidate.v) is named apart from
               the graded lint_wall.warnings.
  resources (fake gw_sh writing a *_syn_rsc.xml, private lanes): the DSP count is recorded; more DSP blocks than the
               golden's count in validation.json fails at stage resources, as many passes; "max_mult" raises the
               limit, "allow_more_dsp" records only, --no-resources records only; no golden count = unchecked; the
               worker's rules state the limit; a queued synthesis's resources FAIL is an attempt with DSP feedback.
  debug_left (lib/hygiene.py): `$display` / `$write` / `$monitor` / `$finish` in the graded code fails at stage hygiene
               (not in comments or strings, not `$writememh`), "allow_display": true lets it through; with task.json
               "lint_waivers" set, a lint_off pair for a rule outside it fails, a listed rule passes, an absent list
               keeps the old rule; every repository golden passes with its task's flags; the worker's self-check
               prints it, TASK.md states it, the settings reach hygiene_check.py.
  write guard (steer_agent.write_guard_problem): a write that drops module/endmodule, or shrinks a version that compiled
               to under half, is refused; section files must keep the marker line and some code.
  steer queue: --steer-queue-max defaults to 120 s; 0 falls back to --queue-max-s.
  heatguard.sh (a test config, fake thermal CSV, dummy process groups, no board): exits when its run's process group
               is gone, also when it was started inside that group; a pgid that is not a number, or no config, is refused.
  repeated first failure (steer_agent RepeatFailWatch): the same first failure on two runs with only comments changed
               gives exactly one note quoting the FAIL lines, the wrong bits and the spec lines; a first failure hit
               twice then a side file gives one note; different first failures, a code change or a PASS give none.
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("STEER_BC250_BASE") or os.path.dirname(HERE)
os.environ["STEER_BC250_BASE"] = BASE
sys.path[:0] = [HERE, BASE, os.path.join(BASE, "lib")]
results = []


def check(name, cond):
    results.append(bool(cond))
    print("%-62s %s" % (name, "ok" if cond else "FAIL"))


def test_gate_cancel(tmp):
    import kat_sections as KS
    fake = os.path.join(tmp, "fake_heavy_run.sh")
    log = os.path.join(tmp, "cancel.log")
    open(fake, "w").write("#!/bin/bash\nif [ \"$1\" = --cancel ]; then echo \"$@\" >> %s; exit 0; fi\n"
                          "echo '[queue] q901 waiting #1'\nsleep 600\n" % log)
    os.chmod(fake, 0o755)
    os.environ["STEER_TREE_CACHE"] = os.path.join(tmp, "cache")
    KS.CACHE = os.environ["STEER_TREE_CACHE"]
    # a synthetic kat-sections task: a skeleton and a pinned tree (materialize is replaced by a plain directory)
    td = os.path.join(tmp, "task")
    tree = os.path.join(tmp, "tree")
    os.makedirs(td)
    os.makedirs(os.path.join(tree, "rtl"))
    open(os.path.join(td, "skeleton.v"), "w").write("module top;\n    // SECTION 1: s\n    // END SECTION 1\nendmodule\n")
    cfg = {"id": "ksec_unit", "top": "top", "golden_files": ["top.v"], "language": "verilog",
           "kat": {"mode": "sections", "integration": {"cmd": ["sh", "-c", "echo 'GATE: PASS'"], "heavy_run": fake,
                                                       "wait_budget": 3, "at_section": 1}}}
    mat = KS.materialize
    KS.materialize = lambda c: tree
    t0 = time.time()
    try:
        ok, detail = KS.gate(cfg, td, os.path.join(td, "skeleton.v"), tmp, None)
    finally:
        KS.materialize = mat
    check("gate: over its lane budget -> harness result", (not ok) and detail.startswith("HARNESS") and time.time() - t0 < 60)
    check("gate: the queued ticket was cancelled", os.path.exists(log) and "q901" in open(log).read())
    left = subprocess.run(["pgrep", "-x", "sleep", "-a"], capture_output=True, text=True).stdout
    check("gate: overlay removed", not os.listdir(os.path.join(KS.CACHE, "gate")))
    return left


def test_boards(tmp):
    import coordinator as C
    C.BOARD_LOCKS = tmp
    check("H9: take a free board", C.take_board("bX", "test a"))
    check("H9: a second take of the same board fails", not C.take_board("bX", "test b"))
    open(os.path.join(C.board_lock("bY") if os.makedirs(C.board_lock("bY")) is None else "", "owner"), "w").write(
        "old run pid 999999 10:00:00\n")
    check("H9: a lock whose holder is gone is taken over", C.take_board("bY", "test c"))
    os.makedirs(C.board_lock("bZ"))
    open(os.path.join(C.board_lock("bZ"), "owner"), "w").write("other pid %d 10:00:00\n" % os.getppid())
    C.release_board("bZ")
    check("H9: release leaves another process's lock", os.path.isdir(C.board_lock("bZ")))
    C.release_board("bX")
    check("H9: release removes our own lock", not os.path.isdir(C.board_lock("bX")))


def test_holds_and_attempts(tmp):
    import coordinator as C
    pdir = os.path.join(tmp, "proj")
    os.makedirs(pdir)
    json.dump({"name": "u", "goal": "unit", "jobs": [{"id": "s7", "task": "v_section_pwm", "section": 3},
                                                    {"id": "s6", "task": "v_section_pwm", "section": 2},
                                                    {"id": "ce", "task": "v_rebuild_ce_frac"}]},
              open(os.path.join(pdir, "project.json"), "w"))
    rd = os.path.join(tmp, "rd")
    os.makedirs(os.path.join(rd, "jobs"))
    P = C.Project(pdir, rd)
    # the integration-gate settings of a kat-sections task (none of the example tasks has a gate)
    P.task_cfg["s7"]["kat"] = {"mode": "sections", "integration": {"hold_first_fail": True}}
    g = {"stage": "integration", "integration": "WRAPPER GATE: FAIL (pads)", "likely_section": 6, "attribution": "section 6"}
    r1 = P.hold_reason("s7", {"grade": g})
    r2 = P.hold_reason("s7", {"grade": g})
    r3 = P.hold_reason("s7", {"grade": dict(g, attribution="claude", integration="INTEGRATION CONTRACT $fatal")})
    check("hold: the first full-assembly gate failure is held", r1)
    check("hold: a second ordinary gate failure is not", r2 is None)
    check("hold: an INTEGRATION CONTRACT failure is always held", r3)
    for act, want in (("retry", ("retry", None)), ("reopen s6", ("pending", "retry")), ("fail", ("failed", None))):
        P.jobs["s7"].update(state="held", attempts=1)
        P.jobs["s6"].update(state="merged", attempts=1)
        P.hold("s7", {"grade": g, "candidate": "x"}, "test")
        open(os.path.join(rd, "hold", "s7.release"), "w").write(act + "\n")
        P.poll_holds()
        got = (P.jobs["s7"]["state"], P.jobs["s6"]["state"] if want[1] else None)
        check("hold release '%s' -> %s" % (act, want), got == want)
    j = P.jobs["ce"]
    j.update(attempts=1, state="working")
    P.after_failure("ce", {"status": "ERROR"}, 1, 2)
    P.jobs["ce"]["attempts"] += 1
    P.after_failure("ce", {"status": "ERROR"}, 1, 2)
    check("H4: two harness ERRORs do not use up the attempt", j["state"] == "retry")
    j["attempts"] += 1
    P.after_failure("ce", {"status": "ERROR"}, 1, 2)
    check("H4: the third ERROR counts", j["state"] == "failed")
    P.after_failure("ce", {"status": "FAIL", "best_snapshot": "/x/best.v", "best_mismatches": 7, "best_first_fail_cycle": 90}, 2, 2)
    check("H3: a failed attempt hands its best snapshot to the next", j.get("resume_from") == "/x/best.v")


def test_resume(tmp):
    import run_api, run_agent
    import steer_agent as sa
    cfg = run_api.load_tasks(["v_rebuild_ce_frac"], False)[0]
    mut = open(os.path.join(cfg["dir"], "mutants", "m1.v")).read()
    snap = os.path.join(tmp, "best.v")
    open(snap, "w").write(mut)
    ws = os.path.join(tmp, "ws")
    out = run_agent.build_workspace(cfg, ws, None, provide_tests=True)
    cfg = dict(cfg, resume_from=snap, resume_note="7 mismatches")
    # the resume block of run_task, applied the same way
    shutil.copy(snap, os.path.join(ws, out))
    t = open(os.path.join(ws, "TASK.md")).read()
    check("H3: resume snapshot is a header-compatible start",
          sa.module_header(mut, cfg["top"]) == sa.module_header(open(os.path.join(cfg["dir"], "golden", out)).read(), cfg["top"]))


def test_must_use_bits(tmp):
    import kat_sections as KS
    td = os.path.join(tmp, "task")
    os.makedirs(os.path.join(td, "tree"))
    open(os.path.join(td, "skeleton.v"), "w").write(
        "module t(input clk, input [15:0] a, output reg [7:0] y);\n    // SECTION 1: s\n    // (write this section here)\n"
        "    // END SECTION 1\nendmodule\n")
    cfg = {"id": "t", "top": "t", "golden_files": ["t.v"], "language": "verilog", "kat": {},
           "lint": {"top": "t", "flags": ["-Wno-DECLFILENAME"]}}
    load, mat = KS.load_spec, KS.materialize
    KS.load_spec = lambda c, d: {1: {"instances": [], "assigns": {}, "must_drive": [], "must_use": ["a"], "after": []}}
    KS.materialize = lambda c: os.path.join(td, "tree")
    try:
        for name, body, want in (("[7:0] select of a 16-bit net passes must_use", "    always @(posedge clk) y <= a[7:0];\n", True),
                                 ("a fully unused net fails must_use", "    always @(posedge clk) y <= 8'd0;\n", False)):
            W = os.path.join(tmp, "w_%d" % want)
            os.makedirs(W)
            cand = os.path.join(W, "cand.v")
            open(cand, "w").write("    // SECTION 1: s\n" + body + "    // END SECTION 1\n")
            res = Res()
            ok = KS.grade_section(cfg, td, W, cand, 1, res, None)
            check("must_use: " + name, ok == want and (want or "a is not used" in (res.get("detail") or "")))
    finally:
        KS.load_spec, KS.materialize = load, mat


def test_syn_finish(tmp):
    import coordinator as C
    pdir = os.path.join(tmp, "proj")
    os.makedirs(pdir)
    json.dump({"name": "u", "goal": "unit", "jobs": [{"id": "ce", "task": "v_rebuild_ce_frac"}]},
              open(os.path.join(pdir, "project.json"), "w"))
    rd = os.path.join(tmp, "rd")
    os.makedirs(os.path.join(rd, "jobs"))
    P = C.Project(pdir, rd)
    cand = os.path.join(tmp, "cand.v")
    shutil.copy(os.path.join(BASE, "tasks", "v_rebuild_ce_frac", "golden", "ce_frac.v"), cand)
    rec = {"task": "v_rebuild_ce_frac", "sample": 0, "job": "ce", "candidate": cand, "best_snapshot": cand,
           "status": "SYN_PENDING"}
    j = P.jobs["ce"]
    fail = {"pass": False, "stage": "syn", "detail": "Gowin synthesis failed: rc=1 ERROR (EX1234) : bad"}
    j.update(attempts=1, state="syn_pending")
    row = P.finish_syn("ce", rec, fail, 2, 2)
    check("syn_pending: a synthesis FAIL is a FAIL row", row["status"] == "FAIL" and row["syn_deferred"])
    check("syn_pending: ... counted as an attempt (retry, attempts 1)", j["state"] == "retry" and j["attempts"] == 1)
    check("syn_pending: ... synthesis error as feedback", "EX1234" in (j.get("feedback") or ""))
    check("syn_pending: ... resumed from the passing candidate", j.get("resume_from") == cand)
    j.update(attempts=1, state="syn_pending")
    rfail = {"pass": False, "stage": "resources", "detail": "Gowin synthesis uses 5 DSP block(s) (MULTALU27X18 x5), the "
             "golden uses 1"}
    row = P.finish_syn("ce", rec, rfail, 2, 2)
    check("syn_pending: a resources FAIL is a FAIL row, an attempt, with DSP feedback", row["status"] == "FAIL" and
          j["state"] == "retry" and "too many DSP blocks" in (j.get("feedback") or "") and "x5" in j["feedback"])
    j.update(attempts=2, state="syn_pending")
    P.finish_syn("ce", rec, fail, 2, 2)
    check("syn_pending: a synthesis FAIL on the last attempt fails the job", j["state"] == "failed")
    j.update(attempts=2, state="syn_pending", harness_errors=0)
    row = P.finish_syn("ce", rec, {"pass": False, "stage": "harness", "detail": "no verdict"}, 2, 2)
    check("syn_pending: a harness result is an ERROR, not an attempt", row["status"] == "ERROR" and j["state"] == "retry")
    j.update(attempts=1, state="syn_pending")
    row = P.finish_syn("ce", rec, {"pass": True, "stage": "done", "detail": "ok"}, 2, 2)
    check("syn_pending: a synthesis PASS merges the job", row["status"] == "PASS" and j["state"] == "merged"
          and os.path.exists(os.path.join(P.repo, "ce_frac.v")))


def copy_task(tmp, name, dst=None, **cfg_update):
    td = os.path.join(tmp, dst or name)
    shutil.copytree(os.path.join(BASE, "tasks", name), td)
    tj = os.path.join(td, "task.json")
    cfg = json.load(open(tj))
    cfg.update(cfg_update)
    for k in [k for k, v in cfg_update.items() if v is None]:
        cfg.pop(k)
    json.dump(cfg, open(tj, "w"), indent=1)
    return td


def grade_text(td, text, tag, *extra):
    f = os.path.join(td, "cand_%s.v" % tag)
    open(f, "w").write(text)
    p = subprocess.run([sys.executable, os.path.join(BASE, "lib", "grade.py"), td, f, "--no-syn"] + list(extra),
                       capture_output=True, text=True, timeout=900, env=dict(os.environ, STEER_GRADE_SCRATCH=os.path.join(td, "gs")))
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"pass": None, "stage": "unreadable", "detail": (p.stdout + p.stderr)[-300:]}


def test_lint_wall(tmp):
    td = copy_task(tmp, "v_rebuild_ce_frac", lint_wall=True, allow_initial=True)
    gold = open(os.path.join(td, "golden", "ce_frac.v")).read()
    width = gold.replace("acc <= sum[W-1:0];", "acc <= sum;")
    waived = gold.replace("            acc <= sum[W-1:0];\n", "            /* verilator lint_off WIDTHTRUNC */\n"
                          "            acc <= sum;\n            /* verilator lint_on WIDTHTRUNC */\n")
    unpaired = gold.replace("            acc <= sum[W-1:0];\n", "            // verilator lint_off WIDTHTRUNC\n            acc <= sum;\n")
    leak = gold.replace("module ce_frac", "`default_nettype none\nmodule ce_frac", 1)
    # a function argument named like a module signal (VARHIDDEN)
    hidden = gold.replace("    initial ce = 1'b0;\n", "    initial ce = 1'b0;\n    function [W-1:0] wrap(input [W-1:0] acc);\n"
                          "        wrap = acc;\n    endfunction\n").replace("acc <= sum[W-1:0];", "acc <= wrap(sum[W-1:0]);")
    cases = [("golden passes", gold, ("done", True)),
             ("a WIDTHTRUNC warning fails at lint_wall", width, ("lint_wall", False, "WIDTHTRUNC")),
             ("the same line waived with lint_off/lint_on passes", waived, ("done", True)),
             ("a lint_off without lint_on fails", unpaired, ("lint_wall", False, "no matching")),
             ("a function argument hiding a module signal fails (VARHIDDEN)", hidden, ("lint_wall", False, "VARHIDDEN")),
             ("`default_nettype none without wire at the end fails", leak, ("hygiene", False, "nettype_leak")),
             ("... and passes with `default_nettype wire after endmodule", leak + "`default_nettype wire\n", ("done", True)),
             ("a non-ASCII character fails", gold.replace("fractional clock enable:", "fractional clock enable \u2014", 1),
              ("hygiene", False, "non_ascii")),
             ("a missing SPDX line fails", gold.split("\n", 1)[1], ("hygiene", False, "missing_header"))]
    for name, text, want in cases:
        g = grade_text(td, text, str(len(results)))
        ok = (g.get("stage"), g.get("pass")) == want[:2] and (len(want) < 3 or want[2] in (g.get("detail") or ""))
        check("lint_wall/hygiene: " + name, ok)
        if not ok:
            print("    got", g.get("stage"), g.get("pass"), (g.get("detail") or "")[:200])
    g = grade_text(td, width, "w2")
    check("lint_wall: the warning is recorded (count and first line)",
          (g.get("lint_wall") or {}).get("warnings") == 1 and "WIDTHTRUNC" in ((g.get("lint_wall") or {}).get("first") or ""))
    off = copy_task(tmp, "v_rebuild_ce_frac", "off", lint_wall=False, allow_initial=True)
    g = grade_text(off, width, "off")
    check("lint_wall: \"lint_wall\": false lets the warning pass", g.get("pass") is True)
    ini = copy_task(tmp, "v_rebuild_ce_frac", "ini", lint_wall=True, allow_initial=None)
    g = grade_text(ini, gold, "ini")
    check("hygiene: an initial block fails (no allow_initial)", g.get("stage") == "hygiene" and "initial_blocks" in g.get("detail", ""))
    decl = gold.replace("    output reg  ce\n", "    output reg  ce = 1'b0\n").replace("    initial ce = 1'b0;\n", "")
    g = grade_text(ini, decl, "decl")
    check("hygiene: ... a declaration initialiser instead passes", g.get("pass") is True)
    pin = copy_task(tmp, "v_rebuild_ce_frac", "pin", lint_wall=True, allow_initial=True,
                    pinned_names=[{"name": "acc", "width": 18}, {"name": "sum", "width": 19}])
    g = grade_text(pin, gold, "pg")
    check("pinned: the golden has acc [17:0] and sum [18:0]", g.get("pass") is True)
    g = grade_text(pin, re.sub(r"\bacc\b", "acc_q", gold), "pr")
    check("pinned: a renamed acc fails with the spec message", g.get("stage") == "pinned" and
          "internal signal acc [17:0] required by the spec is missing or has the wrong width" in g.get("detail", ""))
    g = grade_text(pin, gold.replace("wire [W:0] sum", "wire [W+1:0] sum"), "pw")
    check("pinned: a wider sum fails (recorded under pinned)", g.get("pass") is False and
          any("sum [18:0]" in x for x in g.get("pinned") or []))
    return td, pin


def test_lint_wall_sections(tmp):
    td = copy_task(tmp, "v_section_pwm", lint_wall=True)
    gf = os.path.join(td, "golden", "pwm_gen.v")
    # the copy's golden gets a -Wall warning in SECTION 1 (a 5-bit sum into 4 bits: WIDTHTRUNC), the same function
    gold = open(gf).read().replace("pre_cnt <= pre_cnt + 4'd1;", "pre_cnt <= pre_cnt + 5'd1;")
    open(gf, "w").write(gold)
    g3 = grade_text(td, gold, "s3", "--section=3")
    g1 = grade_text(td, gold, "s1", "--section=1")
    check("sections: s3 passes (the golden's -Wall warnings are outside it)", g3.get("pass") is True)
    check("sections: s1 fails at lint_wall (warnings inside the section)", g1.get("stage") == "lint_wall")
    return td


def test_selfcheck_hygiene(tmp, td, pin):
    import run_agent
    import steer_agent as sa
    gold = open(os.path.join(td, "golden", "ce_frac.v")).read()

    def ws_of(tdir, name, cfg_extra=None):
        cfg = json.load(open(os.path.join(tdir, "task.json")))
        cfg.update(dir=tdir, prompt=open(os.path.join(tdir, "prompt.md")).read(), **(cfg_extra or {}))
        ws = os.path.join(tmp, name)
        out = run_agent.build_workspace(cfg, ws, None, provide_tests=True)
        sa.install_snapshot_wrapper(cfg, ws, out)
        return ws, out

    def run_sc(ws, out, text):
        os.chmod(os.path.join(ws, out), 0o644)
        open(os.path.join(ws, out), "w").write(text)
        r = subprocess.run(["bash", "run_selfcheck.sh"], cwd=ws, capture_output=True, text=True, timeout=600)
        o = r.stdout + r.stderr
        ev = {"kind": "tool_use", "tool": "bash", "call": "$ bash run_selfcheck.sh", "ok": True, "out": o[-600:],
              "fail_lines": sa.selfcheck_fail_lines(o)}
        return r.returncode, o, ev
    ws, out = ws_of(td, "ws_rb")
    check("self-check: hygiene_check.py installed read-only", os.path.exists(os.path.join(ws, "hygiene_check.py")) and
          not os.access(os.path.join(ws, "hygiene_check.py"), os.W_OK))
    rc, o, ev_clean = run_sc(ws, out, gold)
    check("self-check: the golden -> RESULT: PASS", rc == 0 and re.search(r"(?m)^RESULT: PASS", o))
    rc, o, ev_w = run_sc(ws, out, gold.replace("acc <= sum[W-1:0];", "acc <= sum;"))
    check("self-check: a -Wall warning -> FAIL line with its line, RESULT: FAIL", rc != 0 and
          re.search(r"(?m)^FAIL: lint -Wall: %Warning-WIDTHTRUNC: ce_frac\.v:\d+:", o) and
          re.search(r"(?m)^RESULT: FAIL \(lint/file checks; everything else passed\)", o) and not re.search(r"(?m)^RESULT: PASS", o))
    mut = open(os.path.join(td, "mutants", "m1.v")).read()
    rc, o, ev_m = run_sc(ws, out, mut)
    s_w, h_w = sa.selfcheck_score(ev_w), sa.selfcheck_hygiene(ev_w)
    k_clean = sa.rank_key(sa.selfcheck_score(ev_clean), None, sa.selfcheck_hygiene(ev_clean))
    k_w = sa.rank_key(s_w, sa.first_fail_cycle(ev_w), h_w)
    k_m = sa.rank_key(sa.selfcheck_score(ev_m), sa.first_fail_cycle(ev_m), sa.selfcheck_hygiene(ev_m))
    check("self-check: the -Wall run scores 0 mismatches with 1 lint/file problem", (s_w, h_w) == (0, 1))
    check("ranking: clean pass > simulation pass with a warning > simulation failure", k_clean > k_w > k_m)
    check("pass evidence: none from the -Wall run", not sa.pass_evidence_of(ev_w) and sa.pass_evidence_of(ev_clean))
    ws2, out2 = ws_of(pin, "ws_pin")
    rc, o, _ = run_sc(ws2, out2, re.sub(r"\bacc\b", "acc_q", gold))
    check("self-check: a missing pinned name is a FAIL line", rc != 0 and
          "FAIL: internal signal acc [17:0] required by the spec is missing or has the wrong width" in o)


def test_selfcheck_section(tmp, td):
    import run_agent
    import steer_agent as sa
    SEC = run_agent.SEC
    base = json.load(open(os.path.join(td, "task.json")))
    base.update(dir=td, prompt=open(os.path.join(td, "prompt.md")).read())
    gold = open(os.path.join(td, "golden", "pwm_gen.v")).read()
    for n, want in ((1, False), (3, True)):
        cfg = run_agent.section_cfg(base, n)
        ws = os.path.join(tmp, "ws_s%d" % n)
        out = run_agent.build_workspace(cfg, ws, None, provide_tests=True, section_file=True)
        sa.install_snapshot_wrapper(cfg, ws, out)
        os.chmod(os.path.join(ws, out), 0o644)
        open(os.path.join(ws, out), "w").write(SEC.section_file_text(gold, n))
        r = subprocess.run(["bash", "run_selfcheck.sh"], cwd=ws, capture_output=True, text=True, timeout=600)
        o = r.stdout + r.stderr
        if want:
            check("section self-check: s3's golden body -> RESULT: PASS", r.returncode == 0 and re.search(r"(?m)^RESULT: PASS", o))
        else:
            check("section self-check: s1's warning is a FAIL line mapped to section_s1.v", r.returncode != 0 and
                  re.search(r"(?m)^FAIL: lint -Wall: %Warning-WIDTH\w+: section_s1\.v:\d+:", o))


def test_stale_config(tmp):
    """Item: a task.json changed while a project runs takes effect at the next attempt (workspace and grader read the
    same file), and a workspace built from a stale config is caught before the worker starts."""
    import coordinator as C
    import run_agent
    import steer_agent as sa
    pdir = os.path.join(tmp, "proj")
    os.makedirs(pdir)
    json.dump({"name": "u", "goal": "unit", "jobs": [{"id": "ce", "task": "v_rebuild_ce_frac"}]},
              open(os.path.join(pdir, "project.json"), "w"))
    rd = os.path.join(tmp, "rd")
    os.makedirs(os.path.join(rd, "jobs"))
    P = C.Project(pdir, rd)
    src = copy_task(tmp, "v_rebuild_ce_frac", "src_task", lint_wall=False, allow_initial=True)
    P.task_cfg["ce"]["dir"] = src                       # the job's task directory, changed under the project below
    jc1 = P.prepare_job("ce")
    ws1 = os.path.join(tmp, "ws1")
    run_agent.build_workspace(jc1, ws1, None, provide_tests=True)
    hc = lambda ws: open(os.path.join(ws, "hygiene_check.py")).read()
    check("stale: attempt 1 built with lint_wall false (workspace + check)",
          jc1.get("lint_wall") is False and "'lint_wall': False" in hc(ws1) and sa.workspace_config_problems(jc1, ws1) == [])
    tj = json.load(open(os.path.join(src, "task.json")))
    tj["lint_wall"] = True
    json.dump(tj, open(os.path.join(src, "task.json"), "w"), indent=1)
    check("stale: attempt 1's workspace is now flagged against the changed task.json",
          any("lint_wall" in x for x in sa.workspace_config_problems(dict(jc1, dir=src), ws1)))
    jc2 = P.prepare_job("ce")
    ws2 = os.path.join(tmp, "ws2")
    run_agent.build_workspace(jc2, ws2, None, provide_tests=True)
    jd_tj = json.load(open(os.path.join(P.job_dir("ce"), "task.json")))
    check("stale: attempt 2 takes the change (config, hygiene_check.py, job dir task.json)",
          jc2.get("lint_wall") is True and "'lint_wall': True" in hc(ws2) and jd_tj.get("lint_wall") is True
          and sa.workspace_config_problems(jc2, ws2) == [])
    check("stale: the project's cached config follows, its dir stays the task's own",
          P.task_cfg["ce"].get("lint_wall") is True and P.task_cfg["ce"]["dir"] == src)
    ws3 = os.path.join(tmp, "ws3")
    run_agent.build_workspace(dict(jc1, dir=P.job_dir("ce")), ws3, None, provide_tests=True)
    check("stale: a workspace built from the old config fails the check",
          sa.workspace_config_problems(dict(jc1, dir=P.job_dir("ce")), ws3) != [])
    return P, src


def test_retry_feedback(tmp, P, src):
    """Item: a retry's first message carries the previous grade's stage and detail (lint_wall, hygiene, pinned, sim),
    and a post-simulation failure resumes from that attempt's candidate."""
    import steer_agent as sa
    cand = os.path.join(tmp, "cand.v")
    shutil.copy(os.path.join(src, "golden", "ce_frac.v"), cand)
    j = P.jobs["ce"]
    cases = [("lint_wall", "verilator -Wall: 1 warning(s), the first: %Warning-UNUSEDSIGNAL: candidate.v:25:17: Bits of "
                           "signal are not used: 'j'[31:9]", "ce_frac.v:25:17", True),
             ("hygiene", "non_ascii: line 66 has a non-ASCII character (U+2011); use plain ASCII only", "U+2011", True),
             ("pinned", "internal signal acc [17:0] required by the spec is missing or has the wrong width", "acc [17:0]", True),
             ("sim", "seed 2: MISMATCH cycle 53 ce exp 1 got 0", "MISMATCH cycle 53", False)]
    for stage, detail, key, resume in cases:
        j.update(attempts=1, state="working", resume_from=None)
        P.after_failure("ce", {"status": "FAIL", "candidate": cand, "grade": {"stage": stage, "detail": detail}}, 2, 2)
        jc = P.prepare_job("ce")
        first = sa.first_message("ce_frac.v", jc.get("retry_feedback"))
        check("retry feedback (%s): first message says 'the hidden grade failed at %s: ...'" % (stage, stage),
              ("the hidden grade failed at %s: " % stage) in first and key in first and first.startswith("(harness)"))
        check("retry feedback (%s): TASK prompt carries it too" % stage, "## Previous attempt" in jc["prompt"] and key in jc["prompt"])
        check("retry feedback (%s): %s" % (stage, "resumed from the candidate" if resume else "not resumed from the candidate"),
              (j.get("resume_from") == cand) == resume)
    j.update(attempts=1, state="working")
    P.after_failure("ce", {"status": "ERROR", "grade": {"stage": "harness", "detail": "x"}}, 2, 2)
    check("retry feedback: none after a harness ERROR", P.prepare_job("ce").get("retry_feedback") is None)
    check("first message: no retry note on a first attempt", not sa.first_message("x.v").startswith("(harness)"))


def test_kat_build(tmp):
    """Item: kat prompts carry the generated Repository rules paragraph; build_task keeps golden/ and mutants/."""
    import hygiene as H

    def snap(d):
        return {os.path.join(r, f): open(os.path.join(r, f), "rb").read() for r, _, fs in os.walk(d) for f in fs}
    for name in ("kat_debounce",):
        td = copy_task(tmp, name, "b_" + name)
        before = {k: snap(os.path.join(td, k)) for k in ("golden", "mutants")}
        p = subprocess.run([sys.executable, os.path.join(BASE, "lib", "build_task.py"), td], capture_output=True, text=True,
                           timeout=300)
        after = {k: snap(os.path.join(td, k)) for k in ("golden", "mutants")}
        check("kat build %s: builds" % name, p.returncode == 0)
        check("kat build %s: golden/ and mutants/ unchanged (%d, %d files)" % (name, len(before["golden"]), len(before["mutants"])),
              before == after and before["mutants"])
        pr = open(os.path.join(td, "prompt.md")).read()
        check("kat build %s: prompt has the Repository rules paragraph" % name,
              "Repository rules (checked)" in pr and "`default:` branch" in pr and "wire unused = &{1'b0," in pr
              and "`default_nettype wire" in pr and "ASCII only" in pr and "Also required" not in pr)
        check("kat build %s: the repository's prompt.md is the rebuilt one" % name,
              pr == open(os.path.join(BASE, "tasks", name, "prompt.md")).read())
    cfg = json.load(open(os.path.join(BASE, "tasks", "kat_debounce", "task.json")))
    off = H.repository_rules(dict(cfg, lint_wall=False, allow_initial=True,
                                  pinned_names=[{"name": "c6", "width": 3}]), os.path.join(BASE, "tasks", "kat_debounce"))
    check("rules paragraph follows the flags (no lint, no initial rule, pinned names)",
          "verilator" not in off and "initial" not in off and "`c6` [2:0]" in off)
    ks = dict(cfg, kat={"mode": "sections"}, sections=[{"n": 1, "name": "s"}])
    sec = H.repository_rules(ks, os.path.join(BASE, "tasks", "kat_debounce"), "the section check passes")
    check("rules paragraph for kat sections: section lines, no header/footer",
          "on your section's lines" in sec and "Footer" not in sec and "After the section check passes" in sec)


def test_dead_code(tmp):
    """Item: dead code fails at stage hygiene (grade and the pre-gate's check), unless allow_dead_code."""
    import hygiene as H
    td = copy_task(tmp, "v_rebuild_ce_frac", "dc", lint_wall=True, allow_initial=True)
    gold = open(os.path.join(td, "golden", "ce_frac.v")).read()
    sink_c = gold.replace("    initial ce = 1'b0;\n", "    initial ce = 1'b0;\n    wire unused = &{1'b0, 28'd0};\n")
    sink_r = gold.replace("    initial ce = 1'b0;\n", "    initial ce = 1'b0;\n    wire unused = &{1'b0, sum[W], 4'd0};\n")
    empty_if = gold.replace("        if (reset) begin\n", "        if (ce) begin end\n        if (reset) begin\n")
    for name, text, want in (("a constant-only unused sink fails", sink_c, ("hygiene", False, "dead_code")),
                             ("an empty `if (...) begin end` fails", empty_if, ("hygiene", False, "dead_code")),
                             ("a real sink (signals and a constant) passes", sink_r, ("done", True))):
        g = grade_text(td, text, "dc%d" % len(results))
        ok = (g.get("stage"), g.get("pass")) == want[:2] and (len(want) < 3 or want[2] in (g.get("detail") or ""))
        check("dead code: " + name, ok)
        if not ok:
            print("    got", g.get("stage"), g.get("pass"), (g.get("detail") or "")[:200])
    g = grade_text(td, sink_c, "dcr")
    check("dead code: counted in the grade record (hygiene.dead_code)", (g.get("hygiene") or {}).get("dead_code") == 1)
    allow = copy_task(tmp, "v_rebuild_ce_frac", "dc_allow", lint_wall=True, allow_initial=True, allow_dead_code=True)
    g = grade_text(allow, sink_c, "dca")
    check("dead code: \"allow_dead_code\": true lets it pass", g.get("pass") is True)
    code = H.strip_comments_keep_lines
    cases = [("replicated constants", "wire unused_ok = &{4{1'b0}, 2'b11};", True),
             ("assign form, OR reduction", "assign unused = |{8'hFF, 'd3};", True),
             ("a parameter name counts as a name", "wire unused = &{1'b0, W};", False),
             ("a bit-slice of a signal", "wire unused = &{1'b0, a[7:4]};", False),
             ("an ordinary reduction is not a sink", "wire all_set = &{1'b1, 1'b1};", False),
             ("a named empty block", "always @(posedge clk) begin : idle end", True),
             ("an empty block across lines with a comment", "if (x) begin\n  // nothing\nend", True),
             ("`default: ;` is not a block", "case (s)\n 1'b0: y <= 1'b1;\n default: ;\nendcase", False)]
    for name, text, bad in cases:
        check("dead code (unit): %s -> %s" % (name, "found" if bad else "none"), bool(H.dead_code(code(text))) == bad)
    check("dead code (unit): only lines inside span count",
          H.dead_code(code("x;\nwire unused = &{1'b0};\n"), (1, 1)) == [] and H.dead_code(code("x;\nwire unused = &{1'b0};\n"), (2, 2)))
    bad = []
    import glob
    for tj in sorted(glob.glob(os.path.join(BASE, "tasks", "*", "task.json"))):
        cfg = json.load(open(tj))
        if not H.applies(cfg):
            continue
        for gf in glob.glob(os.path.join(os.path.dirname(tj), "golden", "*.v")) + glob.glob(os.path.join(os.path.dirname(tj), "golden", "*.sv")):
            hp = H.hygiene_problems(open(gf, errors="replace").read(), allow_initial=True,
                                    allow_dead_code=bool(cfg.get("allow_dead_code")))
            bad += ["%s: %s" % (cfg["id"], m) for k, _, m in hp if k == "dead_code"]
    check("dead code: every repository golden passes with its task's flags (%s)" % ("; ".join(bad)[:120] or "all"), not bad)
    import run_agent
    import steer_agent as sa
    cfg = json.load(open(os.path.join(td, "task.json")))
    cfg.update(dir=td, prompt=open(os.path.join(td, "prompt.md")).read())
    ws = os.path.join(tmp, "ws_dc")
    out = run_agent.build_workspace(cfg, ws, None, provide_tests=True)
    sa.install_snapshot_wrapper(cfg, ws, out)
    os.chmod(os.path.join(ws, out), 0o644)
    open(os.path.join(ws, out), "w").write(sink_c)
    r = subprocess.run(["bash", "run_selfcheck.sh"], cwd=ws, capture_output=True, text=True, timeout=600)
    o = r.stdout + r.stderr
    check("dead code: the worker's self-check prints it as a FAIL line, RESULT: FAIL", r.returncode != 0 and
          re.search(r"(?m)^FAIL: line \d+: the unused-bits sink `unused` holds only constants", o) and
          re.search(r"(?m)^RESULT: FAIL \(lint/file checks; everything else passed\)", o))
    check("dead code: TASK.md states the rule", "no dead code" in open(os.path.join(ws, "TASK.md")).read())


def test_debug_left(tmp):
    """Item: debug leftovers and unlisted lint waivers fail at stage hygiene (debug_left)."""
    import glob
    import hygiene as H
    import run_agent
    import steer_agent as sa
    td = copy_task(tmp, "v_rebuild_ce_frac", "dbg", lint_wall=True, allow_initial=True)
    gold = open(os.path.join(td, "golden", "ce_frac.v")).read()
    line = "            acc <= sum[W-1:0];\n"
    disp = gold.replace(line, line + '            $display("acc %0d", acc);\n')
    fin = gold.replace(line, line + "            if (1'b0) $finish;\n")
    waived = gold.replace(line, "            /* verilator lint_off WIDTHTRUNC */\n            acc <= sum;\n"
                                "            /* verilator lint_on WIDTHTRUNC */\n")
    for name, text, want in (("$display fails at stage hygiene", disp, ("hygiene", False, "debug_left")),
                             ("$finish fails at stage hygiene", fin, ("hygiene", False, "debug_left")),
                             ("$display only in a comment passes",
                              gold.replace(line, line + '            // $display("x");\n'), ("done", True))):
        g = grade_text(td, text, "dbg%d" % len(results))
        ok = (g.get("stage"), g.get("pass")) == want[:2] and (len(want) < 3 or want[2] in (g.get("detail") or ""))
        check("debug_left: " + name, ok)
        if not ok:
            print("    got", g.get("stage"), g.get("pass"), (g.get("detail") or "")[:200])
    g = grade_text(td, disp, "dbgr")
    check("debug_left: counted in the grade record (hygiene.debug_left)", (g.get("hygiene") or {}).get("debug_left") == 1)
    allow = copy_task(tmp, "v_rebuild_ce_frac", "dbg_allow", lint_wall=True, allow_initial=True, allow_display=True)
    check("debug_left: \"allow_display\": true lets $display pass", grade_text(allow, disp, "dbga").get("pass") is True)
    lw = copy_task(tmp, "v_rebuild_ce_frac", "dbg_lw", lint_wall=True, allow_initial=True, lint_waivers=["UNUSEDSIGNAL"])
    g = grade_text(lw, waived, "lw1")
    check("debug_left: a lint_off for a rule outside lint_waivers fails at hygiene",
          g.get("stage") == "hygiene" and "lint_off WIDTHTRUNC" in (g.get("detail") or "") and "UNUSEDSIGNAL" in g["detail"])
    lw2 = copy_task(tmp, "v_rebuild_ce_frac", "dbg_lw2", lint_wall=True, allow_initial=True, lint_waivers=["WIDTHTRUNC"])
    check("debug_left: a lint_off for a listed rule passes", grade_text(lw2, waived, "lw2").get("pass") is True)
    check("debug_left: no lint_waivers list -> a named, paired lint_off passes (old rule)",
          grade_text(td, waived, "lw3").get("pass") is True)
    code = H.strip_comments_keep_lines
    units = [("$writememh is not debug output", '$writememh("f.hex", mem);', False),
             ("$displayh counts", '$displayh(x);', True), ("$monitor counts", '$monitor("%d", x);', True),
             ("$write counts", '$write("x");', True), ("in a string only", 'x = "$display";', False)]
    for name, text, bad in units:
        check("debug_left (unit): %s" % name, bool(H.debug_left(code(text), text)) == bad)
    t2 = "x;\n$display(1);\n"
    check("debug_left (unit): only lines inside span count",
          H.debug_left(code(t2), t2, span=(1, 1)) == [] and H.debug_left(code(t2), t2, span=(2, 2)))
    check("debug_left (unit): an empty lint_waivers list refuses every waiver",
          H.debug_left("", "// verilator lint_off WIDTH\n", lint_waivers=[]) and
          not H.debug_left("", "// verilator lint_off WIDTH\n", lint_waivers=None))
    bad = []
    for tj in sorted(glob.glob(os.path.join(BASE, "tasks", "*", "task.json"))):
        cfg = json.load(open(tj))
        if not H.applies(cfg):
            continue
        for gf in glob.glob(os.path.join(os.path.dirname(tj), "golden", "*.v")) + glob.glob(os.path.join(os.path.dirname(tj), "golden", "*.sv")):
            hp = H.hygiene_problems(open(gf, errors="replace").read(), allow_initial=True, allow_dead_code=True,
                                    allow_display=bool(cfg.get("allow_display")), lint_waivers=H.lint_waivers(cfg))
            bad += ["%s: %s" % (cfg["id"], m) for k, _, m in hp if k == "debug_left"]
    check("debug_left: every repository golden passes with its task's flags (%s)" % ("; ".join(bad)[:120] or "all"), not bad)
    cfg = json.load(open(os.path.join(lw, "task.json")))
    cfg.update(dir=lw, prompt=open(os.path.join(lw, "prompt.md")).read())
    ws = os.path.join(tmp, "ws_dbg")
    out = run_agent.build_workspace(cfg, ws, None, provide_tests=True)
    sa.install_snapshot_wrapper(cfg, ws, out)
    check("debug_left: the settings reach hygiene_check.py (workspace agrees with task.json)",
          not sa.workspace_config_problems(cfg, ws))
    os.chmod(os.path.join(ws, out), 0o644)
    open(os.path.join(ws, out), "w").write(disp)
    r = subprocess.run(["bash", "run_selfcheck.sh"], cwd=ws, capture_output=True, text=True, timeout=600)
    o = r.stdout + r.stderr
    check("debug_left: the worker's self-check prints it as a FAIL line, RESULT: FAIL", r.returncode != 0 and
          re.search(r"(?m)^FAIL: line \d+: `\$display` is debug code", o) and
          re.search(r"(?m)^RESULT: FAIL \(lint/file checks; everything else passed\)", o))
    tm = open(os.path.join(ws, "TASK.md")).read()
    check("debug_left: TASK.md states the rule and the waiver list", "no debug code" in tm and
          "lint waivers (`verilator lint_off`) only for UNUSEDSIGNAL" in tm)
    rr = H.repository_rules(dict(json.load(open(os.path.join(BASE, "tasks", "kat_debounce", "task.json"))), lint_waivers=[]),
                            os.path.join(BASE, "tasks", "kat_debounce"))
    check("debug_left: the Repository rules paragraph has the rule and the waiver limit",
          "- No debug code: no `$display`" in rr and "This task allows no waivers." in rr)


def test_write_guard():
    """Item: the write guard's decision on a write of the graded file."""
    import steer_agent as sa
    full = "// x\nmodule m (input a, output b);\n  assign b = a;\n  // padding padding padding\nendmodule\n"
    frag = "    wire unused = &{1'b0, a};\n"
    check("write guard: a write without module/endmodule is refused",
          "module/endmodule" in (sa.write_guard_problem(full, frag, top="m") or ""))
    check("write guard: a module of another name is refused",
          sa.write_guard_problem(full, full.replace("module m ", "module n "), top="m") is not None)
    half = "module m (input a, output b);\nendmodule\n"
    check("write guard: under half of a version that compiled is refused",
          "less than half" in (sa.write_guard_problem(full, half, top="m", prev_compiled=True) or ""))
    check("write guard: ... allowed when the previous version did not compile (or it is unknown)",
          sa.write_guard_problem(full, half, top="m", prev_compiled=False) is None and
          sa.write_guard_problem(full, half, top="m", prev_compiled=None) is None)
    check("write guard: a full rewrite passes", sa.write_guard_problem(full, full + "\n", top="m", prev_compiled=True) is None)
    check("write guard: no previous version -> nothing to protect", sa.write_guard_problem(None, frag, top="m") is None)
    mk = "    // SECTION 2: pulse"
    sec = mk + "\n    always @(posedge clk) q <= d;\n"
    check("write guard (section file): the marker line dropped is refused",
          "marker" in (sa.write_guard_problem(sec, "    always @(posedge clk) q <= d;\n", marker=mk) or ""))
    check("write guard (section file): marker kept but no code left is refused",
          "no code" in (sa.write_guard_problem(sec, mk + "\n    // todo\n", marker=mk) or ""))
    check("write guard (section file): an empty start section may stay empty, a body passes",
          sa.write_guard_problem(mk + "\n", mk + "\n", marker=mk) is None and sa.write_guard_problem(mk + "\n", sec, marker=mk) is None)
    check("write guard: a text with no module never ranks or becomes the candidate",
          sa.graded_text_problem(frag, top="m") and not sa.graded_text_problem(full, top="m") and
          sa.graded_text_problem(mk + "\n", marker=mk) and not sa.graded_text_problem(sec, marker=mk))


def test_steer_queue_args():
    """Item: the queued-steer limit."""
    import steer_agent as sa
    a = sa.build_parser().parse_args(["--run-id", "x"])
    check("steer queue: --steer-queue-max defaults to 120 s", a.steer_queue_max == 120 and sa.queue_limit(a) == 120)
    a = sa.build_parser().parse_args(["--run-id", "x", "--steer-queue-max", "0"])
    check("steer queue: --steer-queue-max 0 keeps the old --queue-max-s limit (480 s)", sa.queue_limit(a) == 480)


def test_heatguard(tmp):
    """Item: heatguard.sh ends with its run's process group (fake thermal CSV, dummy groups, no board)."""
    hg = os.path.join(HERE, "heatguard.sh")
    csv = os.path.join(tmp, "csv")
    os.makedirs(csv)
    for b in ("b1", "b2"):
        open(os.path.join(csv, b + ".csv"), "w").write("time,tctl,edge,a,b,c,d,fan,cap\n12:00:00,60,50,0,0,0,0,1000,1500\n")
    conf = os.path.join(tmp, "heatguard.conf")
    open(conf, "w").write('BOARDS="b1 b2"\nSTEPS[b1]="1500 1300 1100"\nSTEPS[b2]="1200 1000 800"\n'
                          'URL[b1]="http://127.0.0.1:9"\nURL[b2]="http://127.0.0.1:9"\nUNLOAD_MODEL=""\n')
    env = dict(os.environ, HEATGUARD_CSV_DIR=csv, HEATGUARD_INTERVAL="1", HEATGUARD_LOG=os.path.join(tmp, "hg.log"),
               HEATGUARD_CONF=conf)
    run_log = os.path.join(tmp, "run.log")
    open(run_log, "w").write("running\n")
    p = subprocess.run(["bash", hg, run_log, str(os.getpid()), tmp], env=dict(env, HEATGUARD_CONF=os.path.join(tmp, "none")),
                       capture_output=True, text=True, timeout=10)
    check("heatguard: no configuration -> refused at once", p.returncode == 2 and "no configuration" in p.stderr)

    def alive(p, s):
        t0 = time.time()
        while time.time() - t0 < s and p.poll() is None:
            time.sleep(0.2)
        return p.poll() is None
    dummy = subprocess.Popen(["sleep", "300"], start_new_session=True)
    g = subprocess.Popen(["bash", hg, run_log, str(dummy.pid), tmp], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        check("heatguard: runs while its run's process group exists", alive(g, 3))
        os.killpg(dummy.pid, 9)
        dummy.wait()
        check("heatguard: exits within 5 s once the group is gone", not alive(g, 5) and g.returncode == 0)
        check("heatguard: says why it stopped", "process group %d is gone" % dummy.pid in open(env["HEATGUARD_LOG"]).read())
    finally:
        for p in (dummy, g):
            if p.poll() is None:
                os.killpg(p.pid, 9)
    # started from the run's own shell, inside the run's group: it must leave the group, or it keeps it alive
    sh = subprocess.Popen(["bash", "-c", 'sleep 300 & S=$!; bash "$1" "$2" "$$" "$3" & echo "$S $!"; wait $S',
                           "x", hg, run_log, tmp], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                          start_new_session=True)
    try:
        sleep_pid, guard_pid = [int(x) for x in sh.stdout.readline().split()]
        time.sleep(3)
        try:
            gpg = os.getpgid(guard_pid)
        except ProcessLookupError:
            gpg = None
        check("heatguard: started inside the run's group it moves to its own group", gpg is not None and gpg != sh.pid)
        os.kill(sleep_pid, 9)
        sh.wait(10)
        t0 = time.time()
        gone = False
        while time.time() - t0 < 6:
            try:
                os.killpg(gpg, 0)
            except (ProcessLookupError, TypeError):
                gone = True
                break
            time.sleep(0.2)
        check("heatguard: ... and exits once the rest of the run's group is gone", gone)
    finally:
        try:
            os.killpg(sh.pid, 9)
        except ProcessLookupError:
            pass
        try:
            if gpg:
                os.killpg(gpg, 9)
        except (ProcessLookupError, NameError, TypeError):
            pass
    p = subprocess.run(["bash", hg, run_log, "results/proj_x", tmp], env=env, capture_output=True, text=True, timeout=10)
    check("heatguard: a pgid that is not a number is refused at once", p.returncode == 2 and "pgid" in (p.stdout + p.stderr))


def test_lint_warnings_raw(tmp):
    """Item: the grade record's raw warning count is named apart from the graded lint_wall count."""
    td = copy_task(tmp, "v_rebuild_ce_frac", "raw", lint_wall=True, allow_initial=True)
    g = grade_text(td, open(os.path.join(td, "golden", "ce_frac.v")).read(), "raw")
    check("lint_warnings_raw: 1 (DECLFILENAME of candidate.v), lint_wall.warnings 0, no ambiguous lint_warnings",
          g.get("pass") is True and g.get("lint_warnings_raw") == 1 and (g.get("lint_wall") or {}).get("warnings") == 0
          and "lint_warnings" not in g)


FAKE_GW_RSC = """#!/bin/bash
mkdir -p syn/impl/gwsynthesis && echo "// fake netlist" > syn/impl/gwsynthesis/fake.vg
cat > syn/impl/gwsynthesis/syn_syn_rsc.xml <<X
<?xml version="1.0" encoding="UTF-8"?>
<Module name="top" Register="20" Lut="30" T_Register="20(20)" T_Lut="30(30)" T_MULTALU27X18="${FAKE_DSP:-0}(0)" T_Bsram="2(2)">
 <SubModule name="u" T_MULTALU27X18="${FAKE_DSP:-0}(${FAKE_DSP:-0})"/>
</Module>
X
"""


def test_resources(tmp):
    """Item: stage resources from the Gowin synthesis report (fake gw_sh, private lane directory)."""
    import hygiene as H
    fake = os.path.join(tmp, "fake_gw_sh")
    open(fake, "w").write(FAKE_GW_RSC)
    os.chmod(fake, 0o755)
    td = copy_task(tmp, "v_rebuild_ce_frac", "res", lint_wall=True, allow_initial=True, gowin_syn=True)
    gold = open(os.path.join(td, "golden", "ce_frac.v")).read()
    vj = os.path.join(td, "validation.json")
    v = json.load(open(vj))

    def gr(dsp, *extra, **tj):
        t = json.load(open(os.path.join(td, "task.json")))
        for k in ("max_mult", "allow_more_dsp"):
            t.pop(k, None)
        t.update(tj)
        json.dump(t, open(os.path.join(td, "task.json"), "w"), indent=1)
        f = os.path.join(td, "cand_r.v")
        open(f, "w").write(gold)
        env = dict(os.environ, STEER_GRADE_SCRATCH=os.path.join(td, "gs"), GOWIN_SH=fake, FAKE_DSP=str(dsp),
                   STEER_HEAVY_LOCKS=os.path.join(tmp, "lane"))
        p = subprocess.run([sys.executable, os.path.join(BASE, "lib", "grade.py"), td, f] + list(extra),
                           capture_output=True, text=True, timeout=900, env=env)
        try:
            return json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            return {"pass": None, "stage": "unreadable", "detail": (p.stdout + p.stderr)[-300:]}
    v["golden"]["syn"] = {"syn_pass": True}
    json.dump(v, open(vj, "w"))
    g = gr(5)
    rs = g.get("resources") or {}
    check("resources: counts parsed from the report (5 DSP, 2 BSRAM)", (rs.get("candidate") or {}).get("dsp") == 5 and
          rs["candidate"].get("bsram") == 2 and rs["candidate"]["prims"].get("MULTALU27X18") == 5)
    check("resources: no golden count recorded -> unchecked, pass", g.get("pass") is True and rs.get("checked") is False)
    v["golden"]["syn"] = {"syn_pass": True, "resources": {"dsp": 1, "bsram": 0, "prims": {"MULTALU27X18": 1}}}
    json.dump(v, open(vj, "w"))
    g = gr(5)
    check("resources: 5 DSP blocks vs the golden's 1 fails at stage resources", g.get("stage") == "resources" and
          g.get("pass") is False and "5 DSP block(s) (MULTALU27X18 x5), the golden uses 1" in (g.get("detail") or ""))
    g = gr(1)
    check("resources: as many as the golden passes", g.get("pass") is True and (g.get("resources") or {}).get("checked"))
    g = gr(2, max_mult=2)
    check("resources: \"max_mult\": 2 allows 2", g.get("pass") is True and g["resources"]["limit_from"] == "max_mult")
    g = gr(3, max_mult=2)
    check("resources: ... and fails 3", g.get("stage") == "resources" and "max_mult allows 2" in (g.get("detail") or ""))
    g = gr(5, allow_more_dsp=True)
    check("resources: \"allow_more_dsp\": true records the counts only", g.get("pass") is True and
          g["resources"]["candidate"]["dsp"] == 5 and g["resources"]["checked"] is False)
    g = gr(5, "--no-resources")
    check("resources: --no-resources records only (validate grading the golden)", g.get("pass") is True and
          g["resources"]["candidate"]["dsp"] == 5)
    cfg = dict(json.load(open(os.path.join(td, "task.json"))), gowin_syn=True)
    check("resources: the worker's rules state the limit", "at most 1 DSP multiplier block after Gowin synthesis" in
          H.rules_text(cfg, td) and H.dsp_limit(dict(cfg, allow_more_dsp=True), td) == (None, None))


def test_repeat_first_fail(tmp):
    """Item: RepeatFailWatch and its note."""
    import steer_agent as sa
    A = ("FAIL: first mismatch at cycle 5137 (sample point 10273); every value below is from that sample point\n"
         "FAIL:   inputs reset=0 ce_a=0 ce_b=1 trig=0 sel=0\n"
         "FAIL:   output v_out expected 0038 got 0000 (wrong bits 0038)\n"
         "FAIL:   outputs that match here: v_x=0000\n"
         "FAIL: output v_out: 237236 mismatching sample points, first at cycle 5137\nFAIL: 237236 mismatches")
    A2 = A.replace("10273", "10275").replace("237236", "1200").replace("got 0000 (wrong bits 0038)", "got 0010 (wrong bits 0028)")
    B = A.replace("cycle 5137", "cycle 6000")
    v1 = "module m;\n  assign v = a;\nendmodule\n"
    v1c = "module m;\n  // a comment\n    assign v =  a;  /* waived */\nendmodule\n"
    v2 = "module m;\n  assign v = b;\nendmodule\n"
    ff = sa.first_fail(A)
    check("first_fail: key keeps the cycle and output, drops the sample point and values",
          ff["key"] == "cycle 5137: output v_out" and sa.first_fail(A2)["key"] == ff["key"] and sa.first_fail(B)["key"] != ff["key"])
    check("first_fail: none from count lines only", sa.first_fail("FAIL: 3 mismatches") is None)
    w = sa.RepeatFailWatch()
    hits = [w.selfcheck(A, v1, 237236), w.selfcheck(A2, v1c, 1200), w.selfcheck(A, v1c, 237236), w.side_file(), w.side_file()]
    check("repeat: same first failure, only comments changed -> exactly one note (rate-limited)",
          [bool(h) for h in hits] == [False, True, False, False, False])
    spec = os.path.join(tmp, "TASK.md")
    open(spec, "w").write("# Task\nThe output v_out is the averaged voltage.\n    output wire [15:0] v_out\n"
                          "//   slot 4: v_out = acc / 32; acc = 0;\nv_out_x is another signal\n")
    note = sa.repeat_fail_note(hits[1][0], hits[1][1], "tone_gen.v", sa.spec_refs("v_out", [spec]))
    check("repeat note: quotes the exact first FAIL lines", A2.split("\n")[0] in note and A2.split("\n")[2] in note)
    check("repeat note: names the output and the wrong bits (XOR)", "output v_out is wrong at cycle 5137" in note and
          "wrong bits 0028 (XOR), bits 5, 3" in note)
    check("repeat note: points at the spec rule first, then prose, port last; not v_out_x",
          note.index("TASK.md line 4") < note.index("TASK.md line 2") < note.index("TASK.md line 3") and "line 5" not in note)
    check("repeat note: fix now with one whole-file Write", "fix it in tone_gen.v now with one Write call holding the whole file" in note)
    w = sa.RepeatFailWatch()
    hits = [w.selfcheck(A, v1, 9), w.selfcheck("", v1, None), w.selfcheck(A, v2, 9), w.side_file(), w.side_file(),
            w.selfcheck(A, v1, 9)]
    check("repeat: hit twice with a code change, then a side file -> one note at the side file",
          [bool(h) for h in hits] == [False, False, False, True, False, False] and "side files" in hits[3][1])
    w = sa.RepeatFailWatch()
    hits = [w.selfcheck(A, v1, 9), w.side_file(), w.selfcheck(A, v2, 9)]
    check("repeat: a side file between two runs of the same first failure -> one note",
          [bool(h) for h in hits] == [False, False, True])
    w = sa.RepeatFailWatch()
    hits = [w.selfcheck(A, v1, 9), w.selfcheck(B, v1, 9), w.side_file(), w.selfcheck(A, v2, 9), w.selfcheck("", v2, 0),
            w.selfcheck(A, v2, 9)]
    check("repeat: different first failures, a code change or a PASS in between -> no note", not any(hits))
    w = sa.RepeatFailWatch()
    hy = "FAIL: line 87: the unused-bits sink `unused` holds only constants\nRESULT: FAIL (compile)"
    hits = [w.selfcheck(hy, v1, None), w.selfcheck(hy, v1, None)]
    check("repeat: runs that did not simulate (file-check FAIL lines only) give no note", not any(hits))


def test_notes():
    """Item: edit and declaration notes."""
    import steer_agent as sa
    check("edit note: 'write the whole file with Write now'", "write the whole file with Write now" in sa.edit_now_note("a.v", "x"))
    one, two = sa.decl_note([(5, "reg a;")], 1), sa.decl_note([(5, "reg a;"), (9, "reg b;")], 2)
    check("decl note: the second time quotes the module-level rule with the lines",
          "AGAIN" not in one and "AGAIN" in two and sa.DECL_RULE in two and "line 5" in two and "line 9" in two)


class Res(dict):
    def fail(self, stage, detail):
        self.update(passed=False, stage=stage, detail=detail)
        return self


def main():
    tmp = tempfile.mkdtemp(prefix="steer_units_")
    try:
        test_gate_cancel(os.path.join(tmp, "g") if os.makedirs(os.path.join(tmp, "g")) is None else "")
        test_boards(os.path.join(tmp, "b") if os.makedirs(os.path.join(tmp, "b")) is None else "")
        test_holds_and_attempts(os.path.join(tmp, "h") if os.makedirs(os.path.join(tmp, "h")) is None else "")
        test_resume(os.path.join(tmp, "r") if os.makedirs(os.path.join(tmp, "r")) is None else "")
        test_must_use_bits(os.path.join(tmp, "u") if os.makedirs(os.path.join(tmp, "u")) is None else "")
        test_syn_finish(os.path.join(tmp, "s") if os.makedirs(os.path.join(tmp, "s")) is None else "")
        os.makedirs(os.path.join(tmp, "lw"))
        td, pin = test_lint_wall(os.path.join(tmp, "lw"))
        std = test_lint_wall_sections(os.path.join(tmp, "lw"))
        test_selfcheck_hygiene(os.path.join(tmp, "lw"), td, pin)
        test_selfcheck_section(os.path.join(tmp, "lw"), std)
        os.makedirs(os.path.join(tmp, "st"))
        P, src = test_stale_config(os.path.join(tmp, "st"))
        test_retry_feedback(os.path.join(tmp, "st"), P, src)
        os.makedirs(os.path.join(tmp, "kb"))
        test_kat_build(os.path.join(tmp, "kb"))
        test_notes()
        os.makedirs(os.path.join(tmp, "dc"))
        test_dead_code(os.path.join(tmp, "dc"))
        test_lint_warnings_raw(os.path.join(tmp, "dc"))
        os.makedirs(os.path.join(tmp, "dbg"))
        test_debug_left(os.path.join(tmp, "dbg"))
        test_write_guard()
        test_steer_queue_args()
        os.makedirs(os.path.join(tmp, "hg"))
        test_heatguard(os.path.join(tmp, "hg"))
        os.makedirs(os.path.join(tmp, "rs"))
        test_resources(os.path.join(tmp, "rs"))
        os.makedirs(os.path.join(tmp, "rf"))
        test_repeat_first_fail(os.path.join(tmp, "rf"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("UNIT TESTS: %s (%d/%d)" % ("PASS" if all(results) else "FAIL", sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
