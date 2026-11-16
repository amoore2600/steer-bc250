#!/usr/bin/env python3
"""kat_sections.py -- section jobs WITHOUT a reference (instance and wiring sections of a skeleton such as
a core's top-level wrapper), graded in layers:

  1. always: the assembled file (the skeleton, every section already merged in the project, and this job's section)
     is linted with Verilator against ports-only stubs of every module it instantiates (stubs made from the real
     sources of a pinned source tree), and the job's section is checked for connectivity against the section spec
     (instances and their port -> net connections, assigns, nets it must drive and nets it must use; Verilator's
     UNDRIVEN / UNUSED warnings on those nets fail it too);
  2. optional: a per-section checker command run on the assembled file; then lib/hygiene.py: 0 -Wall warnings on the
     section's lines (task.json "lint_wall"; the task's lint flags apply), no non-ASCII or initial block in it;
  3. integration (a separate step the coordinator runs once every worker section has passed): a whole-file command
     (e.g. a wrapper-gate simulation) on the assembled file; its first failing check is mapped to the likely section.

task.json (type "kat", kat.mode "sections"):
  "skeleton": source of the skeleton (git:<repo>@<rev>:<path> or a path),
  "tree": {"repo": <path>, "rev": <rev>, "paths": [...], "submodules": [...]}  -- the source tree for stubs, lint
          includes and the integration command (materialized once per rev under scratch/trees),
  "sections_spec": source of the section spec (JSON; see load_spec for the accepted shape),
  "worker_sections": [3, 4, 6, 7],
  "lint": {"top": ..., "incdirs": [...], "defines": [...], "flags": [...], "stub_skip": [...]},
  "kat": {"mode": "sections", "checkers": {"<n>": {"cmd": [...], "pass_regex": ...}},
          "integration": {"cmd": [... "{cand}" ...], "cwd": "{tree}", "pass_regex", "fail_regex", "timeout",
                          "section_hints": [{"regex": ..., "section": n}]}}
"""
import json, os, re, shutil, subprocess, sys, time

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
import gen_kat as K  # noqa: E402
import sections as SEC  # noqa: E402

BASE = os.environ.get("STEER_BC250_BASE") or os.path.dirname(LIB)
# Outside every git work tree: a queue runner may treat a cwd inside a work tree as that repository.
CACHE = os.environ.get("STEER_TREE_CACHE") or os.path.expanduser("~/.cache/steer-bc250")


# ------------------------------------------------------------------------------------------------ source tree
def tree_dir(cfg):
    t = cfg["tree"]
    key = "%s@%s" % (os.path.basename(os.path.expanduser(t["repo"]).rstrip("/")), t["rev"])
    return os.path.join(CACHE, "trees", key)


def materialize(cfg):
    """The pinned source tree (git archive of the listed paths, plus submodules at their recorded commits), made
    once per revision. Returns its directory."""
    t = cfg["tree"]
    d = tree_dir(cfg)
    if os.path.exists(os.path.join(d, ".complete")):
        return d
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    repo = os.path.expanduser(t["repo"])
    arch = subprocess.run(["git", "-C", repo, "archive", t["rev"]] + list(t.get("paths", [])), capture_output=True)
    if arch.returncode != 0:
        raise SystemExit("git archive %s@%s failed: %s" % (repo, t["rev"], arch.stderr.decode()[:200]))
    subprocess.run(["tar", "-x", "-C", d], input=arch.stdout, check=True)
    for sm in t.get("submodules", []):
        ls = subprocess.run(["git", "-C", repo, "ls-tree", t["rev"], sm], capture_output=True, text=True).stdout.split()
        if len(ls) < 3 or ls[1] != "commit":
            raise SystemExit("%s is not a submodule at %s" % (sm, t["rev"]))
        a2 = subprocess.run(["git", "-C", os.path.join(repo, sm), "archive", ls[2]], capture_output=True)
        if a2.returncode != 0:
            raise SystemExit("submodule %s at %s: %s" % (sm, ls[2], a2.stderr.decode()[:200]))
        os.makedirs(os.path.join(d, sm), exist_ok=True)
        subprocess.run(["tar", "-x", "-C", os.path.join(d, sm)], input=a2.stdout, check=True)
    open(os.path.join(d, ".complete"), "w").write(t["rev"] + "\n")
    return d


