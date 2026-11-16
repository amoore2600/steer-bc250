#!/usr/bin/env python3
"""coordinator.py -- Claude coordinates two OpenCode workers (one per BC-250 board) on ONE project goal.

  python3 steer/coordinator.py --project projects/<name> --run-id <id> [steer_agent options ...]

A project (projects/<name>/project.json) is a set of jobs, each built from an existing task (its spec, header, provided
self-check and hidden grader) plus dependencies on other jobs. The coordinator:
  - runs up to one job per board at a time, each through steer_agent.run_task (same sandbox, relay, steering, mailbox);
  - gives a job whose dependency is not finished a STUB of that dependency (same ports, no behaviour) and tells it so;
  - when a job passes, merges its file into the project repo (git), swaps the real file into every dependent job (its
    grader context and, if it is running, its workspace) and sends that worker a message through its inbox;
  - keeps PROJECT_STATUS.md (who does what, what is merged) current in every running workspace;
  - at the end grades the integration: every merged job re-graded with the team's own dependencies, never the originals.
A job whose simulation passed while no heavy lane was free for Gowin synthesis ends SYN_PENDING: its worker is done and
its board is freed at once, and its synthesis is queued through the queue runner (grade.sh --syn-only) with no
deadline; a synthesis PASS merges the job, a synthesis FAIL counts as an attempt. The coordinator waits for every
queued synthesis before it ends.
Workers never talk to each other; the project record is the only shared state, and only the coordinator writes it.
"""
import argparse, datetime, json, os, re, shlex, shutil, signal, subprocess, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]
import steer_agent as sa   # noqa: E402
import run_api              # noqa: E402
import run_agent            # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
import sections as SEC      # noqa: E402

BASE = sa.BASE
POST_SIM_STAGES = ("lint_wall", "hygiene", "pinned", "resources")   # grade stages after a passing simulation / checker


