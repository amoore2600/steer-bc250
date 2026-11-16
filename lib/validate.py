#!/usr/bin/env python3
"""validate.py [--no-syn] [--no-gate] <taskdir>... -- golden must PASS, the starting input must FAIL, every coverage mutant must FAIL
(for testbench tasks: every mutant must FAIL equivalence, the reference testbench must PASS).  Gowin synthesis
of the golden is recorded (skipped with --no-syn), with its resource counts (syn.resources: DSP, BSRAM), the limit
grade.py's resources stage holds candidates to.  Section tasks (task.json "sections"): per section, the golden
passes, the start file with that section empty fails, and every mutant of that section (confined to its body) fails.
The golden also goes through the checks after the simulation (lib/hygiene.py: -Wall unless task.json "lint_wall" is
false, header, nettype, non-ASCII, initial blocks, pinned names); a golden that fails them makes the task invalid.
--no-gate: kat sections tasks skip the integration gate (on the skeleton and in the last section's grade).
Writes <taskdir>/validation.json and prints a one-line verdict per task."""
import glob, json, os, re, subprocess, sys

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
import sections as SEC  # noqa: E402
import gen_kat as K  # noqa: E402
NO_SYN = ["--no-syn"] if "--no-syn" in sys.argv else []
NO_GATE = ["--no-gate"] if "--no-gate" in sys.argv else []


def grade(td, f, *extra):
    extra = list(extra) + [x for x in NO_SYN + NO_GATE if x not in extra]
    p = subprocess.run([sys.executable, os.path.join(LIB, "grade.py"), td, f] + extra, capture_output=True, text=True)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"pass": None, "stage": "harness", "detail": (p.stdout + p.stderr)[-300:]}


def section_of(fname):
    m = re.match(r"s(\d+)(_m\d+)?\.", fname)
    return int(m.group(1)) if m else None


def validate_kat_sections(td, cfg):
    """kat sections: licence checks; for every worker section the skeleton (that section empty) must fail layer 1
    (lint or connectivity, never harness); the integration command must exist and fail the empty skeleton, or is
    reported pending (placeholder). No mutants without a reference."""
    v = {"id": cfg["id"], "type": "kat", "mode": "sections"}
    probs = K.check_private(cfg, td)
    ok = not probs
    v["licence_checks"] = probs or "ok"
    sk = os.path.join(td, "skeleton.v")
    v["empty_sections"] = []
    for n in cfg["worker_sections"]:
        r = grade(td, sk, "--no-syn", "--section=%d" % n)
        v["empty_sections"].append({"section": n, **r})
        ok &= (r["pass"] is False and r["stage"] in ("lint", "connect", "struct"))
    ig = (cfg.get("kat") or {}).get("integration") or {}
    if ig.get("cmd") and not ig.get("placeholder") and NO_GATE:
        v["integration_skipped"] = "--no-gate: the integration gate was not run"
    elif ig.get("cmd") and not ig.get("placeholder"):
        r = grade(td, sk, "--no-syn", "--integration")
        v["integration_on_skeleton"] = r
        ok &= (r["pass"] is False and r["stage"] == "integration")
        print("    integration on the empty skeleton: %s" % str(r.get("detail"))[:200])
    else:
        v["integration_pending"] = "no integration command yet: layer 3 is not checked"
    v["checker_unproven"] = "no reference: no section has been shown to pass yet"
    v["valid"] = bool(ok)
    json.dump(v, open(os.path.join(td, "validation.json"), "w"), indent=1)
    print("%-28s valid=%-5s kat(sections %s) empty=%s integration=%s" % (
        cfg["id"], v["valid"], cfg["worker_sections"], ",".join("s%d:%s:%s" % (x["section"], x["pass"], x["stage"])
                                                                for x in v["empty_sections"]),
        "pending" if v.get("integration_pending") else "skipped" if v.get("integration_skipped") else
        "%s:%s" % (v["integration_on_skeleton"]["pass"], v["integration_on_skeleton"]["stage"])))
    for x in v["empty_sections"]:
        print("    s%d: %s" % (x["section"], str(x.get("detail"))[:200]))


