#!/usr/bin/env python3
"""test_lane_release.py -- regression test for the task-long lane (--lane): with every lane busy the steerer logs
that it is waiting at once; with a free lane it writes an owner file, and on SIGTERM it releases the lane and kills
its worker. Private lock directories only (STEER_HEAVY_LOCKS), a scripted fake LLM, no board."""
import json, os, shutil, signal, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("STEER_BC250_BASE") or os.path.dirname(HERE)
TASK = sys.argv[1] if len(sys.argv) > 1 else "v_rebuild_ce_frac"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run(locks, run_id, port, wait_for, kill_after=None):
    env = dict(os.environ, STEER_BC250_BASE=BASE, STEER_HEAVY_LOCKS=",".join(locks), BAKEOFF_INFER_HOST="127.0.0.1",
               BAKEOFF_INFER_PORT=str(port))
    p = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "steer_agent.py"), "--run-id", run_id, "--engine", "stub",
                          "--tasks", TASK, "--provide-tests", "--timeout", "300", "--lane", "--grade-args=--no-syn"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    out, t0 = "", time.time()
    os.set_blocking(p.stdout.fileno(), False)
    while time.time() - t0 < 120 and wait_for not in out:
        try:
            out += p.stdout.read() or ""
        except TypeError:
            pass
        time.sleep(0.5)
    if kill_after is not None:
        time.sleep(kill_after)
    os.killpg(p.pid, signal.SIGTERM)
    p.wait(60)
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="steer_lane_")
    locks = [os.path.join(tmp, "lock%d" % i) for i in range(2)]
    json.dump([{"text": "Done."}], open(os.path.join(tmp, "s.json"), "w"))
    port = free_port()
    llm = subprocess.Popen([sys.executable, os.path.join(HERE, "oc_probe.py"), "--serve-tcp", str(port), "--out",
                            os.path.join(tmp, "llm"), "--script", os.path.join(tmp, "s.json")],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    ok = True
    try:
        for l in locks:
            os.mkdir(l)
        out = run(locks, "lanetest_busy_%d" % os.getpid(), port, "waiting for a desktop heavy lane")
        a = "waiting for a desktop heavy lane" in out
        print("all lanes busy: waiting line logged at once: %s" % a)
        ok &= a
        os.rmdir(locks[1])
        out = run(locks, "lanetest_free_%d" % os.getpid(), port, "engine:", kill_after=3)
        b = "engine:" in out and not os.path.exists(locks[1])
        print("free lane taken, worker started, lane released on SIGTERM: %s" % b)
        ok &= b
    finally:
        os.killpg(llm.pid, signal.SIGTERM)
        for r in ("lanetest_busy_%d" % os.getpid(), "lanetest_free_%d" % os.getpid()):
            shutil.rmtree(os.path.join(BASE, "results", "steer_" + r), ignore_errors=True)
            shutil.rmtree(os.path.join(BASE, "agent_ws", "steer_" + r), ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("LANE TEST: %s" % ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