# ------------------------------------------------------------------------------------------------ section spec
def load_spec(cfg, td):
    """The section spec as {n: {"instances": [{"module", "name", "connects": {port: net}}], "assigns": {net: expr},
    "must_drive": [...], "must_use": [...], "after": [...]}}. Accepts a list of sections or a dict keyed by number,
    and the synonyms ports/connections (connects), drives (must_drive), reads/uses (must_use), depends_on (after)."""
    raw = json.loads(K.read_source(cfg["sections_spec"], td)) if cfg.get("sections_spec") else {}
    secs = raw.get("sections", raw) if isinstance(raw, dict) else raw
    items = secs.items() if isinstance(secs, dict) else [(x.get("n", x.get("section")), x) for x in secs]
    out = {}
    for k, v in items:
        try:
            n = int(str(k).lstrip("Ss"))
        except ValueError:
            continue
        insts = []
        for i in v.get("instances", []):
            insts.append({"module": i.get("module"), "name": i.get("name") or i.get("instance"),
                          "connects": i.get("connects") or i.get("ports") or i.get("connections") or {},
                          "params": i.get("params") or i.get("parameters") or {}})
        asg = v.get("assigns") or {}
        if isinstance(asg, list):                          # [{"lhs", "rhs"}] (sections.json v1 form)
            asg = {x["lhs"]: x.get("rhs") for x in asg}
        out[n] = {"instances": insts, "assigns": asg, "kat_cmd": v.get("kat_cmd"), "kat": v.get("kat"),
                  "name": v.get("name"),
                  "must_drive": v.get("must_drive") or v.get("drives") or [],
                  "must_use": v.get("must_use") or v.get("reads") or v.get("uses") or [],
                  "after": [int(str(x).lstrip("Ss")) for x in (v.get("after") or v.get("depends_on") or [])]}
    return out


# ------------------------------------------------------------------------------------------------ connectivity
def _strip(text):
    t = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", t)


def _norm(e):
    return re.sub(r"\s+", "", e or "")


def _named_ports(arglist):
    """{port: expr} of a named port list `.a(x), .b(y[3:0])` (balanced parentheses)."""
    out, i = {}, 0
    while True:
        m = re.compile(r"\.\s*(\w+)\s*\(").search(arglist, i)
        if not m:
            return out
        depth, j = 1, m.end()
        while j < len(arglist) and depth:
            depth += {"(": 1, ")": -1}.get(arglist[j], 0)
            j += 1
        out[m.group(1)] = arglist[m.end():j - 1].strip()
        i = j


def find_instances(text, module):
    """[(instance name, {port: expr})] of every instance of `module` in Verilog text (comments stripped)."""
    t = _strip(text)
    res = []
    for m in re.finditer(r"\b%s\b\s*(#\s*\()?" % re.escape(module), t):
        j = m.end()
        if m.group(1):                                     # skip the parameter list
            depth = 1
            while j < len(t) and depth:
                depth += {"(": 1, ")": -1}.get(t[j], 0)
                j += 1
        mm = re.compile(r"\s*(\w+)\s*\(").match(t, j)
        if not mm:
            continue
        depth, k = 1, mm.end()
        while k < len(t) and depth:
            depth += {"(": 1, ")": -1}.get(t[k], 0)
            k += 1
        res.append((mm.group(1), _named_ports(t[mm.end():k - 1])))
    return res


