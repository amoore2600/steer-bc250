#!/usr/bin/env python3
"""run_agent.py -- OpenCode agent-mode bake-off: one sandboxed OpenCode session per (model, task), graded by the
same hidden task graders as the one-shot run.

  python3 ~/steer-bc250/run_agent.py --run-id <id> --model <model-id> [--tasks t1,t2] [--timeout 1200]

Per task, sequentially: build a fresh workspace (only what the one-shot prompt exposed: the starting / buggy / DUT
file, context files, TASK.md; git-initialised), start the host-side LLM forwarder, run
`opencode-cli run --standalone --auto --format json` inside agent/sandbox.sh (bubblewrap) inside lib/heavy.sh (heavy
lane, 4G memory cap, wall-clock timeout), record the event stream and git diff, copy the produced file out, grade it
with the task's grade.sh, append one line to results/agent_<id>/results.jsonl (fsynced).  Resumable: recorded pairs
are skipped.  summary.md compares each task with the one-shot run (--oneshot, default full38_20261028).
"""
import argparse, collections, datetime, glob, hashlib, json, os, re, shutil, subprocess, sys, time

BASE = os.path.expanduser("~/steer-bc250")
sys.path.insert(0, BASE)
import run_api  # noqa: E402  (thermal(), category(), load_tasks())

OPENCODE = "/opt/OpenCode/resources/opencode-cli"
AGENT = os.path.join(BASE, "agent")
HEAVY = os.path.join(BASE, "lib", "heavy.sh")
INFER_HOST = os.environ.get("BAKEOFF_INFER_HOST", "localhost")
INFER_PORT = os.environ.get("BAKEOFF_INFER_PORT", "8080")
GOWIN_SIMLIB = os.path.expanduser("~/gowin/IDE/simlib/gw5a")

TOOLS_NOTE = """You are working in a sandbox: the current directory /srv/work is the only writable place that is kept.
The HDL tools installed here are verilator (5.020), iverilog / vvp (Icarus 12), ghdl (4.1, mcode) and yosys (0.33).
Use them: lint and compile your file, and write your own throwaway testbenches in this directory to simulate it.
Gowin tools are NOT installed here (vendor synthesis runs only at grading)."""


def output_file(cfg):
    return "tb.v" if cfg["type"] == "testbench" else cfg["golden_files"][0]


def selfcheck_dir(cfg):
    """tasks/<id>/selfcheck/ when the task has a generated self-check testbench (lib/gen_selfcheck.py), else None."""
    d = os.path.join(cfg["dir"], "selfcheck")
    return d if os.path.isfile(os.path.join(d, "selfcheck.json")) else None


