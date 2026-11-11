#!/usr/bin/env python3
"""mailbox.py -- the Claude side of steer_agent.py's `--engine mailbox`.

  mailbox.py pending <results_dir>                 list requests with no answer yet (one line each)
  mailbox.py show <req.json>                       print system prompt (once) and the request prompt, ready to hand to a judge
  mailbox.py reply <req.json> '<decision json>'    write res_NNN.json atomically (validates the schema first)
  mailbox.py reply <req.json> --action steer --message "..." [--reasoning ".."] [--evidence ".."]
  mailbox.py watch <results_dir>                   print one line per NEW unanswered request as it appears (for Monitor)
"""
import argparse, glob, json, os, sys, time

ACTIONS = ("steer", "redirect", "wait", "done", "blocked")


def pending(rd):
    out = []
    for req in sorted(glob.glob(os.path.join(rd, "mailbox", "*", "req_*.json"))):
        res = req.replace("req_", "res_")
        if not os.path.exists(res):
            out.append(req)
    return out


def line(req):
    r = json.load(open(req))
    ctx = r.get("ctx") or {}
    return "%s | n=%s %s | trigger: %s | worker %s | steers %s/%s | pass_evidence: %s" % (
        req, r.get("n"), r.get("t"), r.get("trigger"), "RUNNING" if ctx.get("worker_running") else "ENDED",
        ctx.get("steers"), ctx.get("max_steers"), "yes" if ctx.get("pass_evidence") else "no")


def reply(req, d):
    if d.get("action") not in ACTIONS:
        sys.exit("action must be one of %s" % (ACTIONS,))
    for k in ("message", "reasoning", "evidence"):
        d.setdefault(k, "")
    if d["action"] in ("steer", "redirect") and not d["message"].strip():
        sys.exit("steer/redirect needs a message")
    res = req.replace("req_", "res_")
    tmp = res + ".tmp"
    json.dump(d, open(tmp, "w"), indent=1)
    os.rename(tmp, res)
    print("wrote", res)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pending", "show", "reply", "watch"])
    ap.add_argument("target")
    ap.add_argument("decision", nargs="?")
    ap.add_argument("--action"); ap.add_argument("--message", default=""); ap.add_argument("--reasoning", default="")
    ap.add_argument("--evidence", default="")
    a = ap.parse_args()
    if a.cmd == "pending":
        for req in pending(a.target):
            print(line(req))
    elif a.cmd == "show":
        r = json.load(open(a.target))
        if r.get("n") == 1:
            print("=== SYSTEM ===\n" + r["system"] + "\n")
        print("=== REQUEST n=%s trigger=%s ===\n%s" % (r.get("n"), r.get("trigger"), r["prompt"]))
    elif a.cmd == "reply":
        if a.decision:
            d = json.loads(a.decision)
        else:
            d = {"action": a.action, "message": a.message, "reasoning": a.reasoning, "evidence": a.evidence}
        reply(a.target, d)
    elif a.cmd == "watch":
        seen = set()
        while True:
            for req in pending(a.target):
                if req not in seen:
                    seen.add(req)
                    print(line(req), flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
