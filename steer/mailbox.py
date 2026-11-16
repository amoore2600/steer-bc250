#!/usr/bin/env python3
"""mailbox.py -- the Claude side of steer_agent.py's `--engine mailbox`.

  mailbox.py pending <results_dir>                 list requests with no answer yet (one line each)
  mailbox.py show <req.json>                       print system prompt (once) and the request prompt, ready to hand to a judge
  mailbox.py reply <req.json> '<decision json>'    write res_NNN.json atomically (validates the schema first)
  mailbox.py reply <req.json> --action steer --message "..." [--reasoning ".."] [--evidence ".."]
  mailbox.py watch <results_dir>                   print one line per NEW unanswered request as it appears (for Monitor)

A steer or redirect may carry at most MAX_CODE_LINES lines of code and must not contain a whole module; give the first
failing cause as 1-3 literal points instead. --allow-big overrides and marks the decision "big_steer": true, so the
job is reported as steerer-assisted.
"""
import argparse, glob, json, os, re, sys, time

ACTIONS = ("steer", "redirect", "wait", "done", "blocked")
MAX_CODE_LINES = 15
CODE_RE = re.compile(r"(;\s*$|\bbegin\b|\bend\b|<=|\bassign\b|\balways\b|\breg\b|\bwire\b|\bcase\b|\bendcase\b)")


def code_lines(msg):
    """Lines of a message that look like Verilog (inside a code fence, or ending in ';' / using HDL keywords)."""
    n, fence = 0, False
    for l in msg.splitlines():
        if l.strip().startswith("```"):
            fence = not fence
            continue
        if l.strip() and (fence or CODE_RE.search(l)):
            n += 1
    return n


def size_problem(msg):
    if re.search(r"^\s*module\s+\w+\s*(#\s*\(|\(|;)", msg, re.M) and re.search(r"^\s*endmodule\b", msg, re.M):
        return "the message contains a whole module"
    n = code_lines(msg)
    if n > MAX_CODE_LINES:
        return "the message carries %d code lines (max %d)" % (n, MAX_CODE_LINES)
    return None


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


def reply(req, d, allow_big=False):
    if d.get("action") not in ACTIONS:
        sys.exit("action must be one of %s" % (ACTIONS,))
    for k in ("message", "reasoning", "evidence"):
        d.setdefault(k, "")
    if d["action"] in ("steer", "redirect") and not d["message"].strip():
        sys.exit("steer/redirect needs a message")
    if d["action"] in ("steer", "redirect"):
        prob = size_problem(d["message"])
        if prob and not allow_big:
            sys.exit("steer too big: %s. Give the first failing cause as 1-3 literal points (a few lines), then wait; "
                     "or pass --allow-big, which marks the job steerer-assisted." % prob)
        if prob:
            d["big_steer"] = True
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
    ap.add_argument("--allow-big", action="store_true", help="allow a large steer; marks the decision big_steer")
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
        reply(a.target, d, a.allow_big)
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
