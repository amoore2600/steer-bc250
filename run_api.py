#!/usr/bin/env python3
"""run_api.py -- HDL bake-off runner: every (model, task) pair through the BC-250 llama-server, graded locally.

  python3 ~/steer-bc250/run_api.py --run-id <id> [--models m1,m2] [--tasks t1,t2] [--include-invalid]

Requests are grouped by model (one swap per model).  Each pair: build the messages, size max_tokens from the
server's own tokenizer, send (temperature 0, timeout 900 s), save the raw response, extract the last fenced code
block, grade it with the task's grade.sh (sequentially; Gowin synthesis runs in a heavy lane), and append one line
to results/<run_id>/results.jsonl (flushed + fsynced).  Pairs already in results.jsonl are skipped (resumable).
summary.md is rewritten after every pair.  Thermal max per request window: the BC-250 thermal CSV, read over ssh.
"""
import argparse, datetime, glob, json, os, re, statistics, subprocess, sys, time, urllib.request

BASE = os.path.expanduser("~/steer-bc250")
URL = os.environ.get("BAKEOFF_URL", "http://localhost:8080")
MODELS = ["Qwen3-Coder-30B-A3B-Instruct-UD-IQ3_XXS", "Qwen3-Coder-30B-A3B-Instruct-UD-Q2_K_XL",
          "Qwen3.6-35B-A3B-UD-IQ2_M", "gpt-oss-20b-MXFP4"]
SYSTEM = ("You are an expert FPGA HDL engineer. Reply with exactly one fenced code block that contains the complete "
          "requested file, and nothing else: no explanation before or after the code block.")
CTX = 32768
# Reasoning models spend output tokens on hidden reasoning before the answer; the others only need room
# for the largest file (~4k tokens) and are capped lower so a degenerate repetition loop ends sooner.
MAX_GEN = {"gpt-oss-20b-MXFP4": 16384, "Qwen3.6-35B-A3B-UD-IQ2_M": 16384}
MAX_GEN_DEFAULT = 8192
TEMPS = ["tctl_c", "gpu_edge_c", "apu_tsi_c", "nvme_c"]