class Project:
    def __init__(self, pdir, rd):
        self.pdir = os.path.abspath(pdir)
        self.spec = json.load(open(os.path.join(self.pdir, "project.json")))
        self.rd = rd
        self.lock = threading.RLock()
        self.repo = os.path.join(rd, "repo")
        self.jobs = {j["id"]: dict(j, state="pending", board=None, attempts=0, result=None) for j in self.spec["jobs"]}
        self.gate_failed = set()
        self.task_cfg = {}
        for j in self.jobs.values():
            loaded = run_api.load_tasks([j["task"]], False)
            if not loaded:
                raise SystemExit("job %s: task %s not found or not validated" % (j["id"], j["task"]))
            cfg = loaded[0]
            if j.get("section") is not None:          # a section job: one labelled section of a section task
                cfg = run_agent.section_cfg(cfg, j["section"])
            self.task_cfg[j["id"]] = cfg
        self.log = open(os.path.join(rd, "project_log.md"), "a")
        os.makedirs(self.repo, exist_ok=True)
        if not os.path.isdir(os.path.join(self.repo, ".git")):
            self.git("init", "-q")
            open(os.path.join(self.repo, "README.md"), "w").write("# %s\n\n%s\n" % (self.spec["name"], self.spec["goal"]))
            self.git("add", "-A"); self.git("commit", "-q", "-m", "project start")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=coordinator", "-c", "user.email=coordinator@localhost"]
                              + list(args), capture_output=True, text=True)

    def note(self, text):
        line = "- %s %s" % (datetime.datetime.now().strftime("%H:%M:%S"), text)
        print("[project] " + text, flush=True)
        self.log.write(line + "\n"); self.log.flush()

    # ---- the job's own task directory: a copy of the task, with its dependencies as stubs until they are merged
    def job_dir(self, jid):
        return os.path.join(self.rd, "jobs", jid)

    def inbox(self, jid):
        return os.path.join(self.rd, "inbox", jid)

    def dep_file(self, dep_jid):
        return self.task_cfg[dep_jid]["golden_files"][0]

    def uses(self, jid):
        """Jobs whose file this job compiles with: every context file (shown or hidden) owned by another job, so a
        dependency of a dependency counts too (the CPU compiles with the ALU AND the adder the ALU uses)."""
        cfg = self.task_cfg[jid]
        names = {os.path.basename(c) for c in cfg.get("context_src", []) + cfg.get("context_hidden", [])}
        return [o for o in self.jobs if o != jid and self.dep_file(o) in names]

    def stub_text(self, dep_jid):
        cfg = self.task_cfg[dep_jid]
        skel = os.path.join(cfg["dir"], "skeleton" + os.path.splitext(cfg["golden_files"][0])[1])
        start = skel if cfg.get("sections") and os.path.exists(skel) else \
            os.path.join(cfg["dir"], cfg.get("start_override") or os.path.join("start", sorted(os.listdir(os.path.join(cfg["dir"], "start")))[0]))
        cm = "--" if cfg["language"] == "vhdl" else "//"
        owner = self.jobs[dep_jid]
        return ("%s STUB: interface only, no behaviour. This module is being written by the %s worker as job '%s'.\n"
                "%s The coordinator replaces this file with the real one as soon as it passes; do not edit or rewrite it.\n"
                % (cm, owner["board"] or "other", dep_jid, cm)) + open(start).read()

    def prepare_job(self, jid):
        """The job directory (a fresh copy of the task directory) and the job's config. The config is read from the
        job directory's own task.json and prompt.md, the files its grade.sh reads, so a task changed while the
        project runs takes effect at the next job or attempt start, for the workspace and the grader alike."""
        src = self.task_cfg[jid]["dir"]
        jd = self.job_dir(jid)
        if os.path.isdir(jd):
            shutil.rmtree(jd)
        shutil.copytree(src, jd)
        cfg = run_agent.task_cfg_from_dir(jd, self.jobs[jid].get("section"))
        self.task_cfg[jid] = dict(cfg, dir=src)
        os.makedirs(self.inbox(jid), exist_ok=True)
        for f in os.listdir(self.inbox(jid)):
            os.remove(os.path.join(self.inbox(jid), f))
        pending = []
        for dep in self.uses(jid):
            fname = self.dep_file(dep)
            merged = os.path.join(self.repo, fname)
            if self.jobs[dep]["state"] == "merged" and os.path.exists(merged):
                shutil.copy(merged, os.path.join(jd, fname))
            else:
                open(os.path.join(jd, fname), "w").write(self.stub_text(dep))
                pending.append(dep)
        if pending:
            open(os.path.join(self.inbox(jid), "deps_pending"), "w").write(",".join(pending))
        jc = dict(cfg)
        jc["dir"] = jd
        # context files come from the job directory (team versions or stubs), never from the original sources
        jc["context_src"] = [os.path.join(jd, os.path.basename(c)) for c in cfg.get("context_src", [])]
        jc["context_hidden"] = [os.path.join(jd, os.path.basename(c)) for c in cfg.get("context_hidden", [])]
        # the worker's brief: the task prompt WITHOUT the pasted originals of modules other jobs are writing (they would
        # contradict the stubs and show other jobs' answers), plus the project section
        pm = os.path.join(jd, "prompt.md")
        text = open(pm).read()
        for dep in self.uses(jid):
            fname = self.dep_file(dep)
            text, n = re.subn(r"\n`%s`:\n\n```[a-z]*\n.*?\n```" % re.escape(fname),
                              "\n`%s`: written by job '%s' of this project (see the Project section below)." % (fname, dep),
                              text, flags=re.S)
            if n != 1:
                raise SystemExit("prompt of %s: pasted block of %s found %d times" % (jid, fname, n))
        open(pm, "w").write(text + "\n\n" + self.project_brief(jid, pending))
        if cfg.get("section") is not None and cfg["type"] == "kat":
            # kat sections: the job works on the project's file so far (sections already merged), else the skeleton
            merged = os.path.join(self.repo, cfg["golden_files"][0])
            shutil.copy(merged if os.path.exists(merged) else os.path.join(jd, "skeleton.v"), os.path.join(jd, "section_base.v"))
            self.copy_modules(jd, cfg["golden_files"][0])
        if self.jobs[jid].get("feedback"):
            open(pm, "a").write("\n## Integration feedback\n\n%s\n" % self.jobs[jid]["feedback"])
        rf = self.jobs[jid].get("retry_feedback")
        if rf:
            # also the first line of the worker's first message (run_task)
            jc["retry_feedback"] = rf
            if rf != self.jobs[jid].get("feedback"):
                open(pm, "a").write("\n## Previous attempt\n\n%s\n" % rf)
        jc["prompt"] = open(pm).read()          # the worker's TASK.md is built from cfg["prompt"], not from the file
        jc["inbox"] = self.inbox(jid)
        if self.jobs[jid].get("max_steers"):
            jc["max_steers"] = int(self.jobs[jid]["max_steers"])    # a big job can carry a larger steer budget
        if self.jobs[jid].get("resume_from"):
            jc["resume_from"], jc["resume_note"] = self.jobs[jid]["resume_from"], self.jobs[jid].get("resume_note")
        for k in ("write_whole", "lessons", "section_file"):          # per-job worker rules override the run flags
            if k in self.jobs[jid]:
                v = self.jobs[jid][k]
                jc[k] = v if isinstance(v, bool) else str(v).lower() not in ("off", "false", "0")
        return jc

    def project_brief(self, jid, pending):
        j = self.jobs[jid]
        lines = ["## Project", "", "This task is one job of a two-worker project: %s" % self.spec["goal"],
                 "Your job: **%s** (%s). Write only `%s`." % (jid, j.get("summary", ""), self.task_cfg[jid]["golden_files"][0]),
                 "Read `PROJECT_STATUS.md` in this directory for who is doing what; it is updated while you work."]
        for dep in self.uses(jid):
            fname = self.dep_file(dep)
            if dep in pending:
                lines.append("`%s` is still being written by the other worker (job '%s'). The file here is a STUB with the "
                             "right ports and no behaviour, so self-check failures caused by it are expected until it is "
                             "replaced. Do not edit or rewrite `%s`, and do not copy its logic into your file. Get everything "
                             "else right; you will be told when the real module arrives, then rerun the self-check." % (fname, dep, fname))
            else:
                lines.append("`%s` is the team's finished version from job '%s' (read-only for you)." % (fname, dep))
        return "\n".join(lines) + "\n"

    def copy_modules(self, jd, wrapper):
        """The project's merged modules (every merged .v other than the wrapper) into <job dir>/modules/, where the
        kat-sections grader takes the section's module checker input and the gate's module files from."""
        md = os.path.join(jd, "modules")
        os.makedirs(md, exist_ok=True)
        for f in os.listdir(self.repo):
            if f.endswith(".v") and f != wrapper:
                shutil.copy(os.path.join(self.repo, f), os.path.join(md, f))

    def section_blockers(self, jid):
        """Section jobs of the same task this section job must wait for (section spec 'after'), not yet merged."""
        cfg = self.task_cfg[jid]
        if cfg.get("section") is None:
            return []
        sec = [x for x in cfg.get("sections", []) if int(x["n"]) == int(cfg["section"])]
        after = set(sec[0].get("after", [])) if sec else set()
        return [o for o, oc in self.task_cfg.items() if o != jid and oc.get("task_id") == cfg.get("task_id")
                and oc.get("section") is not None and int(oc["section"]) in after and self.jobs[o]["state"] != "merged"]

    def grade_feedback(self, jid, g):
        """The retry message for a failed grade: its stage and detail, file names as the worker knows them."""
        detail = re.sub(r"\bcandidate\.s?v\b", self.dep_file(jid), str(g.get("detail") or "no detail"))
        return "the hidden grade failed at %s: %s" % (g.get("stage"), detail[:600])

    def after_failure(self, jid, rec, max_attempts, max_harness_errors):
        """A failed job: a harness ERROR is not the worker's attempt (up to max_harness_errors per job); the next
        attempt starts from this one's best snapshot, or, when the grade failed only at a check after the simulation
        (lint_wall, hygiene, pinned, resources), from this attempt's candidate. A FAIL's grade stage and detail become
        the next attempt's retry feedback."""
        j = self.jobs[jid]
        if rec.get("status") == "ERROR" and j.get("harness_errors", 0) < max_harness_errors:
            j["harness_errors"] = j.get("harness_errors", 0) + 1
            j["attempts"] -= 1
        j["state"] = "retry" if j["attempts"] < max_attempts else "failed"
        if rec.get("best_snapshot"):
            j["resume_from"] = rec["best_snapshot"]
            j["resume_note"] = "%s mismatches, first failing cycle %s" % (rec.get("best_mismatches"),
                                                                           rec.get("best_first_fail_cycle"))
        g = rec.get("grade") or {}
        j["retry_feedback"] = None
        if rec.get("status") == "FAIL" and g.get("stage"):
            j["retry_feedback"] = self.grade_feedback(jid, g)
            cand = rec.get("candidate")
            if g["stage"] in POST_SIM_STAGES and cand and os.path.exists(cand) and os.path.getsize(cand):
                j["resume_from"] = cand
                j["resume_note"] = "it passed the checker; the hidden grade failed at %s" % g["stage"]

    def syn_cmd(self, jid, rec, a, who):
        """grade.sh <candidate> --syn-only for a SYN_PENDING job: only the task's synthesis, queued with no deadline."""
        return [os.path.join(self.job_dir(jid), "grade.sh"), rec["candidate"], "--syn-only", "--who=" + who] + \
            sa.grade_args(self.task_cfg[jid]) + a.grade_args.split()

    def syn_pending(self, jid, rec):
        j = self.jobs[jid]
        j["state"] = "syn_pending"
        j["syn_candidate"] = rec["candidate"]
        self.note("job %s on %s: simulation PASSED, Gowin synthesis pending (no lane in time); the worker is done, the "
                  "board is free, synthesis queued without a deadline (candidate %s)" % (jid, rec.get("board"), rec["candidate"]))

    def finish_syn(self, jid, rec, gr, max_attempts, max_harness_errors, running_ws=None):
        """The queued synthesis of a SYN_PENDING job ended. PASS: the grade is complete, merge. FAIL (Gowin errors): a
        normal failed attempt with the synthesis error as feedback, resumed from the simulation-passing candidate.
        Anything else is a harness ERROR (H4). Returns the results.jsonl row (syn_deferred: it completes the session
        row of the same job and attempt, it is not a session of its own)."""
        j = self.jobs[jid]
        row = {k: rec.get(k) for k in ("run_id", "mode", "engine", "model", "task", "type", "language", "sample", "section",
                                       "project", "job", "board", "candidate", "session_id", "best_snapshot")}
        row.update(syn_deferred=True, grade=gr, stage=gr.get("stage"), detail=gr.get("detail"),
                   finished=datetime.datetime.now().isoformat(timespec="seconds"))
        row["pass"] = bool(gr.get("pass"))
        row["status"] = "PASS" if row["pass"] else ("FAIL" if gr.get("stage") in ("syn", "resources") else "ERROR")
        q = gr.get("syn_queue") or {}
        where = " (%s, waited %s s)" % (q.get("placement"), q.get("wait_s")) if q.get("placement") else ""
        if row["pass"]:
            self.note("job %s: Gowin synthesis PASSED%s; grade complete (PASS)" % (jid, where))
            self.merge(jid, rec["candidate"], running_ws or {})
            return row
        fb = {"status": row["status"], "best_snapshot": rec.get("best_snapshot")}
        if row["status"] == "FAIL" and gr.get("stage") == "resources":
            j["feedback"] = ("Your previous attempt passed the hidden simulation, but Gowin synthesis of the same file "
                             "uses too many DSP blocks: %s. Your file starts as that attempt; keep its behaviour and "
                             "restructure it to use fewer multipliers." % str(gr.get("detail"))[:600])
        elif row["status"] == "FAIL":
            j["feedback"] = ("Your previous attempt passed the hidden simulation, but Gowin synthesis of the same file "
                             "failed: %s. Your file starts as that attempt; keep its behaviour and change only what "
                             "synthesis rejects." % str(gr.get("detail"))[:600])
        self.after_failure(jid, fb, max_attempts, max_harness_errors)
        if row["status"] == "FAIL":
            j["retry_feedback"] = j["feedback"]
        if rec.get("best_snapshot"):
            j["resume_note"] = "passed the simulation; Gowin synthesis %s" % ("failed" if row["status"] == "FAIL" else "did not run")
        self.note("job %s: queued Gowin synthesis %s%s (%s); job %s" % (
            jid, row["status"], where, str(gr.get("detail"))[:200], j["state"]))
        return row

    def hold_reason(self, jid, rec):
        """Why a failed job is held for the porting chat instead of counting as a worker attempt, or None: a whole-file
        gate failure attributed to Claude-owned code (e.g. an INTEGRATION CONTRACT $fatal), or the task's first gate failure
        on the full assembly (kat.integration.hold_first_fail)."""
        g = rec.get("grade") or {}
        if g.get("stage") != "integration":
            return None
        cfg = self.task_cfg[jid]
        ig = (cfg.get("kat") or {}).get("integration") or {}
        if g.get("attribution") == "claude":
            return "the gate failure is attributed to Claude-owned code (%s)" % ig.get("claude_regex", "INTEGRATION CONTRACT")
        if ig.get("hold_first_fail") and cfg.get("task_id") not in self.gate_failed:
            self.gate_failed.add(cfg.get("task_id"))
            return "first gate failure on the full assembly"
        return None

    def hold(self, jid, rec, why):
        g = rec.get("grade") or {}
        j = self.jobs[jid]
        j["state"] = "held"
        j["attempts"] -= 1                       # not a worker attempt until the porting chat says so
        d = os.path.join(self.rd, "hold")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, jid + ".md"), "w").write(
            "# HOLD: job %s (%s)\n\nWhy held: %s.\n\nGate: %s\nLikely section: %s; attribution: %s\nCandidate: %s\n\n"
            "Release by writing `%s` with one line:\n- `retry`: run this job again (counts as an attempt)\n"
            "- `reopen <job id>`: reopen that job with the gate failure as its feedback; this job waits for it\n"
            "- `fail`: give up on this job\n" % (jid, self.task_cfg[jid]["id"], why, g.get("integration") or g.get("detail"),
                                                  g.get("likely_section"), g.get("attribution"), rec.get("candidate"),
                                                  os.path.join(d, jid + ".release")))
        self.note("HOLD job %s: %s; gate: %s (see hold/%s.md; the porting chat releases it)" % (
            jid, why, str(g.get("integration") or g.get("detail"))[:160], jid))

    def poll_holds(self):
        """Apply the porting chat's release files for held jobs."""
        d = os.path.join(self.rd, "hold")
        for jid, j in self.jobs.items():
            rp = os.path.join(d, jid + ".release")
            if j["state"] != "held" or not os.path.exists(rp):
                continue
            words = open(rp).read().split()
            os.rename(rp, rp + ".done")
            if words and words[0] == "retry":
                j["state"] = "retry"
            elif len(words) >= 2 and words[0] == "reopen" and words[1] in self.jobs:
                o = self.jobs[words[1]]
                o["state"], o["feedback"] = "retry", "The whole-file gate failed with every section in place: %s" % (
                    open(os.path.join(d, jid + ".md")).read().split("Gate: ", 1)[-1].split("\n")[0])
                o["retry_feedback"] = o["feedback"]
                o["attempts"] = min(o["attempts"], max(0, o["attempts"] - 1))
                j["state"] = "pending"
            else:
                j["state"] = "failed"
            self.note("released job %s: %s" % (jid, " ".join(words) or "fail"))

    def kat_section_groups(self):
        """{task id: [job ids]} of the kat-sections tasks in this project."""
        g = {}
        for jid, cfg in self.task_cfg.items():
            if cfg["type"] == "kat" and (cfg.get("kat") or {}).get("mode") == "sections" and cfg.get("section") is not None:
                g.setdefault(cfg["task_id"], []).append(jid)
        return g

    def run_integration(self, tid, jids, a):
        """Integration layer of a kat-sections task: once every section job of it is merged, run the task's whole-file
        command on the merged file. On a FAIL the likely section's job is reopened with the first failing check as
        feedback (if it has attempts left). Returns the grader's record."""
        cfg = self.task_cfg[jids[0]]
        fname = cfg["golden_files"][0]
        merged = os.path.join(self.repo, fname)
        ig = (cfg.get("kat") or {}).get("integration") or {}
        if not ig.get("cmd") or ig.get("placeholder"):
            self.note("integration of %s: no integration command yet (placeholder); sections graded on layer 1-2 only" % tid)
            return {"pass": None, "detail": "placeholder"}
        g = subprocess.run([os.path.join(cfg["dir"], "grade.sh"), merged, "--integration"] + a.grade_args.split(),
                           capture_output=True, text=True,
                           timeout=int(cfg.get("grade_timeout") or (int(ig.get("wait_budget", 1800)) + int(ig.get("timeout", 3600)) + 1200)))
        try:
            r = json.loads(g.stdout.strip().splitlines()[-1])
        except Exception:
            r = {"pass": False, "detail": "integration grader output unreadable"}
        open(os.path.join(self.rd, "integration", "%s_layer3.log" % tid), "a").write(g.stdout + g.stderr)
        self.note("integration of %s: %s (%s)" % (tid, "PASS" if r.get("pass") else "FAIL", str(r.get("detail"))[:200]))
        if not r.get("pass"):
            h = r.get("likely_section")
            for jid in jids:
                if h is not None and int(self.task_cfg[jid]["section"]) == int(h):
                    j = self.jobs[jid]
                    if j["attempts"] < a.max_attempts:
                        j["state"] = "retry"
                        j["feedback"] = ("The whole-file integration check failed after every section was merged: %s. "
                                         "It most likely comes from this section; check its connections against the "
                                         "statement." % r.get("detail"))
                        j["retry_feedback"] = j["feedback"]
                        self.note("reopened job %s with the integration failure" % jid)
        return r

    def status_md(self):
        rows = ["# Project status: %s" % self.spec["name"], "", self.spec["goal"], "",
                "| Job | File | Worker | State |", "| --- | --- | --- | --- |"]
        for j in self.jobs.values():
            rows.append("| %s | %s | %s | %s |" % (j["id"], self.dep_file(j["id"]), j["board"] or "-", j["state"]))
        rows += ["", "Only the coordinator changes this file and the interfaces between jobs."]
        return "\n".join(rows) + "\n"

    def publish_status(self, running_ws):
        text = self.status_md()
        open(os.path.join(self.rd, "PROJECT_STATUS.md"), "w").write(text)
        for ws in running_ws.values():
            try:
                open(os.path.join(ws, "PROJECT_STATUS.md"), "w").write(text)
            except OSError:
                pass

    def merge(self, jid, cand, running_ws):
        fname = self.dep_file(jid)
        cfg = self.task_cfg[jid]
        if cfg.get("section") is not None:
            # a section job contributes only its section: splice it into the project's copy of the file (the task's
            # skeleton until the first section of that file is merged)
            dst = os.path.join(self.repo, fname)
            base = open(dst).read() if os.path.exists(dst) else \
                open(os.path.join(cfg["dir"], "skeleton" + os.path.splitext(fname)[1])).read()
            body = SEC.body(open(cand, errors="replace").read(), int(cfg["section"]), cfg["language"])
            open(dst, "w").write(SEC.replace(base, int(cfg["section"]), body or "", cfg["language"]))
            cand = dst
        else:
            shutil.copy(cand, os.path.join(self.repo, fname))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "merge %s (%s, worker %s)" % (fname, jid, self.jobs[jid]["board"]))
        self.jobs[jid]["state"] = "merged"
        self.note("merged `%s` from job %s (%s)" % (fname, jid, self.jobs[jid]["board"]))
        for other, oc in self.task_cfg.items():     # kat-section jobs grade against the merged modules
            if oc["type"] == "kat" and (oc.get("kat") or {}).get("mode") == "sections" and os.path.isdir(self.job_dir(other)):
                self.copy_modules(self.job_dir(other), oc["golden_files"][0])
        for other in self.jobs.values():
            if jid not in self.uses(other["id"]):
                continue
            jd = self.job_dir(other["id"])
            if os.path.isdir(jd):
                shutil.copy(cand, os.path.join(jd, fname))          # its grader now uses the team's real module
            ws = running_ws.get(other["id"])
            if ws:
                shutil.copy(cand, os.path.join(ws, fname))
                msg = ("The other worker finished `%s` (job '%s') and it has been merged: the stub in your directory is now "
                       "the real module. Do not edit it. Run `bash run_selfcheck.sh` now and fix any remaining failures in "
                       "your own file until it prints RESULT: PASS." % (fname, jid))
                ib = self.inbox(other["id"])
                n = len(os.listdir(ib))
                tmp = os.path.join(ib, ".msg_%03d.tmp" % n)
                json.dump({"message": msg, "from": jid}, open(tmp, "w"))
                os.rename(tmp, os.path.join(ib, "msg_%03d.json" % n))  # message first ...
                still = [d for d in self.uses(other["id"]) if self.jobs[d]["state"] != "merged"]
                dp = os.path.join(ib, "deps_pending")
                if still:
                    open(dp, "w").write(",".join(still))
                elif os.path.exists(dp):
                    os.remove(dp)                                     # ... then release the wait
                self.note("sent job %s the merged `%s`" % (other["id"], fname))