def connectivity(body, spec):
    """Problems of one filled section against its spec entry (empty list = connected as specified)."""
    probs = []
    for inst in spec.get("instances", []):
        found = find_instances(body, inst["module"])
        if not found:
            probs.append("no instance of %s in the section" % inst["module"])
            continue
        if inst.get("name"):
            named = [f for f in found if f[0] == inst["name"]]
            if not named:
                probs.append("the %s instance must be named %s (found %s)" % (inst["module"], inst["name"],
                                                                         ", ".join(f[0] for f in found)))
                continue
            found = named
        ports = found[0][1]
        for port, net in inst["connects"].items():
            if port not in ports:
                probs.append("%s.%s is not connected (expected %s)" % (inst["name"] or inst["module"], port, net))
            elif _norm(ports[port]) != _norm(net):
                probs.append("%s.%s is connected to %s, expected %s" % (inst["name"] or inst["module"], port, ports[port], net))
    t = _strip(body)
    for net, expr in spec.get("assigns", {}).items():
        m = re.search(r"\bassign\s+%s\s*=\s*(.*?);" % re.escape(net), t, re.S)
        if not m:
            probs.append("no `assign %s = ...`" % net)
        elif expr is not None and _norm(m.group(1)) != _norm(expr):
            probs.append("assign %s = %s, expected %s" % (net, m.group(1).strip(), expr))
    for net in spec.get("must_use", []):
        if not re.search(r"\b%s\b" % re.escape(net.split("[")[0]), t):
            probs.append("the section never uses %s" % net)
    return probs


# ------------------------------------------------------------------------------------------------ lint with stubs
def instantiated_modules(text):
    t = _strip(text)
    kw = {"module", "input", "output", "inout", "wire", "reg", "assign", "always", "if", "else", "case", "begin",
          "end", "localparam", "parameter", "integer", "genvar", "for", "function", "task", "initial", "return"}
    names = set()
    for m in re.finditer(r"^\s*([A-Za-z_]\w*)\s+(?:#\s*\(|[A-Za-z_]\w*\s*\()", t, re.M):
        if m.group(1) not in kw:
            names.add(m.group(1))
    return names


def module_header(src, name):
    """`module name ... ;` + `endmodule` from a source file's text (ports-only stub), or None."""
    m = re.search(r"^\s*module\s+%s\b" % re.escape(name), src, re.M)
    if not m:
        return None
    i, depth = m.start(), 0
    j = src.index("(", i) if "(" in src[i:] else None
    k = i
    while k < len(src):
        ch = src[k]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0 and j is not None and k > j:
            break
        k += 1
    return src[m.start():k + 1] + "\nendmodule\n"


def make_stubs(cfg, tree, text, extra_sources=()):
    """Ports-only stubs of every module the text instantiates, from the first .v/.sv file of the tree (or of
    extra_sources, searched first) that defines it. Returns (stub text, [modules not found])."""
    want = instantiated_modules(text) - set((cfg.get("lint") or {}).get("stub_skip", []))
    files = list(extra_sources)
    for d, _, fs in os.walk(tree):
        files += [os.path.join(d, f) for f in sorted(fs) if f.endswith((".v", ".sv"))]
    stubs, missing = [], []
    for name in sorted(want):
        for f in files:
            src = open(f, errors="replace").read()
            h = module_header(src, name)
            if h:
                stubs.append("// stub of %s (ports from %s)\n%s" % (name, os.path.relpath(f, tree) if f.startswith(tree) else f, h))
                break
        else:
            missing.append(name)
    return "\n".join(stubs), missing


