#!/usr/bin/env python3
"""oc_probe.py -- run the real worker stack (agent/sandbox.sh + opencode run) against a FAKE LLM that answers from a
script, and record every request body OpenCode sends.  No board, no network: the fake server listens on the same
unix socket the forwarder would use.  Use it after an OpenCode update, or to prove what the worker's model sees
(system prompt, instructions, skills, tool list) and what the harness observes (event shapes, tool errors).

  python3 steer/oc_probe.py --out <dir> [--script steps.json] [--worker-rules] [--write-whole] [--config-extra extra.json]
         [--graded demo.v]
  python3 steer/oc_probe.py --serve-tcp 18080 --out <dir> [--script steps.json]
         serve the scripted fake LLM on 127.0.0.1:<port> only, for a dry run of steer_agent.py / coordinator.py
         (BAKEOFF_INFER_HOST=127.0.0.1 BAKEOFF_INFER_PORT=<port> ... --engine stub --no-lane)

The default script is: turn 1 an `edit` call on the graded file whose oldString does not exist, turn 2 a `write` of
the whole file, turn 3 plain text.  Each step is {"tool": name, "args": {...}}, {"text": "..."} or {"error": "message"} (a mid-stream server error); a request that
carries no tools (title generation) gets a fixed short text.
Writes <out>/bodies.jsonl (one request body per line), <out>/events.jsonl (the worker's JSON events),
<out>/summary.json (what was found).
"""
import argparse, asyncio, json, os, shutil, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]
import steer_agent as sa   # noqa: E402

DEFAULT_SCRIPT = [
    {"tool": "edit", "args": {"path": "@GRADED@", "oldString": "this text is not in the file", "newString": "x"}},
    {"tool": "write", "args": {"path": "@GRADED@", "content": "module probe(input a, output b);\n  assign b = a;\nendmodule\n"}},
    {"text": "Done."},
]