def build_workspace(cfg, ws, agents_md=None, provide_tests=False):
    td = cfg["dir"]
    os.makedirs(ws)
    gname = cfg["golden_files"][0]
    if cfg["type"] == "rebuild":
        src = glob.glob(os.path.join(td, "start", "*"))[0]
        shutil.copy(src, os.path.join(ws, gname))
        what = "`%s` contains the module/entity header (name, parameters/generics, ports, comments) with an empty body. " \
               "Write the complete implementation into that file, keeping the header exactly." % gname
    elif cfg["type"] == "bugfix":
        src = glob.glob(os.path.join(td, "start", "*"))[0]
        shutil.copy(src, os.path.join(ws, gname))
        what = "`%s` contains exactly one bug. Fix it in place, changing as little as possible." % gname
    else:
        shutil.copy(os.path.join(td, "golden", gname), os.path.join(ws, gname))
        what = ("`%s` is the module under test (do not edit it). Write the self-checking testbench into `tb.v` "
                "(it will be compiled as `iverilog -g2012 -o sim tb.v %s` and run with `vvp -n sim`)." % (gname, gname))
    for c in cfg.get("context_src", []):
        shutil.copy(os.path.join(BASE, "src", c), os.path.join(ws, os.path.basename(c)))
    prim_note = ""
    if cfg.get("gowin"):
        prim = "prim_sim.vhd" if cfg["language"] == "vhdl" else "prim_sim.v"
        dst = os.path.join(ws, prim)
        shutil.copy(os.path.join(GOWIN_SIMLIB, prim), dst)
        os.chmod(dst, 0o444)
        prim_note = ("`%s` is Gowin's own GW5A simulation library (read-only context, do not edit or copy it into your "
                     "answer): it defines the vendor primitives with their real ports and parameters. The hidden grader "
                     "simulates your file together with this same library%s." % (prim, " (analysed into the work library "
                     "with `ghdl -a --std=08 -fsynopsys -frelaxed`)" if prim.endswith(".vhd") else
                     " (`iverilog -g2012 ... prim_sim.v`; Verilator cannot parse it)"))
    out = output_file(cfg)
    tests_note = []
    scd = selfcheck_dir(cfg) if provide_tests else None
    if scd:
        sc = json.load(open(os.path.join(scd, "selfcheck.json")))
        for f in sc["files"]:                                   # testbench + vector file only (no README / seeds)
            shutil.copy(os.path.join(scd, f), os.path.join(ws, f))
        tb, vec = sc["files"][0], sc["files"][1]
        wrapper = os.path.join(ws, "run_selfcheck.sh")             # one command, no arguments to get wrong
        open(wrapper, "w").write("#!/bin/bash\n# Runs the provided self-check testbench against your design. Do not edit.\n"
                                 "cd \"$(dirname \"$0\")\" && exec " + sc["hdl_check_cmd"] + "\n")
        os.chmod(wrapper, 0o555)
        tests_note = ["## Self-check testbench (provided)", "",
                      "A self-check testbench with known-correct expected outputs is provided: `%s` (its expected values are in "
                      "`%s`). It drives `%s` with a fixed set of random inputs and compares every output, cycle by cycle, against "
                      "values recorded from a correct reference implementation. Run it, exactly like this and with no arguments, "
                      "from this directory:" % (tb, vec, cfg["top"]), "",
                      "    bash run_selfcheck.sh", "",
                      "It lints, compiles and simulates your `%s` with the testbench and ends with one line, `RESULT: PASS` or "
                      "`RESULT: FAIL (...)`. On a mismatch it prints `FAIL: cycle N port P expected E got G` lines. Keep fixing "
                      "`%s` and re-running `bash run_selfcheck.sh` until it prints `RESULT: PASS`; do not finish on a FAIL. "
                      "Do not edit `run_selfcheck.sh`, `%s` or `%s`. It covers one set of random inputs only; the final grading "
                      "uses different random inputs, so make the design correct in general, not just for these vectors."
                      % (out, out, tb, vec), ""]
    if agents_md:
        shutil.copy(agents_md, os.path.join(ws, "AGENTS.md"))   # project-level rules, loaded by OpenCode with the global ones
    lines = ["# Task %s" % cfg["id"], "", what, "", TOOLS_NOTE, ""] + ([prim_note, ""] if prim_note else []) + [
             "Grading (hidden, after you finish): only `%s` is taken from this directory and checked exactly as the "
             "original task statement below describes. Other files you create are ignored." % out, "",
             "When the statement below says to reply with the file or a code block, write the file `%s` instead. "
             "Finish by leaving the final version of `%s` in place." % (out, out), ""] + tests_note + [
             "## Original task statement", "", cfg["prompt"].rstrip(), ""]
    open(os.path.join(ws, "TASK.md"), "w").write("\n".join(lines))
    g = ["git", "-C", ws, "-c", "user.name=bakeoff", "-c", "user.email=bakeoff@localhost"]
    subprocess.run(g[:3] + ["init", "-q"], check=True)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-q", "-m", "task start"], check=True)
    return out