INTEGRATION_SEED = 7777    # never shown to workers (their self-check uses 9001, the per-job graders 1-3)


BOARD_LOCKS = os.environ.get("STEER_BOARD_LOCK_DIR") or "/tmp"


def board_lock(name):
    return os.path.join(BOARD_LOCKS, "steer_board_%s.job" % name)


def take_board(name, who):
    """One steering job per board across coordinators: mkdir wins; the owner file names the run, job and pid. A lock
    whose pid is gone is stale and is taken over."""
    l = board_lock(name)
    try:
        os.mkdir(l)
    except FileExistsError:
        try:
            pid = int(open(os.path.join(l, "owner")).read().split(" pid ")[1].split()[0])
            os.kill(pid, 0)
            return False
        except (OSError, ValueError, IndexError):
            shutil.rmtree(l, ignore_errors=True)        # the holder is gone
            try:
                os.mkdir(l)
            except FileExistsError:
                return False
    open(os.path.join(l, "owner"), "w").write("%s pid %d %s\n" % (who, os.getpid(), datetime.datetime.now().strftime("%H:%M:%S")))
    return True


def release_board(name):
    l = board_lock(name)
    try:
        if " pid %d " % os.getpid() in open(os.path.join(l, "owner")).read():
            shutil.rmtree(l, ignore_errors=True)
    except OSError:
        pass