def post(path, body, timeout):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def chat(body, idle_timeout):
    """Streamed chat completion, reassembled into the non-streamed response shape.

    The router does not deliver long non-streamed responses reliably; streaming keeps bytes flowing, and
    idle_timeout bounds the gap between chunks (model load + prompt processing), not the whole request."""
    body = dict(body, stream=True, stream_options={"include_usage": True})
    req = urllib.request.Request(URL + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    content, reasoning, finish, timings, usage = [], [], None, {}, {}
    with urllib.request.urlopen(req, timeout=idle_timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            ev = json.loads(data)
            if "error" in ev:
                raise RuntimeError("server error: %s" % json.dumps(ev["error"])[:300])
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    content.append(d["content"])
                if d.get("reasoning_content"):
                    reasoning.append(d["reasoning_content"])
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
            timings = ev.get("timings") or timings
            usage = ev.get("usage") or usage
    if finish is None:
        raise RuntimeError("stream ended without finish_reason")
    return {"choices": [{"message": {"content": "".join(content), "reasoning_content": "".join(reasoning)},
                         "finish_reason": finish}], "timings": timings, "usage": usage}


SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "frequency_penalty", "repeat_penalty", "seed")


def sampling_for(model, profile):
    """Sampling fields for a model: 't0' = temperature 0 only; 'vendor' = sampling.json (must list the model);
    'none' = send nothing (server defaults)."""
    if profile == "t0":
        return {"temperature": 0}
    if profile == "none":
        return {}
    if profile in ("vendor", "vendor_high"):
        table = json.load(open(os.path.join(BASE, "sampling.json")))
        if model not in table:
            raise SystemExit("sampling.json has no vendor settings for %s" % model)
        if profile == "vendor_high":   # gpt-oss chat template: "Reasoning: high" in the system message
            return dict(table[model], chat_template_kwargs={"reasoning_effort": "high"})
        return dict(table[model])
    raise SystemExit("unknown sampling profile %s" % profile)


def slot_sampling(model):
    """The sampling parameters the server used for the last task on its slot (/slots), for verification."""
    try:
        prm = post_get("/slots?model=" + model)[0]["params"]
        return {k: prm.get(k) for k in SAMPLING_KEYS if k in prm}
    except Exception as e:
        return {"error": str(e)[:200]}


def post_get(path, timeout=30):
    with urllib.request.urlopen(URL + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ntok(model, text, timeout=300):
    if not text:
        return 0
    return len(post("/tokenize", {"model": model, "content": text}, timeout)["tokens"])


def category(tid):
    for p, c in (("v_rebuild", "verilog_rebuild"), ("vh_rebuild", "vhdl_rebuild"), ("v_bugfix", "bugfix"),
                 ("vh_bugfix", "bugfix"), ("tb_", "testbench"), ("gw_", "gowin_primitive")):
        if tid.startswith(p):
            return c
    return "other"


def extract(content):
    """Last fenced code block; '<think>' sections removed first.  Returns (code, how)."""
    c = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S)
    blocks = re.findall(r"```[^\n`]*\n(.*?)```", c, flags=re.S)
    if blocks:
        return blocks[-1], "fenced"
    m = re.search(r"```[^\n`]*\n(.*)$", c, flags=re.S)          # opened, never closed
    if m:
        return m.group(1), "unclosed"
    if re.search(r"^\s*(module|library|entity|`timescale|//|--)", c, re.M):
        return c, "nofence"
    return "", "none"


def thermal(t0, t1):
    """Max of each temperature column over [t0, t1] (desktop epoch; same timezone and clock as the board)."""
    out = {}
    days = sorted({datetime.date.fromtimestamp(t0 - 10), datetime.date.fromtimestamp(t1 + 10)})
    rows = []
    for d in days:
        try:
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "bc250",
                                "tail -n 2000 thermal/%s.csv" % d.isoformat()], capture_output=True, text=True, timeout=60)
        except Exception:
            continue
        for line in p.stdout.splitlines():
            f = line.strip().split(",")
            if len(f) < 10 or not re.match(r"\d\d:\d\d:\d\d$", f[0]):
                continue
            ts = datetime.datetime.combine(d, datetime.time.fromisoformat(f[0])).timestamp()
            rows.append((ts, f))
    cols = ["time", "tctl_c", "gpu_edge_c", "apu_tsi_c", "therm14_c", "therm15_c", "nvme_c", "fan2_rpm", "gov_max_mhz", "load1"]
    sel = [f for ts, f in rows if t0 - 6 <= ts <= t1 + 6]
    for c in TEMPS + ["gov_max_mhz", "fan2_rpm"]:
        i = cols.index(c)
        vals = []
        for f in sel:
            try:
                vals.append(float(f[i]))
            except ValueError:
                pass
        out[c] = max(vals) if vals else None
    out["samples"] = len(sel)
    return out


def load_tasks(names, include_invalid):
    tasks = []
    for tj in sorted(glob.glob(os.path.join(BASE, "tasks", "*", "task.json"))):
        td = os.path.dirname(tj)
        cfg = json.load(open(tj))
        if names and cfg["id"] not in names:
            continue
        vj = os.path.join(td, "validation.json")
        valid = os.path.exists(vj) and json.load(open(vj)).get("valid")
        if not valid and not include_invalid:
            continue
        cfg["dir"] = td
        cfg["prompt"] = open(os.path.join(td, "prompt.md")).read()
        tasks.append(cfg)
    return tasks


def ext_of(cfg):
    if cfg["type"] == "testbench":
        return ".v"
    return {"vhdl": ".vhd", "systemverilog": ".sv"}.get(cfg["language"], ".v")