def classify_shell(cmd):
    c = cmd or ""
    kinds = []
    if re.search(r"\bverilator\b", c):
        kinds.append("verilator_build" if re.search(r"--(binary|cc|exe|build)\b", c) else "verilator_lint")
    if re.search(r"\bghdl\s+(-s|-a|analy)", c):
        kinds.append("ghdl_analyse")
    if re.search(r"\bghdl\s+(-e|-r|--elab-run|elab)", c):
        kinds.append("ghdl_elab_run")
    if re.search(r"\biverilog\b", c):
        kinds.append("iverilog_compile")
    if re.search(r"\bvvp\b", c):
        kinds.append("vvp_sim")
    if re.search(r"\byosys\b", c):
        kinds.append("yosys")
    return kinds or ["other_shell"]


def parse_events(path):
    ev = []
    for l in open(path, errors="replace"):
        l = l.strip()
        if l.startswith("{"):
            try:
                ev.append(json.loads(l))
            except ValueError:
                pass
    tools = collections.Counter()
    shell = collections.Counter()
    tok = collections.Counter()
    trace, final = [], ""
    steps = 0
    fin = 0
    errors = []
    for e in ev:
        t, p = e.get("type"), e.get("part", {}) or {}
        if t == "step_start":
            steps += 1
        elif t == "step_finish":
            fin += 1
            tk = p.get("tokens") or {}
            for k in ("input", "output", "reasoning"):
                tok[k] += tk.get(k) or 0
            tok["cache_read"] += (tk.get("cache") or {}).get("read") or 0
        elif t == "tool_use":
            name = p.get("tool")
            tools[name] += 1
            st = p.get("state") or {}
            inp = st.get("input") or {}
            if name in ("bash", "shell"):
                for k in classify_shell(inp.get("command")):
                    shell[k] += 1
                desc = "$ " + str(inp.get("command"))[:160]
            elif name in ("edit", "write", "patch", "multiedit"):
                desc = "%s %s" % (name, inp.get("filePath") or inp.get("path") or "")
            else:
                desc = "%s %s" % (name, json.dumps(inp)[:120])
            out = str(st.get("output") or st.get("error") or "")
            trace.append({"tool": name, "status": st.get("status"), "call": desc,
                          "out": out[:300].replace("\n", " | ")})
        elif t == "text":
            final = p.get("text") or final
        elif t == "error":
            errors.append(json.dumps(e)[:400])
    return {"steps": steps, "finished_steps": fin, "tool_calls": dict(tools), "shell_kinds": dict(shell), "tokens": dict(tok),
            "final_message": final[:2000], "trace": trace, "events": len(ev), "event_errors": errors}