def integration(proj, a):
    """Project-level check with known answers. A job with no dependencies: its hidden grader on the merged file. A job
    with dependencies: expected outputs recorded from the ORIGINAL module with the ORIGINAL dependencies (hidden seed),
    compared against the team's module running with the team's dependencies. (The per-job equivalence grader cannot do
    this: it gives the reference and the candidate the same dependency files, so a broken dependency breaks both alike.)"""
    sys.path.insert(0, os.path.join(BASE, "lib"))
    import gen_selfcheck as gs
    res = {}
    for jid, j in proj.jobs.items():
        fname = proj.dep_file(jid)
        cand = os.path.join(proj.repo, fname)
        if j["state"] != "merged" or not os.path.exists(cand):
            res[jid] = {"pass": False, "detail": "not merged"}
            continue
        if not proj.uses(jid) or proj.task_cfg[jid]["type"] == "kat" or proj.task_cfg[jid].get("section") is not None:
            # graded by the task's own grader: no golden-recorded integration vectors for kat tasks (no golden may
            # exist), and a section job is graded on its section of the merged file
            # a whole-file job with no dependencies: the merged file and its context are exactly what passed synthesis
            ns = ["--no-syn"] if not proj.uses(jid) and proj.task_cfg[jid].get("section") is None \
                and "--no-syn" not in a.grade_args.split() else []
            try:
                g = subprocess.run([os.path.join(proj.task_cfg[jid]["dir"], "grade.sh"), cand] + sa.grade_args(proj.task_cfg[jid])
                                   + a.grade_args.split() + ns,
                                   capture_output=True, text=True, timeout=int(proj.task_cfg[jid].get("grade_timeout") or a.grade_timeout))
            except subprocess.TimeoutExpired as e:
                res[jid] = {"pass": False, "stage": "harness", "detail": "integration grade timed out after %s s" % e.timeout}
                continue
            open(os.path.join(proj.rd, "integration", jid + ".log"), "w").write(g.stdout + g.stderr)
            try:
                res[jid] = json.loads(g.stdout.strip().splitlines()[-1])
            except Exception:
                res[jid] = {"pass": False, "detail": "grader output unreadable"}
            continue
        ref = os.path.join(proj.rd, "integration", jid + "_ref")        # original task: golden module + golden context
        if os.path.isdir(ref):
            shutil.rmtree(ref)
        shutil.copytree(proj.task_cfg[jid]["dir"], ref)
        gs.SELFCHECK_SEED = INTEGRATION_SEED
        gs.generate(ref)
        sc = json.load(open(os.path.join(ref, "selfcheck", "selfcheck.json")))
        chk = os.path.join(proj.rd, "integration", jid + "_check")
        if os.path.isdir(chk):
            shutil.rmtree(chk)
        os.makedirs(chk)
        for f in sc["files"]:
            shutil.copy(os.path.join(ref, "selfcheck", f), chk)
        for c in proj.task_cfg[jid].get("context_files", []):           # shared types etc. from the task ...
            shutil.copy(os.path.join(ref, c), chk)
        for dep in proj.uses(jid):                                      # ... the team's dependencies at any depth ...
            shutil.copy(os.path.join(proj.repo, proj.dep_file(dep)), chk)
        shutil.copy(cand, chk)                                          # ... and the team's module
        g = subprocess.run(["bash", "-c", sc["hdl_check_cmd"]], cwd=chk, capture_output=True, text=True, timeout=a.grade_timeout)
        out = g.stdout + g.stderr
        open(os.path.join(proj.rd, "integration", jid + ".log"), "w").write(out)
        line = [l for l in out.splitlines() if l.startswith("RESULT:")]
        res[jid] = {"pass": bool(line) and line[-1].startswith("RESULT: PASS"), "stage": "integration",
                    "detail": (line[-1] if line else out[-200:]) + " (seed %d, team dependencies)" % INTEGRATION_SEED}
    return res