class FakeLLM:
    def __init__(self, sock, script, bodies):
        self.sock, self.script, self.bodies = sock, script, bodies
        self.n = 0

    def pick(self, body):
        if not body.get("tools"):
            return {"text": "probe title"}
        done = sum(1 for m in body.get("messages", []) if m.get("role") == "assistant")
        return self.script[min(done, len(self.script) - 1)]

    @staticmethod
    def sse(obj):
        return b"data: " + json.dumps(obj).encode() + b"\n\n"

    def reply(self, body, step):
        base = {"id": "chatcmpl-probe", "object": "chat.completion.chunk", "created": int(time.time()),
                "model": body.get("model", "probe")}
        out = []
        if "error" in step:
            # a server-side failure mid-stream, as llama-server reports a reply that broke its tool-call grammar
            return self.sse({"error": {"message": step["error"], "type": "server_error", "code": 500}}) + b"data: [DONE]\n\n"
        if "tool" in step:
            self.n += 1
            tc = {"index": 0, "id": "call_%d" % self.n, "type": "function",
                  "function": {"name": step["tool"], "arguments": json.dumps(step["args"])}}
            out.append(self.sse(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": [tc]},
                                                      "finish_reason": None}])))
            fin = "tool_calls"
        else:
            out.append(self.sse(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": step["text"]},
                                                      "finish_reason": None}])))
            fin = "stop"
        out.append(self.sse(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": fin}],
                                 usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})))
        out.append(b"data: [DONE]\n\n")
        return b"".join(out)

    async def handle(self, r, w):
        try:
            while True:
                head = await r.readuntil(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                n = 0
                for l in lines[1:]:
                    if l.lower().startswith("content-length:"):
                        n = int(l.split(":", 1)[1])
                raw = await r.readexactly(n) if n else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                with open(self.bodies, "a") as f:
                    f.write(json.dumps({"request": lines[0], "body": body}) + "\n")
                if lines[0].startswith("POST") and "completions" in lines[0]:
                    payload = self.reply(body, self.pick(body))
                    w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: %d\r\n\r\n" % len(payload))
                    w.write(payload)
                else:
                    payload = json.dumps({"object": "list", "data": [{"id": "gpt-oss-20b-MXFP4", "object": "model"}]}).encode()
                    w.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(payload))
                    w.write(payload)
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            w.close()

    def serve(self, ready):
        async def main():
            if isinstance(self.sock, int):
                srv = await asyncio.start_server(self.handle, "127.0.0.1", self.sock)
            else:
                srv = await asyncio.start_unix_server(self.handle, path=self.sock)
            ready.set()
            async with srv:
                await srv.serve_forever()
        asyncio.run(main())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="probe directory (recreated)")
    ap.add_argument("--script", default="", help="JSON list of steps (default: failing edit, whole-file write, text)")
    ap.add_argument("--graded", default="demo.v", help="graded file name in the workspace")
    ap.add_argument("--worker-rules", action="store_true", help="install the worker rules file the steerer installs")
    ap.add_argument("--write-whole", action="store_true", help="worker rules include the whole-file write rule")
    ap.add_argument("--config-extra", default="", help="JSON file merged into the worker opencode.json")
    ap.add_argument("--message", default="Read TASK.md and do the task.")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--serve-tcp", type=int, default=0)
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    if a.serve_tcp:
        os.makedirs(out, exist_ok=True)
        script = json.load(open(a.script)) if a.script else DEFAULT_SCRIPT
        script = json.loads(json.dumps(script).replace("@GRADED@", a.graded))
        FakeLLM(a.serve_tcp, script, os.path.join(out, "bodies.jsonl")).serve(threading.Event())
        return
    shutil.rmtree(out, ignore_errors=True)
    ws, oc = os.path.join(out, "work"), os.path.join(out, "oc")
    sk = os.path.join(sa.BASE, "agent_ws", "sock", "probe%d" % os.getpid())   # AF_UNIX paths are limited to 107 bytes
    shutil.rmtree(sk, ignore_errors=True)
    for d in (ws, oc, sk):
        os.makedirs(d)
    open(os.path.join(ws, a.graded), "w").write("module probe(input a, output b);\nendmodule\n")
    open(os.path.join(ws, "TASK.md"), "w").write("# Probe task\n\nWrite `%s`.\n" % a.graded)
    script = json.load(open(a.script)) if a.script else DEFAULT_SCRIPT
    script = json.loads(json.dumps(script).replace("@GRADED@", a.graded))
    if a.worker_rules:
        sa.install_worker_rules(ws, sa.worker_rules_text(a.graded, a.write_whole))
    occ = sa.worker_config(sa.run_agent.AGENT, "gpt-oss-20b-MXFP4")
    if a.config_extra:
        occ.update(json.load(open(a.config_extra)))
    json.dump(occ, open(os.path.join(sk, "opencode.json"), "w"), indent=2)
    bodies = os.path.join(out, "bodies.jsonl")
    llm = FakeLLM(os.path.join(sk, "llm.sock"), script, bodies)
    ready = threading.Event()
    threading.Thread(target=llm.serve, args=(ready,), daemon=True).start()
    ready.wait(10)
    argv = ["timeout", "-k", "5", str(a.timeout), os.path.join(sa.run_agent.AGENT, "sandbox.sh"), ws, oc, sk,
            sa.OPENCODE, "run", "--standalone", "--auto", "--format", "json", "--model", "bc250/gpt-oss-20b-MXFP4", a.message]
    p = subprocess.run(argv, capture_output=True, text=True)
    open(os.path.join(out, "events.jsonl"), "w").write(p.stdout)
    open(os.path.join(out, "stderr.txt"), "w").write(p.stderr)
    events = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
    reqs = [json.loads(l) for l in open(bodies)] if os.path.exists(bodies) else []
    main_reqs = [r["body"] for r in reqs if r["body"].get("tools")]
    first = main_reqs[0] if main_reqs else {}
    system = "\n".join(m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
                       for m in first.get("messages", []) if m.get("role") == "system")
    tools = sorted(t.get("function", {}).get("name") for t in first.get("tools", []))
    uses = [{"tool": (e.get("part") or {}).get("tool"), "status": ((e.get("part") or {}).get("state") or {}).get("status"),
             "input": ((e.get("part") or {}).get("state") or {}).get("input"),
             "error": str(((e.get("part") or {}).get("state") or {}).get("error") or "")[:200]}
            for e in events if e.get("type") == "tool_use"]
    summ = {"exit": p.returncode, "requests": len(reqs), "tool_requests": len(main_reqs), "tools": tools,
            "system_chars": len(system),
            "has_available_skills": "<available_skills>" in system,
            "skills_listed": [s.split("</name>")[0] for s in system.split("<name>")[1:]],
            "worker_rules_in_system": "WORKER RULES" in system,
            "lessons_in_system": "hdl-lessons" in system and "Read only the FIRST failure line" in system,
            "tool_uses": uses, "graded_file_after": open(os.path.join(ws, a.graded)).read()}
    json.dump(summ, open(os.path.join(out, "summary.json"), "w"), indent=1)
    open(os.path.join(out, "system_prompt.txt"), "w").write(system)
    shutil.copy(os.path.join(sk, "opencode.json"), os.path.join(out, "opencode.json"))
    shutil.rmtree(sk, ignore_errors=True)
    print(json.dumps({k: v for k, v in summ.items() if k != "graded_file_after"}, indent=1))


if __name__ == "__main__":
    main()