def sample_section(rows, ref_rows, ref_name, models):
    """Per model: mean pass rate over samples, pass@N, and per task k/N, next to a reference run (one row per pair)."""
    L = []
    if not any(r.get("samples_total", 1) > 1 for r in rows):
        return L
    L += ["## Samples: pass rate and pass@N", "",
          "k/N = samples that passed; pass@N = task solved by at least one of its N samples. Reference: %s." % ref_name, "",
          "| model | sampling | samples per task | mean pass rate | pass@N | tasks | reference PASS |", "|---|---|---|---|---|---|---|"]
    for m in models:
        rs = [r for r in rows if r["model"] == m]
        tasks = sorted({r["task"] for r in rs})
        n = max(r.get("samples_total", 1) for r in rs)
        rate = sum(r["status"] == "PASS" for r in rs) / len(rs) if rs else 0
        pak = sum(any(r["status"] == "PASS" for r in rs if r["task"] == t) for t in tasks)
        refp = sum(1 for t in tasks if (ref_rows.get((m, t)) or {}).get("status") == "PASS")
        samp = sorted({json.dumps(r.get("sampling_sent"), sort_keys=True) for r in rs})
        L.append("| %s | %s | %d | %.1f%% (%d/%d) | %d/%d | %d | %d/%d |" % (
            m, " ".join(samp), n, 100 * rate, sum(r["status"] == "PASS" for r in rs), len(rs), pak, len(tasks), len(tasks),
            refp, len(tasks)))
    L += ["", "| task | " + " | ".join("%s k/N" % m for m in models) + " | " + " | ".join("%s ref" % m for m in models) + " |",
          "|" + "---|" * (2 * len(models) + 1)]
    for t in sorted({r["task"] for r in rows}):
        cells, refs = [], []
        for m in models:
            rs = sorted((r for r in rows if r["model"] == m and r["task"] == t), key=lambda r: r.get("sample", 0))
            cells.append("%d/%d %s" % (sum(r["status"] == "PASS" for r in rs), len(rs),
                                      "".join("P" if r["status"] == "PASS" else ("T" if r["status"] == "TRUNCATED" else
                                              ("E" if r["status"] == "ERROR" else "F")) for r in rs)) if rs else "-")
            o = ref_rows.get((m, t))
            refs.append("-" if not o else o["status"])
        L.append("| %s | %s | %s |" % (t, " | ".join(cells), " | ".join(refs)))
    return L + [""]


def load_rows(rj):
    last = {}
    if os.path.exists(rj):
        for l in open(rj):
            if l.strip():
                r = json.loads(l)
                last[(r["model"], r["task"], r.get("sample", 0))] = r      # a re-run sample keeps its latest row
    return list(last.values())


