#!/usr/bin/env python3
"""test_launch.py -- regression test: a coordinator job must start its OpenCode worker and its events must flow,
for every job kind, even while every desktop heavy lane is held by someone else.

  python3 steer/test_launch.py [task or task@s<n> ...]      (default: one task of each kind present in tasks/)

Runs a scripted fake LLM (steer/oc_probe.py --serve-tcp) and coordinator.py with the stub engine against it, with
STEER_HEAVY_LOCKS pointing at private lock directories that are all pre-created (busy). No board, no Gowin. Passes
when every job logged a worker start with a bound OpenCode session id and a non-empty event stream.
"""
import json, os, shutil, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("STEER_BC250_BASE") or os.path.dirname(HERE)
DEFAULT = ["v_rebuild_ce_frac", "kat_debounce", "v_section_pwm@s2"]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    names = sys.argv[1:] or [t for t in DEFAULT if os.path.isdir(os.path.join(BASE, "tasks", t.split("@")[0]))]
    tmp = tempfile.mkdtemp(prefix="steer_launch_")
    locks = [os.path.join(tmp, "lock%d" % i) for i in range(3)]
    for l in locks:
        os.mkdir(l)                                   # every lane busy
    jobs = []
    for i, n in enumerate(names):
        t, _, sec = n.partition("@")
        j = {"id": "j%d" % i, "task": t, "summary": n}
        if sec:
            j["section"] = int(sec.lstrip("s"))
        jobs.append(j)
    pdir = os.path.join(tmp, "proj")
    os.makedirs(pdir)
    json.dump({"name": "launch_test", "goal": "launch regression test", "jobs": jobs}, open(os.path.join(pdir, "project.json"), "w"))
    json.dump([{"text": "Done."}], open(os.path.join(tmp, "script.json"), "w"))
    port = free_port()
    env = dict(os.environ, STEER_BC250_BASE=BASE, STEER_HEAVY_LOCKS=",".join(locks), STEER_BOARD_LOCK_DIR=tmp)
    llm = subprocess.Popen([sys.executable, os.path.join(HERE, "oc_probe.py"), "--serve-tcp", str(port), "--out",
                            os.path.join(tmp, "llm"), "--script", os.path.join(tmp, "script.json")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    run_id = "launchtest_%d" % os.getpid()
    boards = ",".join("b%d=127.0.0.1:%d" % (i, port) for i in range(2))
    try:
        p = subprocess.run([sys.executable, os.path.join(HERE, "coordinator.py"), "--project", pdir, "--run-id", run_id,
                            "--engine", "stub", "--provide-tests", "--auto-done", "--timeout", "150", "--watchdog", "60",
                            "--max-steers", "1", "--max-attempts", "1", "--grade-args=--no-syn", "--boards", boards],
                           env=env, capture_output=True, text=True, timeout=1800)
    finally:
        os.killpg(llm.pid, 15)
    rd = os.path.join(BASE, "results", "proj_" + run_id)
    rows = [json.loads(l) for l in open(os.path.join(rd, "workers.jsonl"))] if os.path.exists(os.path.join(rd, "workers.jsonl")) else []
    ok = True
    for j, n in zip(jobs, names):
        tag_part = "__%s__" % (n if "@" not in n else n.split("@")[0] + "@s" + n.split("@")[1].lstrip("s"))
        started = [r for r in rows if tag_part in r["tag"] and r["what"] == "start"]
        bound = [r for r in rows if tag_part in r["tag"] and r["what"] == "sid_bound"]
        evs = [f for f in os.listdir(os.path.join(rd, "events")) if tag_part in f and f.endswith("s0.jsonl")] if os.path.isdir(os.path.join(rd, "events")) else []
        nev = sum(1 for f in evs for l in open(os.path.join(rd, "events", f)) if l.startswith("{"))
        good = bool(started) and bool(bound) and nev > 0
        ok &= good
        print("%-28s worker starts %d, session bound %s, events %d -> %s" % (n, len(started), bool(bound), nev, "OK" if good else "FAIL"))
    if not ok:
        print(p.stdout[-2000:] + p.stderr[-2000:])
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(rd, ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "agent_ws", "proj_" + run_id), ignore_errors=True)
    print("LAUNCH TEST: %s" % ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