def main():
    ap = sa.build_parser()
    ap.add_argument("--project", required=True, help="directory holding project.json")
    ap.add_argument("--boards", default=os.environ.get("STEER_BOARDS", "board1=localhost:8080"),
                    help="name=host:port per worker board")
    ap.add_argument("--max-attempts", type=int, default=2, help="attempts per job before the project gives up on it")
    ap.add_argument("--max-harness-errors", type=int, default=2,
                    help="harness ERRORs per job that do not count as attempts (then they do)")
    a = ap.parse_args()
    sa.set_worker(a.model)
    for sg in (sa.signal.SIGTERM, sa.signal.SIGHUP):
        sa.signal.signal(sg, lambda signum, frame: sys.exit(128 + signum))
    rd = os.path.join(BASE, "results", "proj_" + a.run_id)
    wsroot = os.path.join(BASE, "agent_ws", "proj_" + a.run_id)
    for d in ("cand", "grade", "events", "decisions", "goalpacks", "readout", "mailbox", "jobs", "inbox", "integration"):
        os.makedirs(os.path.join(rd, d), exist_ok=True)
    os.makedirs(wsroot, exist_ok=True)
    hg = sa.start_heatguard(rd) if a.heatguard else None     # stopped at exit (atexit) or at the end below
    open(os.path.join(rd, "mailbox", "README.md"), "w").write(sa.MAILBOX_README)
    open(os.path.join(rd, "mailbox", "SYSTEM.md"), "w").write(sa.SYSTEM + "\n")
    boards = []
    for b in a.boards.split(","):
        name, hp = b.split("=")
        host, port = hp.split(":")
        boards.append((name, host, port))
    proj = Project(a.project, rd)
    samp = run_api.sampling_for(a.model, a.sampling)
    rj = open(os.path.join(rd, "results.jsonl"), "a")
    running_ws, threads = {}, {}
    syn_jobs = {}                 # jid -> {"thread", "proc", "rec", "cmd"}: queued synthesis of SYN_PENDING jobs
    stopping = threading.Event()
    syn_who = "steer %s" % proj.spec["name"]

    def write_row(row):
        try:
            rj.write(json.dumps(row) + "\n"); rj.flush()
        except ValueError:          # closed at exit
            pass

    def run_syn(jid):
        """Background: grade.sh --syn-only through the queue runner with no deadline; a harness result is retried twice."""
        sj = syn_jobs[jid]
        log = os.path.join(rd, "grade", os.path.basename(sj["rec"]["candidate"]).rsplit(".", 1)[0] + ".syn.log")
        for n in range(3):
            try:
                p = subprocess.Popen(sj["cmd"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     errors="replace", start_new_session=True)
                sj["proc"] = p
                out = p.communicate()[0]
                open(log, "a").write(out)
                gr = json.loads(out.strip().splitlines()[-1])
            except Exception as e:      # noqa: BLE001 -- the job must leave syn_jobs, or the coordinator never ends
                gr = {"pass": False, "stage": "harness", "detail": "--syn-only: %s: %s" % (type(e).__name__, str(e)[:200])}
            if stopping.is_set():
                return
            if gr.get("stage") != "harness":
                break
            with proj.lock:
                proj.note("job %s: queued synthesis harness error (%s)%s" % (jid, str(gr.get("detail"))[:160],
                          "; queuing it again" if n < 2 else ""))
        with proj.lock:
            if stopping.is_set():
                return
            try:
                write_row(proj.finish_syn(jid, sj["rec"], gr, a.max_attempts, a.max_harness_errors, running_ws))
            finally:
                del syn_jobs[jid]
            proj.publish_status(running_ws)

    def start_syn(jid, rec):
        cmd = proj.syn_cmd(jid, rec, a, syn_who)
        syn_jobs[jid] = {"rec": rec, "cmd": cmd, "proc": None}
        t = threading.Thread(target=run_syn, args=(jid,), daemon=True)
        syn_jobs[jid]["thread"] = t
        t.start()
    proj.note("project %s: %d jobs on %s" % (proj.spec["name"], len(proj.jobs), ", ".join(b[0] for b in boards)))

    def ready():
        # a job may start before its dependencies are finished (it gets stubs); dependencies first when possible
        cand = [j for j in proj.jobs.values() if j["state"] in ("pending", "retry") and j["attempts"] < a.max_attempts
                and not proj.section_blockers(j["id"])]
        cand.sort(key=lambda j: (len(proj.uses(j["id"])), j["attempts"]))
        return cand

    def run_job(jid, board):
        name, host, port = board
        with proj.lock:
            j = proj.jobs[jid]
            j["state"], j["board"] = "working", name
            j["attempts"] += 1
            jc = proj.prepare_job(jid)
            jc["infer_host"], jc["infer_port"] = host, port
            si = j["attempts"] - 1
            tag = "%s__%s__s%d" % (a.model, jc["id"], si)
            running_ws[jid] = os.path.join(wsroot, tag, "work")
            proj.note("job %s -> %s (attempt %d)%s" % (jid, name, j["attempts"],
                      "; waiting on " + open(os.path.join(proj.inbox(jid), "deps_pending")).read()
                      if os.path.exists(os.path.join(proj.inbox(jid), "deps_pending")) else ""))
        threading.Thread(target=lambda: (time.sleep(20), proj.publish_status(running_ws)), daemon=True).start()
        try:
            rec = sa.run_task(a, jc, si, rd, wsroot, samp)
        except BaseException as e:      # noqa: BLE001 -- one failed job must not take the project down
            rec = {"task": jc["id"], "status": "ERROR", "pass": False, "detail": "%s: %s" % (type(e).__name__, e)}
        with proj.lock:
            rec.update({"project": proj.spec["name"], "job": jid, "board": name})
            write_row(rec)
            running_ws.pop(jid, None)
            hold = proj.hold_reason(jid, rec)
            if rec.get("status") == "SYN_PENDING":
                # not a FAIL and not a harness error: no restart; this thread ends now, which frees the board
                proj.syn_pending(jid, rec)
                start_syn(jid, rec)
            elif rec.get("pass"):
                proj.merge(jid, rec["candidate"], running_ws)
            elif hold:
                proj.hold(jid, rec, hold)
            else:
                proj.after_failure(jid, rec, a.max_attempts, a.max_harness_errors)
                proj.note("job %s on %s: %s (%s)" % (jid, name, rec.get("status"), str(rec.get("detail"))[:160]))
            if rec.get("peg_native_errors"):
                proj.note("job %s: %d tool-format failures (peg-native)" % (jid, rec["peg_native_errors"]))
            proj.publish_status(running_ws)

    free = list(boards)
    integrated, layer3 = {}, {}
    try:
        while True:
            with proj.lock:
                for jid, t in list(threads.items()):
                    if not t.is_alive():
                        release_board(t.board[0])
                        free.append(t.board)
                        del threads[jid]
                todo = ready()
                while free and todo:
                    b = next((x for x in free if take_board(x[0], "coordinator %s job %s" % (a.run_id, todo[0]["id"]))), None)
                    if b is None:
                        break                            # every free board is running another coordinator's job
                    free.remove(b)
                    j = todo.pop(0)
                    t = threading.Thread(target=run_job, args=(j["id"], b), daemon=True)
                    t.board = b
                    threads[j["id"]] = t
                    t.start()
                    time.sleep(2)
                for tid, jids in proj.kat_section_groups().items():
                    sig = tuple(proj.git("rev-parse", "HEAD").stdout.split())
                    at = ((proj.task_cfg[jids[0]].get("kat") or {}).get("integration") or {}).get("at_section")
                    if at is None and all(proj.jobs[x]["state"] == "merged" for x in jids) and integrated.get(tid) != sig:
                        integrated[tid] = sig
                        layer3[tid] = proj.run_integration(tid, jids, a)
                proj.poll_holds()
                if not threads and not ready() and not syn_jobs and not any(j["state"] == "held" for j in proj.jobs.values()):
                    break
            time.sleep(5)
    finally:
        stopping.set()
        with proj.lock:
            for jid, sj in list(syn_jobs.items()):
                # stopped with a synthesis outstanding: stop it (the queue runner withdraws its ticket) and leave the way to finish
                if sj.get("proc") is not None and sj["proc"].poll() is None:
                    try:
                        os.killpg(sj["proc"].pid, signal.SIGTERM)
                    except OSError:
                        pass
                cmd = " ".join(shlex.quote(x) for x in sj["cmd"])
                proj.note("STOPPED with job %s SYN_PENDING: the simulation PASSED, Gowin synthesis did not finish. Candidate "
                          "%s. Finish it by hand: `%s`; on PASS merge the candidate into %s." % (
                              jid, sj["rec"]["candidate"], cmd, proj.repo))
                write_row({"run_id": sj["rec"].get("run_id"), "model": sj["rec"].get("model"), "task": sj["rec"].get("task"),
                           "sample": sj["rec"].get("sample"), "project": proj.spec["name"], "job": jid,
                           "status": "SYN_PENDING", "pass": False, "stage": "syn_pending", "syn_deferred": True,
                           "stopped": True, "candidate": sj["rec"]["candidate"], "syn_finish_cmd": cmd,
                           "detail": "coordinator stopped before the queued synthesis finished",
                           "finished": datetime.datetime.now().isoformat(timespec="seconds")})
        rj.close()
        sa.cleanup_live()          # job threads are daemon threads: their own cleanup never runs when the process exits
        for b in boards:
            release_board(b[0])
    res = integration(proj, a)
    ok = all(r.get("pass") for r in res.values())
    proj.note("INTEGRATION %s: %s" % ("PASS" if ok else "FAIL", ", ".join("%s=%s" % (k, "pass" if v.get("pass") else
                                                                              "fail (%s)" % v.get("detail")) for k, v in res.items())))
    json.dump({"project": proj.spec["name"], "integration_pass": ok, "integration": res, "kat_section_integration": layer3,
               "jobs": {k: {x: v[x] for x in ("state", "board", "attempts")} for k, v in proj.jobs.items()}},
              open(os.path.join(rd, "project_result.json"), "w"), indent=1)
    tot = sa.write_run_summary(rd)
    proj.note("mistakes: " + ", ".join("%s %d" % (k, tot[k]) for k in sa.MISTAKE_KEYS))
    print("done", flush=True)
    sa.stop_heatguard(hg)


if __name__ == "__main__":
    main()