def write_summary(rd, ref="full38_20261028"):
    rows = load_rows(os.path.join(rd, "results.jsonl"))
    ref_rows = {(r["model"], r["task"]): r for r in load_rows(os.path.join(BASE, "results", ref, "results.jsonl"))}
    models = [m for m in MODELS if any(r["model"] == m for r in rows)] + sorted({r["model"] for r in rows} - set(MODELS))
    cats = ["verilog_rebuild", "vhdl_rebuild", "bugfix", "testbench", "gowin_primitive"]
    L = ["# Bake-off summary: %s" % os.path.basename(rd), "",
         "Generated %s from results.jsonl (%d results). PASS counts are out of the tasks run in each category; "
         "TRUNCATED = finish_reason length (harness/token-limit artifact, counted separately from FAIL)." % (
             datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), len(rows)), ""]
    L += sample_section(rows, ref_rows, ref, models)
    L.append("| model | " + " | ".join(cats) + " | total PASS | FAIL | TRUNCATED | ERROR | median gen tok/s | median prompt tok/s | total wall (min) | peak tctl / gpu edge (C) |")
    L.append("|" + "---|" * (len(cats) + 9))
    for m in models:
        rs = [r for r in rows if r["model"] == m]
        cells = []
        for c in cats:
            cr = [r for r in rs if r["category"] == c]
            cells.append("%d/%d" % (sum(r["status"] == "PASS" for r in cr), len(cr)) if cr else "-")
        gen = [r["timings"]["predicted_per_second"] for r in rs if r.get("timings") and r["timings"].get("predicted_per_second")]
        pp = [r["timings"]["prompt_per_second"] for r in rs if r.get("timings") and r["timings"].get("prompt_per_second")]
        wall = sum((r.get("request_s") or 0) + (r.get("grade_s") or 0) for r in rs) / 60
        tc = [r["thermal"]["tctl_c"] for r in rs if r.get("thermal") and r["thermal"].get("tctl_c") is not None]
        ge = [r["thermal"]["gpu_edge_c"] for r in rs if r.get("thermal") and r["thermal"].get("gpu_edge_c") is not None]
        L.append("| %s | %s | %d/%d | %d | %d | %d | %s | %s | %.1f | %s / %s |" % (
            m, " | ".join(cells), sum(r["status"] == "PASS" for r in rs), len(rs),
            sum(r["status"] == "FAIL" for r in rs), sum(r["status"] == "TRUNCATED" for r in rs),
            sum(r["status"] == "ERROR" for r in rs),
            "%.1f" % statistics.median(gen) if gen else "-", "%.1f" % statistics.median(pp) if pp else "-",
            wall, max(tc) if tc else "-", max(ge) if ge else "-"))
    L += ["", "## Per task", "", "| task | " + " | ".join(models) + " |", "|" + "---|" * (len(models) + 1)]
    for t in sorted({r["task"] for r in rows}):
        cells = []
        for m in models:
            rs = sorted((x for x in rows if x["model"] == m and x["task"] == t), key=lambda x: x.get("sample", 0))
            cells.append("-" if not rs else " / ".join(("%s (%s)" % (r["status"], r.get("stage")) if r["status"] != "PASS" else "PASS") for r in rs))
        L.append("| %s | %s |" % (t, " | ".join(cells)))
    L += ["", "## Gowin synthesis of answers", "", "| model | task | syn | warnings | EX2478 | EX3988 |", "|---|---|---|---|---|---|"]
    for r in rows:
        s = (r.get("grade") or {}).get("syn")
        if s:
            L.append("| %s | %s | %s | %s | %s | %s |" % (r["model"], r["task"], "ok" if s.get("syn_pass") else "FAIL",
                                                     s.get("syn_warnings"), s.get("ex2478"), s.get("ex3988")))
    if any(r.get("reasoning_tokens") for r in rows):
        L += ["", "## Reasoning tokens (reasoning_content, server tokenizer)", "", "| model | task | reasoning | answer (completion - reasoning) |", "|---|---|---|---|"]
        for r in rows:
            if r.get("reasoning_tokens"):
                L.append("| %s | %s | %d | %s |" % (r["model"], r["task"], r["reasoning_tokens"],
                                                   (r.get("completion_tokens") or 0) - r["reasoning_tokens"]))
    open(os.path.join(rd, "summary.md"), "w").write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--tasks", default="")
    ap.add_argument("--include-invalid", action="store_true")
    ap.add_argument("--grade-timeout", type=int, default=2400)
    ap.add_argument("--samples", type=int, default=1, help="independent samples per (model, task)")
    ap.add_argument("--sampling", default="t0", choices=["t0", "vendor", "vendor_high", "none"], help="t0 = temperature 0; vendor = sampling.json; vendor_high = vendor + reasoning_effort high")
    ap.add_argument("--reference", default="full38_20261028", help="run shown next to this one in summary.md")
    a = ap.parse_args()
    rd = os.path.join(BASE, "results", a.run_id)
    for d in ("raw", "cand", "grade"):
        os.makedirs(os.path.join(rd, d), exist_ok=True)
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
    tasks = load_tasks([t for t in a.tasks.split(",") if t], a.include_invalid)
    models = [m for m in a.models.split(",") if m]
    for m in models:
        sampling_for(m, a.sampling)                         # fail early on a model without settings
    print("run %s: %d models x %d tasks, %d already done" % (a.run_id, len(models), len(tasks), len(done)), flush=True)
    for model in models:
        first = True
        samp = sampling_for(model, a.sampling)
        for cfg, si in [(c, i) for c in tasks for i in range(a.samples)]:
            if (model, cfg["id"], si) in done:
                continue
            tag = "%s__%s__s%d" % (model, cfg["id"], si)
            msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": cfg["prompt"]}]
            rec = {"run_id": a.run_id, "model": model, "task": cfg["id"], "type": cfg["type"], "language": cfg["language"],
                   "category": category(cfg["id"]), "sample": si, "samples_total": a.samples, "sampling_profile": a.sampling,
                   "sampling_sent": samp, "first_after_swap": first, "started": datetime.datetime.now().isoformat(timespec="seconds")}
            try:
                tmpl = post("/apply-template", {"model": model, "messages": msgs}, 300)["prompt"]
                pt = ntok(model, tmpl)
            except Exception as e:
                pt = ntok(model, SYSTEM + "\n" + cfg["prompt"]) + 32
                rec["prompt_tokens_note"] = "apply-template failed (%s); estimated" % e
            mt = min(MAX_GEN.get(model, MAX_GEN_DEFAULT), CTX - pt - 512)
            rec["prompt_tokens_est"], rec["max_tokens"] = pt, mt
            body = dict({"model": model, "messages": msgs, "max_tokens": mt, "stream": False}, **samp)
            t0 = time.time()
            resp, err = None, None
            for attempt in range(2):
                try:
                    resp = chat(body, 300)
                    break
                except Exception as e:
                    err = "%s: %s" % (type(e).__name__, e)
                    time.sleep(30)
            t1 = time.time()
            rec["request_s"] = round(t1 - t0, 1)
            rec["server_slot_sampling"] = slot_sampling(model)
            rec["thermal"] = thermal(t0, t1)
            if resp is None:
                rec.update(status="ERROR", stage="request", detail=err[:300], **{"pass": False})
            else:
                json.dump(resp, open(os.path.join(rd, "raw", tag + ".json"), "w"), indent=1)
                ch = resp["choices"][0]
                msg = ch.get("message", {})
                content = msg.get("content") or ""
                reasoning = msg.get("reasoning_content") or ""
                rec["finish_reason"] = ch.get("finish_reason")
                tm = resp.get("timings", {})
                rec["timings"] = {k: tm.get(k) for k in ("prompt_n", "prompt_per_second", "predicted_n", "predicted_per_second", "prompt_ms", "predicted_ms")}
                us = resp.get("usage", {})
                rec["prompt_tokens"] = us.get("prompt_tokens")
                rec["completion_tokens"] = us.get("completion_tokens")
                rd_ = (us.get("completion_tokens_details") or {}).get("reasoning_tokens")
                rec["reasoning_tokens"] = rd_ if rd_ is not None else (ntok(model, reasoning) if reasoning else 0)
                code, how = extract(content)
                rec["extraction"] = how
                cand = os.path.join(rd, "cand", tag + ext_of(cfg))
                open(cand, "w").write(code)
                rec["candidate"] = cand
                if rec["finish_reason"] == "length" and how != "fenced":
                    rec.update(status="TRUNCATED", stage="truncated", detail="finish_reason=length, no complete code block", **{"pass": False})
                elif not code.strip():
                    rec.update(status="FAIL", stage="extract", detail="no code block in the answer", **{"pass": False})
                else:
                    g0 = time.time()
                    try:
                        p = subprocess.run([os.path.join(cfg["dir"], "grade.sh"), cand], capture_output=True, text=True,
                                           timeout=a.grade_timeout)
                        open(os.path.join(rd, "grade", tag + ".log"), "w").write(p.stdout + p.stderr)
                        g = json.loads(p.stdout.strip().splitlines()[-1])
                    except Exception as e:
                        g = {"pass": False, "stage": "harness", "detail": "grade.sh: %s: %s" % (type(e).__name__, e)}
                    rec["grade_s"] = round(time.time() - g0, 1)
                    rec["grade"] = g
                    rec.update(stage=g.get("stage"), detail=g.get("detail"), **{"pass": bool(g.get("pass"))})
                    rec["status"] = "PASS" if g.get("pass") else ("TRUNCATED" if rec["finish_reason"] == "length" else
                                                                  ("ERROR" if g.get("stage") == "harness" else "FAIL"))
            rec["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
            with open(rj, "a") as f:
                f.write(json.dumps(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
            first = False
            print("%s %-40s %-28s s%d %-9s %-8s gen %s tok/s  req %ss" % (rec["finished"], model, cfg["id"], si, rec["status"],
                  rec.get("stage"), (rec.get("timings") or {}).get("predicted_per_second"), rec["request_s"]), flush=True)
            write_summary(rd, a.reference)
    write_summary(rd, a.reference)
    print("done", flush=True)


if __name__ == "__main__":
    main()