def lint(cfg, tree, cand_file, W, extra_sources=()):
    """Verilator lint of the assembled file against stubs. Returns (errors, warnings by net {net: [kinds]})."""
    L = cfg.get("lint") or {}
    text = open(cand_file, errors="replace").read()
    pp = subprocess.run(["verilator", "-E", "-P"] + ["-I" + os.path.join(tree, i) for i in L.get("incdirs", [])]
                        + list(L.get("defines", [])) + [cand_file], capture_output=True, text=True, cwd=W, timeout=120)
    if pp.returncode == 0 and pp.stdout.strip():
        text = pp.stdout                     # instantiations as the defines select them (`ifdef / `ifndef)
    stubs, missing = make_stubs(cfg, tree, text, extra_sources)
    sp = os.path.join(W, "kat_stubs.v")
    open(sp, "w").write("`default_nettype wire\n" + stubs)
    cmd = (["verilator", "--lint-only", "-Wall", "-Wno-fatal", "--top-module", L.get("top", cfg["top"])]
           + ["-I" + os.path.join(tree, i) for i in L.get("incdirs", [])] + list(L.get("defines", []))
           + list(L.get("flags", [])) + [sp, cand_file])
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=W, timeout=300)
    lines = p.stdout.splitlines() + p.stderr.splitlines()
    errs = [l for l in lines if l.startswith("%Error") and "Exiting due to" not in l]
    errs = ["module(s) not found in the source tree: %s" % ", ".join(missing)] + errs if missing else errs
    warn = {}
    for l in lines:
        if "kat_stubs.v" in l:
            continue                        # the stubs drive nothing by design; only the assembled file counts
        m = re.match(r"%Warning-(UNDRIVEN|UNUSED\w*|PINMISSING|PINCONNECTEMPTY|WIDTH\w*|MULTIDRIVEN):.*?Signal is not (?:driven|used)[^:]*: '([^']+)'", l)
        if m:
            warn.setdefault(m.group(2), []).append(m.group(1))
        m = re.match(r"%Warning-(UNDRIVEN|UNUSEDSIGNAL):.*?'([^']+)'", l)
        if m:
            # "Bits of signal are not used/driven" is a partial use (e.g. a [7:0] select of a 16-bit bus): not a fault
            warn.setdefault(m.group(2), []).append(m.group(1) + ("_BITS" if "Bits of signal" in l else ""))
    return [e.replace(W + "/", "") for e in errs], warn, [l for l in lines if l.startswith("%Warning")]


# ------------------------------------------------------------------------------------------------ assembly
def assemble(cfg, td, cand_text, n):
    """The candidate for section n: the base file (the project's merged file, else the skeleton) with section n's
    body from cand_text. Returns (assembled text, problem or None)."""
    base_p = os.path.join(td, "section_base.v") if os.path.exists(os.path.join(td, "section_base.v")) else \
        os.path.join(td, "skeleton.v")
    base = open(base_p, errors="replace").read()
    body = SEC.body(cand_text, n)
    if body is None:
        return None, "the markers of SECTION %d must appear exactly once" % n
    return SEC.replace(base, n, body), None


def grade_section(cfg, td, W, cand, n, res, run):
    """Layers 1 and 2 for section n. Fills res; returns True on pass."""
    spec = load_spec(cfg, td).get(n, {})
    text, prob = assemble(cfg, td, open(cand, errors="replace").read(), n)
    if prob:
        res.fail("struct", prob)
        return False
    af = os.path.join(W, cfg["golden_files"][0])
    open(af, "w").write(text)
    tree = materialize(cfg)
    probs = connectivity(SEC.body(text, n), spec)
    errs, warn, wlines = lint(cfg, tree, af, W)
    if errs:
        res.fail("lint", "verilator: " + errs[0])
        return False
    for net in spec.get("must_drive", []):
        base = net.split("[")[0]
        if "UNDRIVEN" in warn.get(base, []):
            probs.append("%s is not driven" % base)
    for net in spec.get("must_use", []):
        base = net.split("[")[0]
        if any(k.startswith("UNUSED") and not k.endswith("_BITS") for k in warn.get(base, [])):
            probs.append("%s is not used" % base)
    res["connectivity"] = probs
    if probs:
        res.fail("connect", "SECTION %d: " % n + "; ".join(probs[:4]) + (" (+%d more)" % (len(probs) - 4) if len(probs) > 4 else ""))
        return False
    chk = ((cfg.get("kat") or {}).get("checkers") or {}).get(str(n))
    if not chk and spec.get("kat_cmd"):
        chk = module_checker(cfg, td, spec, res)
    if chk:
        ok, detail = run_cmd(chk, af, tree, W, run)
        res["checker"] = detail
        if not ok:
            res.fail("tb", "SECTION %d checker: %s" % (n, detail))
            return False
    import hygiene as H
    # -Wall warnings and file checks on the section's own lines (the lint above already ran with -Wall)
    if not H.grade_checks(cfg, td, W, af, wlines, res, run, SEC.body_span_lines(text, n)):
        return False
    ig = (cfg.get("kat") or {}).get("integration") or {}
    if ig.get("at_section") is not None and int(ig["at_section"]) == n and ig.get("cmd") and not ig.get("placeholder"):
        # the whole-file gate, on the full assembly only (the last section); layers 1-2 above ran first, every time
        ok, detail = gate(cfg, td, af, W, run)
        res["integration"] = detail
        if not ok and detail.startswith("HARNESS:"):
            res.fail("harness", detail)
            return False
        if not ok:
            h = section_hint(cfg, detail)
            res["likely_section"] = h
            res["attribution"] = ("claude" if re.search(ig.get("claude_regex", r"INTEGRATION CONTRACT"), detail or "") else
                                  "worker" if h == n else "section %s" % h if h is not None else "unknown")
            res.fail("integration", detail + (" (likely SECTION %d)" % h if h is not None else ""))
            return False
    return True


