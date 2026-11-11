#!/usr/bin/env python3
"""validate.py <taskdir>... -- golden must PASS, the starting input must FAIL, every coverage mutant must FAIL
(for testbench tasks: every mutant must FAIL equivalence, the reference testbench must PASS).  Gowin synthesis
of the golden is recorded.  Writes <taskdir>/validation.json and prints a one-line verdict per task."""
import glob, json, os, subprocess, sys

LIB = os.path.dirname(os.path.abspath(__file__))


def grade(td, f, *extra):
    p = subprocess.run([sys.executable, os.path.join(LIB, "grade.py"), td, f] + list(extra), capture_output=True, text=True)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"pass": None, "stage": "harness", "detail": (p.stdout + p.stderr)[-300:]}


for td in map(os.path.abspath, sys.argv[1:]):
    cfg = json.load(open(os.path.join(td, "task.json")))
    v = {"id": cfg["id"]}
    ok = True
    if cfg["type"] == "testbench":
        g = grade(td, os.path.join(td, "golden", cfg["golden_files"][0]), "--equiv")
        v["golden_equiv"] = g
        ok &= bool(g["pass"])
        r = grade(td, os.path.join(td, "ref_tb.v"))
        v["reference_tb"] = r
        ok &= bool(r["pass"])
    else:
        g = grade(td, os.path.join(td, "golden", cfg["golden_files"][0]))
        v["golden"] = g
        ok &= bool(g["pass"])
    v["start"] = []
    for f in sorted(glob.glob(os.path.join(td, "start", "*"))):
        r = grade(td, f, "--no-syn")
        v["start"].append({"file": os.path.basename(f), **r})
        ok &= (r["pass"] is False and r["stage"] != "harness")
    v["mutants"] = []
    for f in sorted(glob.glob(os.path.join(td, "mutants", "*"))):
        r = grade(td, f, "--no-syn", "--equiv")
        v["mutants"].append({"file": os.path.basename(f), **r})
        ok &= (r["pass"] is False and r["stage"] in ("sim", "compile", "lint"))
    v["valid"] = bool(ok)
    json.dump(v, open(os.path.join(td, "validation.json"), "w"), indent=1)
    gl = v.get("golden") or v.get("golden_equiv")
    syn = gl.get("syn") or {}
    print("%-28s valid=%-5s golden=%s(%s) start=%s mutants=%s syn=%s warn=%s ex2478=%s" % (
        cfg["id"], v["valid"], gl["pass"], gl["stage"],
        ",".join("%s:%s" % (s["pass"], s["stage"]) for s in v["start"]),
        ",".join("%s:%s" % (s["pass"], s["stage"]) for s in v["mutants"]),
        syn.get("syn_pass"), syn.get("syn_warnings"), syn.get("ex2478")))
    if not ok:
        for k in ("golden", "golden_equiv", "reference_tb"):
            if k in v and not v[k]["pass"]:
                print("   ", k, v[k]["stage"], v[k]["detail"][:300])