def write_summary(rd, refs):
    """refs: one-shot runs shown next to the agent results, e.g. full38_20261028 (temperature 0) and the one-shot
    vendor-sampling run."""
    rows = run_api.load_rows(os.path.join(rd, "results.jsonl"))
    refrows = {}
    for ref in refs:
        rr = {}
        for r in run_api.load_rows(os.path.join(BASE, "results", ref, "results.jsonl")):
            rr.setdefault((r["model"], r["task"]), []).append(r)
        refrows[ref] = rr

    def simruns(rs):
        """Simulation runs (iverilog compiles, vvp, ghdl -e/-r) summed over samples."""
        t = 0
        for r in rs:
            k = r.get("shell_kinds") or {}
            t += k.get("iverilog_compile", 0) + k.get("vvp_sim", 0) + k.get("ghdl_elab_run", 0)
        return t

    agent_refs = [ref for ref in refs if any("shell_kinds" in x for v in refrows[ref].values() for x in v)]

    def kn(rs):
        return "%d/%d" % (sum(r["status"] == "PASS" for r in rs), len(rs)) if rs else "-"
    L = ["# Agent-mode bake-off: %s" % os.path.basename(rd), "",
         "Generated %s. References (one-shot, same tasks, same graders): %s. k/N = passing samples of N; "
         "pass@N = solved by any sample." % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), ", ".join(refs)), ""]
    for m in sorted({r["model"] for r in rows}):
        rs = [r for r in rows if r["model"] == m]
        tasks = sorted({r["task"] for r in rs})
        n = max(r.get("samples_total", 1) for r in rs)
        npass = sum(r["status"] == "PASS" for r in rs)
        pak = sum(any(r["status"] == "PASS" for r in rs if r["task"] == t) for t in tasks)
        L += ["## %s" % m, "",
              "Agent: mean pass rate %.1f%% (%d/%d samples), pass@%d %d/%d tasks, %d samples per task. Timeouts: %d. "
              "Total wall %.1f min. Simulation runs: %d, samples that simulated %d/%d." % (100.0 * npass / len(rs), npass, len(rs), n, pak, len(tasks), n,
                                        sum(bool(r.get("timed_out")) for r in rs), sum(r.get("wall_s") or 0 for r in rs) / 60,
                                        simruns(rs), sum(1 for r in rs if simruns([r]) > 0), len(rs))]
        for ref in refs:
            rr = [x for t in tasks for x in refrows[ref].get((m, t), [])]
            if rr:
                L.append("Reference %s: mean pass rate %.1f%% (%s), pass@any %d/%d tasks%s." % (
                    ref, 100.0 * sum(x["status"] == "PASS" for x in rr) / len(rr), kn(rr),
                    sum(any(x["status"] == "PASS" for x in refrows[ref].get((m, t), [])) for t in tasks), len(tasks),
                    (", simulation runs %d, samples that simulated %d/%d" % (simruns(rr), sum(1 for x in rr if simruns([x]) > 0), len(rr)))
                    if ref in agent_refs else ""))
        L += ["", "Sampling forced by the forwarder: %s; server /slots after each sample: %s." % (
                  sorted({json.dumps(r.get("sampling_sent"), sort_keys=True) for r in rs}),
                  sorted({json.dumps(r.get("server_slot_sampling"), sort_keys=True) for r in rs})), "",
              "| task | agent k/N | per sample (P/F, stage) | " + " | ".join(refs) + " | mean wall (s) | mean tool calls | lint / compile / sim runs (sum) | sim runs: this run" +
              "".join(" / %s" % ref for ref in agent_refs) + " | samples that simulated: this run" + "".join(" / %s" % ref for ref in agent_refs) + " | peak tctl |",
              "|" + "---|" * (9 + len(refs))]
        for t in tasks:
            ts = sorted((r for r in rs if r["task"] == t), key=lambda r: r.get("sample", 0))
            sk = collections.Counter()
            for r in ts:
                sk.update(r.get("shell_kinds") or {})
            lint = sk["verilator_lint"] + sk["ghdl_analyse"]
            comp = sk["iverilog_compile"] + sk["verilator_build"]
            sim = sk["vvp_sim"] + sk["ghdl_elab_run"]
            per = ", ".join(("P" if r["status"] == "PASS" else "%s(%s%s)" % (r["status"][0], r.get("stage"),
                             ",timeout" if r.get("timed_out") else "")) for r in ts)
            def simulated(rs):
                return "%d/%d" % (sum(1 for r in rs if simruns([r]) > 0), len(rs))
            L.append("| %s | %s | %s | %s | %.0f | %.1f | %d / %d / %d | %s | %s | %s |" % (
                t, kn(ts), per, " | ".join(kn(refrows[ref].get((m, t), [])) for ref in refs),
                sum(r.get("wall_s") or 0 for r in ts) / len(ts), sum(sum((r.get("tool_calls") or {}).values()) for r in ts) / len(ts),
                lint, comp, sim, " / ".join([str(simruns(ts))] + [str(simruns(refrows[ref].get((m, t), []))) for ref in agent_refs]),
                " / ".join([simulated(ts)] + [simulated(refrows[ref].get((m, t), [])) for ref in agent_refs]),
                max(((r.get("thermal") or {}).get("tctl_c") or 0) for r in ts)))
        L.append("")
    open(os.path.join(rd, "summary.md"), "w").write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--oneshot", default="full38_20261028", help="comma-separated one-shot runs shown as references")
    ap.add_argument("--grade-timeout", type=int, default=2400)
    ap.add_argument("--sampling", default="vendor", choices=["t0", "vendor", "vendor_high", "none"],
                    help="sampling forced on every chat request by the host-side forwarder (run_api.sampling_for)")
    ap.add_argument("--samples", type=int, default=1, help="independent samples per task (fresh workspace and session each)")
    ap.add_argument("--skills", default="on", choices=["on", "off"], help="off hides ~/.config/opencode/skills inside the sandbox")
    ap.add_argument("--preload-skills", action="store_true",
                    help="mention @hdl-check (and @gowin-primitives on Gowin tasks) in the prompt so OpenCode loads them up front")
    ap.add_argument("--project-agents", default="", help="file copied into every workspace as AGENTS.md (project rules)")
    ap.add_argument("--provide-tests", action="store_true",
                    help="copy tasks/<id>/selfcheck/ (golden-derived self-check testbench, lib/gen_selfcheck.py) into the workspace "
                         "and tell the agent in TASK.md how to run it; grading still uses the hidden test on other seeds")
    a = ap.parse_args()
    rd = os.path.join(BASE, "results", "agent_" + a.run_id)
    wsroot = os.path.join(BASE, "agent_ws", a.run_id)
    for d in ("cand", "grade", "events"):
        os.makedirs(os.path.join(rd, d), exist_ok=True)
    os.makedirs(wsroot, exist_ok=True)
    rj = os.path.join(rd, "results.jsonl")
    done = set()
    if os.path.exists(rj):
        for l in open(rj):
            if l.strip():
                r = json.loads(l)
                if r["status"] != "ERROR":
                    done.add((r["model"], r["task"], r.get("sample", 0)))
    else:
        open(rj, "w").close()
    tasks = run_api.load_tasks([t for t in a.tasks.split(",") if t], False)
    samp = run_api.sampling_for(a.model, a.sampling)
    refs = [x for x in a.oneshot.split(",") if x]
    print("agent run %s: %s x %d tasks x %d samples, sampling %s, %d already done" % (
        a.run_id, a.model, len(tasks), a.samples, samp, len(done)), flush=True)
    for cfg, si in [(c, i) for c in tasks for i in range(a.samples)]:
        if (a.model, cfg["id"], si) in done:
            continue
        tag = "%s__%s__s%d" % (a.model, cfg["id"], si)
        base = os.path.join(wsroot, tag)
        shutil.rmtree(base, ignore_errors=True)
        ws, oc = os.path.join(base, "work"), os.path.join(base, "oc")
        sk = os.path.join(BASE, "agent_ws", "sock", hashlib.sha1(("%s/%s" % (a.run_id, tag)).encode()).hexdigest()[:10])
        shutil.rmtree(sk, ignore_errors=True)               # short path: AF_UNIX socket paths are limited to 107 bytes
        os.makedirs(oc)
        os.makedirs(sk)
        agents_md = os.path.abspath(os.path.expanduser(a.project_agents)) if a.project_agents else None
        out = build_workspace(cfg, ws, agents_md, provide_tests=a.provide_tests)
        tests_provided = bool(a.provide_tests and selfcheck_dir(cfg))
        if a.preload_skills:   # skill texts appended to the workspace AGENTS.md: always in the prompt, not left to the model
            with open(os.path.join(ws, "AGENTS.md"), "a") as f:
                for k in ["hdl-check"] + (["gowin-primitives"] if cfg.get("gowin") else []):
                    body = open(os.path.expanduser("~/.config/opencode/skills/%s/SKILL.md" % k)).read()
                    body = body.split("\n---\n", 1)[1] if body.startswith("---") else body
                    f.write("\n\n<!-- skill %s, preloaded -->\n%s" % (k, body))
            g = ["git", "-C", ws, "-c", "user.name=bakeoff", "-c", "user.email=bakeoff@localhost"]
            subprocess.run(g + ["commit", "-q", "-a", "--amend", "--no-edit"], check=True)
        occ = json.load(open(os.path.join(AGENT, "opencode.sandbox.json")))
        occ["model"] = occ["small_model"] = "bc250/" + a.model   # title requests on the model under test: no router swap
        json.dump(occ, open(os.path.join(sk, "opencode.json"), "w"), indent=2)
        reqlog = os.path.join(rd, "events", tag + ".requests.jsonl")
        fargs = [sys.executable, os.path.join(AGENT, "fwd.py"), "unix2tcp", os.path.join(sk, "llm.sock"), INFER_HOST, INFER_PORT, reqlog]
        fargs.append(json.dumps(samp) if samp else "-")
        fargs.append(str(json.load(open(os.path.join(AGENT, "opencode.sandbox.json")))["provider"]["bc250"]["models"][a.model]["limit"]["output"]))
        if agents_md:
            # logged per request: is the project rule (its second line) and the global rules' heading in the prompt
            fargs += [[l.strip() for l in open(agents_md) if l.strip()][1], "# FPGA work rules"]
        fwd = subprocess.Popen(fargs)
        for _ in range(50):
            if os.path.exists(os.path.join(sk, "llm.sock")):
                break
            time.sleep(0.1)
        if fwd.poll() is not None or not os.path.exists(os.path.join(sk, "llm.sock")):
            raise SystemExit("LLM forwarder failed to start for %s (socket %s); stopping the run" % (tag, sk))
        msg = ("Read TASK.md in the current directory (/srv/work) and complete the task it describes. Work only inside "
               "/srv/work. When you are done, the file %s must contain your final answer." % out)
        cmd = [HEAVY, str(a.timeout), os.path.join(AGENT, "sandbox.sh"), ws, oc, sk,
               OPENCODE, "run", "--standalone", "--auto", "--format", "json", "--model", "bc250/" + a.model, msg]
        rec = {"run_id": "agent_" + a.run_id, "mode": "agent", "model": a.model, "task": cfg["id"], "type": cfg["type"],
               "language": cfg["language"], "category": run_api.category(cfg["id"]), "output_file": out,
               "sample": si, "samples_total": a.samples, "sampling_profile": a.sampling, "sampling_sent": samp,
               "project_agents": os.path.basename(a.project_agents) if a.project_agents else None, "skills": a.skills,
               "preload_skills": a.preload_skills, "provide_tests": tests_provided,
               "timeout_s": a.timeout, "started": datetime.datetime.now().isoformat(timespec="seconds")}
        evp = os.path.join(rd, "events", tag + ".jsonl")
        t0 = time.time()
        with open(evp, "w") as fo, open(os.path.join(rd, "events", tag + ".stderr"), "w") as fe:
            p = subprocess.run(cmd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
                               env=dict(os.environ, SANDBOX_NO_SKILLS="1" if a.skills == "off" else "0"))
        t1 = time.time()
        fwd.terminate()
        try:
            fwd.wait(5)
        except subprocess.TimeoutExpired:
            fwd.kill()
        rec["wall_s"] = round(t1 - t0, 1)
        rec["exit_code"] = p.returncode
        rec["timed_out"] = p.returncode in (124, 137)
        rec["thermal"] = run_api.thermal(t0, t1)
        rq = [json.loads(l) for l in open(reqlog)] if os.path.exists(reqlog) else []
        rec["llm_requests"] = len(rq)
        rec["sampling_received"] = sorted({json.dumps({k: v for k, v in (r.get("received") or {}).items() if k not in ("model", "stream")}, sort_keys=True) for r in rq})
        rec["sampling_sent_seen"] = sorted({json.dumps({k: v for k, v in (r.get("sent") or {}).items() if k not in ("model", "stream")}, sort_keys=True) for r in rq})
        rec["server_slot_sampling"] = run_api.slot_sampling(a.model)
        if agents_md:
            rec["project_agents"] = os.path.basename(a.project_agents)
            mk = [r.get("marker_in_body") or {} for r in rq]
            proj = [l.strip() for l in open(agents_md) if l.strip()][1]
            rec["project_rule_in_requests"] = "%d/%d" % (sum(1 for m in mk if m.get(proj)), len(rq))
            rec["global_rules_in_requests"] = "%d/%d" % (sum(1 for m in mk if m.get("# FPGA work rules")), len(rq))
        ev = parse_events(evp)
        rec.update({k: ev[k] for k in ("steps", "tool_calls", "shell_kinds", "tokens", "final_message", "events", "event_errors")})
        rec["last_step_unfinished"] = ev["steps"] > ev["finished_steps"]   # the last model turn ended without a step_finish (e.g. output limit)
        json.dump(ev["trace"], open(os.path.join(rd, "events", tag + ".trace.json"), "w"), indent=1)
        g = ["git", "-C", ws, "-c", "user.name=bakeoff", "-c", "user.email=bakeoff@localhost"]
        subprocess.run(g + ["add", "-A"], capture_output=True)
        subprocess.run(g + ["commit", "-q", "-m", "agent end"], capture_output=True)
        root = subprocess.run(g + ["rev-list", "--max-parents=0", "HEAD"], capture_output=True, text=True).stdout.split()[0]
        diff = subprocess.run(g + ["diff", root, "HEAD", "--stat"], capture_output=True, text=True).stdout
        subprocess.run(g + ["diff", root, "HEAD"], stdout=open(os.path.join(rd, "events", tag + ".diff"), "w"))
        rec["diff_stat"] = diff.strip().splitlines()[-1] if diff.strip() else "no changes"
        produced = os.path.join(ws, out)
        rec["output_changed"] = out in diff
        cand = os.path.join(rd, "cand", tag + (".v" if cfg["type"] == "testbench" else os.path.splitext(out)[1]))
        if os.path.exists(produced):
            shutil.copy(produced, cand)
        else:
            open(cand, "w").close()
        rec["candidate"] = cand
        g0 = time.time()
        try:
            gp = subprocess.run([os.path.join(cfg["dir"], "grade.sh"), cand], capture_output=True, text=True, timeout=a.grade_timeout)
            open(os.path.join(rd, "grade", tag + ".log"), "w").write(gp.stdout + gp.stderr)
            gr = json.loads(gp.stdout.strip().splitlines()[-1])
        except Exception as e:
            gr = {"pass": False, "stage": "harness", "detail": "grade.sh: %s: %s" % (type(e).__name__, e)}
        rec["grade_s"] = round(time.time() - g0, 1)
        rec["grade"] = gr
        rec["pass"] = bool(gr.get("pass"))
        rec["stage"], rec["detail"] = gr.get("stage"), gr.get("detail")
        if p.returncode not in (0, 124, 137) and ev["events"] == 0:
            rec["status"] = "ERROR"                     # the agent never ran (sandbox / lane / OpenCode start failure)
        else:
            rec["status"] = "PASS" if rec["pass"] else ("ERROR" if gr.get("stage") == "harness" else "FAIL")
        rec["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        with open(rj, "a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())
        print("%s %-26s s%d %-6s %-8s wall %6.0fs steps %s tools %s%s" % (rec["finished"], cfg["id"], si, rec["status"], rec["stage"],
              rec["wall_s"], rec["steps"], sum(rec["tool_calls"].values()), " TIMEOUT" if rec["timed_out"] else ""), flush=True)
        write_summary(rd, refs)
    write_summary(rd, refs)
    print("done", flush=True)


if __name__ == "__main__":
    main()