def module_files(td):
    """{module: file} of modules this job may use: the project's merged versions (the coordinator copies them into
    <job dir>/modules/), by file name <module>.v."""
    d = os.path.join(td, "modules")
    return {os.path.splitext(f)[0]: os.path.join(d, f) for f in os.listdir(d)} if os.path.isdir(d) else {}


def module_checker(cfg, td, spec, res):
    """The section's module checker (sections.json kat_cmd, e.g. `sim/kat/run_kat.sh <module>`) on the project's
    merged module, or the tree's copy in kat.modules_dir (default rtl). None (noted in res) when no written module exists yet: the instance is then
    graded by lint and connectivity only, unless kat.checker_required."""
    m = spec.get("kat")
    have = module_files(td).get(m)
    tree = materialize(cfg)
    mdir = (cfg.get("kat") or {}).get("modules_dir", "rtl")
    if not have and os.path.exists(os.path.join(tree, mdir, m + ".v")):
        have = os.path.join(tree, mdir, m + ".v")
    if not have:
        res["checker"] = "pending: module %s not written yet" % m
        if (cfg.get("kat") or {}).get("checker_required"):
            return {"cmd": ["sh", "-c", "echo 'KAT %s: FAIL (module not written yet)'; exit 1" % m]}
        return None
    return {"cmd": spec["kat_cmd"].split() + [have], "cwd": "{tree}", "pass_regex": r"^KAT %s: PASS" % m,
            "verdict_regex": r"^KAT ", "timeout": 900}