def validate_kat(td, cfg):
    """kat task: licence checks; the checker must run on the start file and FAIL it (stage tb, not compile/harness);
    with a reference implementation the reference must PASS and every mutant must FAIL; mutant files in an
    external kat.mutants_dir must FAIL too. Without a reference the checker is reported as unproven."""
    v = {"id": cfg["id"], "type": "kat", "mode": cfg["kat"].get("mode", "testbench")}
    probs = K.check_private(cfg, td) + K.check_derived(cfg, td)
    v["licence_and_derived_checks"] = probs or "ok"
    ok = not probs
    v["start"] = []
    for f in sorted(glob.glob(os.path.join(td, "start", "*"))):
        r = grade(td, f, "--no-syn")
        v["start"].append({"file": os.path.basename(f), **r})
        ok &= (r["pass"] is False and r["stage"] == "tb")
    gl = os.path.join(td, "golden", cfg["golden_files"][0])
    if os.path.exists(gl):
        v["golden"] = grade(td, gl, "--no-resources")
        ok &= bool(v["golden"]["pass"])
    else:
        v["golden"] = {"pass": None, "stage": "none", "detail": "no reference implementation"}
        v["checker_unproven"] = ("no reference implementation: the checker has not yet been shown to accept a correct "
                                 "module; the first PASS (or a reference added to task.json) proves it")
    v["mutants"] = []
    files = sorted(glob.glob(os.path.join(td, "mutants", "*")))
    md = cfg["kat"].get("mutants_dir")
    if md:
        files += sorted(glob.glob(os.path.join(os.path.expanduser(md), "*.v")))
    for f in files:
        r = grade(td, f, "--no-syn")
        v["mutants"].append({"file": f if md and f.startswith(os.path.expanduser(md)) else os.path.basename(f), **r})
        ok &= (r["pass"] is False and r["stage"] in ("tb", "compile", "lint"))
    if cfg.get("mutant_notes") and not files:
        v["mutants_pending"] = "%d described in mutant_notes, to be made from the first passing module" % len(cfg["mutant_notes"])
    v["valid"] = bool(ok)
    json.dump(v, open(os.path.join(td, "validation.json"), "w"), indent=1)
    print("%-28s valid=%-5s kat(%s) start=%s reference=%s(%s) lint_wall=%s mutants=%s%s" % (
        cfg["id"], v["valid"], v["mode"], ",".join("%s:%s" % (x["pass"], x["stage"]) for x in v["start"]),
        v["golden"]["pass"], v["golden"]["stage"], cfg.get("lint_wall", True), ",".join("%s:%s" % (x["pass"], x["stage"]) for x in v["mutants"]) or "-",
        "  CHECKER UNPROVEN (no reference)" if v.get("checker_unproven") else ""))
    for x in v["start"] + v["mutants"] + [v["golden"]]:
        if x.get("detail"):
            print("    %s: %s %s" % (x.get("file", "reference"), x.get("stage"), str(x["detail"])[:220]))
    if probs:
        print("    " + "\n    ".join(probs))


for td in map(os.path.abspath, [a for a in sys.argv[1:] if not a.startswith("--")]):
    cfg = json.load(open(os.path.join(td, "task.json")))
    if cfg["type"] == "kat" and (cfg.get("kat") or {}).get("mode") == "sections":
        validate_kat_sections(td, cfg)
        continue
    if cfg["type"] == "kat":
        validate_kat(td, cfg)
        continue
    v = {"id": cfg["id"]}
    ok = True
    secs = [int(x["n"]) for x in cfg.get("sections", [])]
    if secs:
        gpath = os.path.join(td, "golden", cfg["golden_files"][0])
        gtext = open(gpath).read()
        v["golden_sections"] = []
        for n in secs:
            g = grade(td, gpath, "--section=%d" % n, "--no-resources")
            v["golden_sections"].append({"section": n, **g})
            ok &= bool(g["pass"])
        v["golden"] = dict(v["golden_sections"][0], detail="all sections: " + ", ".join(
            "%d:%s" % (x["section"], x["pass"]) for x in v["golden_sections"]), **{"pass": all(x["pass"] for x in v["golden_sections"])})
    if secs:
        pass
    elif cfg["type"] == "testbench":
        g = grade(td, os.path.join(td, "golden", cfg["golden_files"][0]), "--equiv", "--no-resources")
        v["golden_equiv"] = g
        ok &= bool(g["pass"])
        r = grade(td, os.path.join(td, "ref_tb.v"))
        v["reference_tb"] = r
        ok &= bool(r["pass"])
    else:
        g = grade(td, os.path.join(td, "golden", cfg["golden_files"][0]), "--no-resources")
        v["golden"] = g
        ok &= bool(g["pass"])
    v["start"] = []
    for f in sorted(glob.glob(os.path.join(td, "start", "*"))):
        n = section_of(os.path.basename(f)) if secs else None
        r = grade(td, f, "--no-syn", *(["--section=%d" % n] if n is not None else []))
        v["start"].append({"file": os.path.basename(f), **r})
        ok &= (r["pass"] is False and r["stage"] != "harness")
    v["mutants"] = []
    for f in sorted(glob.glob(os.path.join(td, "mutants", "*"))):
        n = section_of(os.path.basename(f)) if secs else None
        r = grade(td, f, "--no-syn", "--equiv", *(["--section=%d" % n] if n is not None else []))
        if n is not None:
            r["confined"] = SEC.confined(gtext, open(f).read(), n, cfg["language"])
            ok &= r["confined"]
        v["mutants"].append({"file": os.path.basename(f), **r})
        ok &= (r["pass"] is False and r["stage"] in ("sim", "compile", "lint"))
    v["valid"] = bool(ok)
    json.dump(v, open(os.path.join(td, "validation.json"), "w"), indent=1)
    gl = v.get("golden") or v.get("golden_equiv")
    syn = gl.get("syn") or {}
    if secs:
        print("   sections: " + "; ".join("s%d golden=%s" % (x["section"], x["pass"]) for x in v["golden_sections"]))
    print("%-28s valid=%-5s golden=%s(%s) lint_wall=%s start=%s mutants=%s syn=%s warn=%s ex2478=%s dsp=%s bsram=%s" % (
        cfg["id"], v["valid"], gl["pass"], gl["stage"], cfg.get("lint_wall", True) if "lint_wall" in gl else "-",
        ",".join("%s:%s" % (s["pass"], s["stage"]) for s in v["start"]),
        ",".join("%s:%s" % (s["pass"], s["stage"]) for s in v["mutants"]),
        syn.get("syn_pass"), syn.get("syn_warnings"), syn.get("ex2478"), (syn.get("resources") or {}).get("dsp"),
        (syn.get("resources") or {}).get("bsram")))
    if not ok:
        for k in ("golden", "golden_equiv", "reference_tb"):
            if k in v and not v[k]["pass"]:
                print("   ", k, v[k]["stage"], v[k]["detail"][:300])