def gate(cfg, td, cand_file, W, run):
    """The integration gate on the assembled file: an overlay of the pinned tree (outside every git work tree) with
    the project's merged modules in kat.modules_dir (default rtl/), run through the queue runner ($STEER_HEAVY_RUN or
    kat.integration.heavy_run, default lib/queue_run.sh) unless kat.integration.direct.
    The lane wait is bounded (kat.integration.wait_budget s, default 1800): when it runs out, or on any error or
    signal, the queued ticket is cancelled, the process group killed and the overlay removed (a harness result)."""
    ig = cfg["kat"]["integration"]
    if ig.get("direct"):
        tree = materialize(cfg)
        return run_cmd(dict(ig, cwd="{tree}"), cand_file, tree, W, run)
    tree = materialize(cfg)
    ov = os.path.join(CACHE, "gate", "%s_%d_%d" % (cfg["id"].replace("@", "_"), os.getpid(), int(time.time())))
    shutil.copytree(tree, ov, symlinks=True)
    hr = os.path.expanduser(ig.get("heavy_run") or os.environ.get("STEER_HEAVY_RUN") or os.path.join(LIB, "queue_run.sh"))
    who = "steer %s %d" % (cfg["id"], os.getpid())
    proc, ticket = None, {}
    try:
        for m, f in module_files(td).items():
            os.makedirs(os.path.join(ov, cfg["kat"].get("modules_dir", "rtl")), exist_ok=True)
            shutil.copy(f, os.path.join(ov, cfg["kat"].get("modules_dir", "rtl"), m + ".v"))
        wrap = os.path.join(ov, "steer_candidate_" + os.path.basename(cand_file))
        shutil.copy(cand_file, wrap)
        cmd = [hr, "--who", who, "--kind", "sim", "--mem", ig.get("mem", "6G"), "--desktop-only",
               "--timeout", str(int(ig.get("timeout", 3600))), "--"] + [x.replace("{cand}", wrap) for x in ig["cmd"]]
        proc = subprocess.Popen(cmd, cwd=ov, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", start_new_session=True)
        lines = []

        def pump():
            for l in proc.stdout:
                lines.append(l)
                m = re.search(r"\[queue\]\s+(q\d+)", l)
                if m and "id" not in ticket:
                    ticket["id"] = m.group(1)
                if re.search(r"running on|started|dispatched to", l):
                    ticket["running"] = True
        th = __import__("threading").Thread(target=pump, daemon=True)
        th.start()
        t0, budget, run_t = time.time(), int(ig.get("wait_budget", 1800)), int(ig.get("timeout", 3600)) + 600
        while proc.poll() is None:
            waited = time.time() - t0
            if (not ticket.get("running") and waited > budget) or waited > budget + run_t:
                return False, "HARNESS: the gate did not %s within %d s (ticket %s cancelled)" % (
                    "get a lane" if not ticket.get("running") else "finish", waited, ticket.get("id"))
            time.sleep(2)
        th.join(10)
        out = "".join(lines)
        verdict = [l for l in out.splitlines() if re.search(ig.get("verdict_regex", r"PASS|FAIL"), l)]
        if not verdict:
            return False, "HARNESS: no verdict line (rc=%d): %s" % (proc.returncode, out.strip()[-200:].replace("\n", " | "))
        ok = bool(re.search(ig.get("pass_regex", r"PASS"), out, re.M)) and not (
            ig.get("fail_regex") and re.search(ig["fail_regex"], out, re.M)) and proc.returncode == 0
        return ok, verdict[-1][:300]
    finally:
        if proc is not None and proc.poll() is None:
            if ticket.get("id"):
                subprocess.run([hr, "--cancel", ticket["id"], "--who", who], capture_output=True, timeout=60)
            try:
                os.killpg(proc.pid, 15)
                proc.wait(30)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    pass
        shutil.rmtree(ov, ignore_errors=True)


def run_cmd(spec, cand_file, tree, W, run):
    """Run a checker/integration command ({cand}, {tree}, {W} substituted). Returns (pass, detail line)."""
    sub = lambda s: s.replace("{cand}", cand_file).replace("{tree}", tree).replace("{W}", W)
    cmd = [sub(x) for x in spec["cmd"]]
    if spec.get("uncapped"):
        # the queue runner makes its own capped scope for the job; inside light.sh's 4G scope it would be capped twice
        try:
            p = subprocess.run(cmd, cwd=sub(spec.get("cwd", "{tree}")), capture_output=True, text=True, errors="replace",
                               timeout=int(spec.get("timeout", 1800)) + 3600)
            rc, out = p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            rc, out = 124, "timeout (lane wait included)"
    else:
        rc, out, _ = run(cmd, int(spec.get("timeout", 1800)) + 900, cwd=sub(spec.get("cwd", "{tree}")))
    ok = bool(re.search(spec.get("pass_regex", r"PASS"), out, re.M)) and not (
        spec.get("fail_regex") and re.search(spec["fail_regex"], out, re.M))
    if spec.get("require_rc0", True) and rc != 0:
        ok = False
    verdict = [l for l in out.splitlines() if re.search(spec.get("verdict_regex", r"PASS|FAIL"), l)]
    if not verdict:
        # no verdict line: the command did not run to its end (refused, crashed, timed out) -- a harness problem
        return False, "HARNESS: no verdict line (rc=%d): %s" % (rc, out.strip()[-200:].replace("\n", " | "))
    return ok, verdict[-1][:300]


def section_hint(cfg, detail):
    """The section an integration failure most likely belongs to (kat.integration.section_hints), or None."""
    for h in ((cfg.get("kat") or {}).get("integration") or {}).get("section_hints", []):
        if re.search(h["regex"], detail or "", re.I):
            return int(h["section"])
    return None


# ------------------------------------------------------------------------------------------------ worker self-check
CHECK_PY = '''#!/usr/bin/env python3
# section_check.py -- the worker's self-check for one section: Verilator lint of the file against ports-only stubs
# of the modules it instantiates, then the section's connections against section_spec.json. Do not edit.
import json, os, re, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
@FUNCS@
cfg = json.load(open(os.path.join(HERE, "section_spec.json")))
n, spec, out = cfg["section"], cfg["spec"], cfg["file"]
text = open(os.path.join(HERE, out), errors="replace").read()
body = section_body(text, n)
if body is None:
    print("FAIL: the markers of SECTION %d must appear exactly once" % n); print("FAIL: 1 mismatches"); print("RESULT: FAIL (markers)"); sys.exit(1)
print("== lint (verilator -Wall, ports-only stubs of the instantiated modules)")
p = subprocess.run(["verilator", "--lint-only", "-Wall", "-Wno-fatal", "--top-module", cfg["top"], "-I" + os.path.join(HERE, "inc")]
                   + cfg["defines"] + cfg["flags"] + [os.path.join(HERE, "kat_stubs.v"), os.path.join(HERE, out)],
                   capture_output=True, text=True, cwd=HERE)
lines = [l for l in (p.stdout + p.stderr).splitlines() if "kat_stubs.v" not in l]
errs = [l for l in lines if l.startswith("%Error") and "Exiting due to" not in l]
for l in errs[:8]:
    print(l.replace(HERE + "/", ""))
probs = connectivity(body, spec)
undriven = {m.group(1) for m in (re.search(r"Signal is not driven[^']*'([^']+)'", l) for l in lines) if m}
for net in spec.get("must_drive", []):
    if net.split("[")[0] in undriven:
        probs.append("%s is not driven" % net.split("[")[0])
print("== connections of SECTION %d" % n)
for x in probs[:12]:
    print("FAIL: " + x)
bad = len(errs) + len(probs)
if bad == 0:
    print("  ok: lint clean, every instance, connection, assign and driven net as specified")
    print("RESULT: PASS"); sys.exit(0)
print("FAIL: %d mismatches" % bad)
print("RESULT: FAIL (%s)" % ", ".join(x for x, c in (("lint", errs), ("connections", probs)) if c)); sys.exit(1)
'''


def workspace_selfcheck(cfg, td, ws, n):
    """Layer 1 as the worker's run_selfcheck.sh: include files, ports-only stubs (from the pinned tree, for every
    module the skeleton or this section's spec instantiates), the section's spec and a standalone checker."""
    import inspect
    tree = materialize(cfg)
    L = cfg.get("lint") or {}
    inc = os.path.join(ws, "inc")
    os.makedirs(inc, exist_ok=True)
    for i in L.get("incdirs", []):
        for f in os.listdir(os.path.join(tree, i)):
            if f.endswith((".vh", ".svh")):
                shutil.copy(os.path.join(tree, i, f), os.path.join(inc, f))
    spec = load_spec(cfg, td).get(n, {})
    text = open(os.path.join(ws, cfg["golden_files"][0]), errors="replace").read()
    text += "\n" + "\n".join("%s u_%d ();" % (i["module"], k) for k, i in enumerate(spec.get("instances", [])))
    stubs, _ = make_stubs(cfg, tree, text)
    open(os.path.join(ws, "kat_stubs.v"), "w").write("`default_nettype wire\n// Ports-only stubs (generated); do not edit.\n" + stubs)
    json.dump({"section": n, "spec": spec, "file": cfg["golden_files"][0], "top": L.get("top", cfg["top"]),
               "defines": L.get("defines", []), "flags": L.get("flags", [])},
              open(os.path.join(ws, "section_spec.json"), "w"), indent=1)
    funcs = "\n\n".join(inspect.getsource(f) for f in (_strip, _norm, _named_ports, find_instances, connectivity))
    funcs += "\n\n" + inspect.getsource(SEC._rx) + "\n\n" + inspect.getsource(SEC._open_rx) + "\n\n" + \
        inspect.getsource(SEC.comment_of) + "\n\n" + inspect.getsource(SEC.find).replace("def find(", "def section_find(") + \
        "\n\ndef section_body(text, n):\n    span = section_find(text, n)\n    return None if span is None else text[span[0]:span[1]]\n"
    open(os.path.join(ws, "section_check.py"), "w").write(CHECK_PY.replace("@FUNCS@", funcs))
    files = ["section_check.py", "section_spec.json", "kat_stubs.v"]
    for f in files:
        os.chmod(os.path.join(ws, f), 0o444)
    return "python3 section_check.py", files
