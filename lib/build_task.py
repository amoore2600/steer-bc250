#!/usr/bin/env python3
"""build_task.py <taskdir> -- materialise a task from its task.json + spec.md (+ symptom in task.json for bug fixes).

Creates: golden/ (copies of the source files from src/ in this repository), start/ (the task's starting input: blank module,
buggy file or trivial testbench), mutants/ (coverage or testbench mutants from task.json replacements),
tb_equiv.* (equivalence testbench), prompt.md (the exact user message), grade.sh, and fills line_count.
"""
import json, os, re, shutil, subprocess, sys

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
import sections as SEC  # noqa: E402
import gen_kat as K  # noqa: E402
import hygiene as H  # noqa: E402
SRC = os.path.join(os.environ.get("STEER_BC250_BASE") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

VERILOG_TOOLS = ("Your file is checked with `verilator --lint-only -Wall` (Verilator 5.020), compiled with Icarus Verilog 12 "
                 "(`iverilog -g2012`), simulated against a reference implementation with seeded random stimulus (every output "
                 "compared continuously, cycle-exact, power-up values included), and synthesized with GowinSynthesis for a "
                 "GW5AST-138 FPGA (Gowin's SystemVerilog-2017 mode). Use synthesizable Verilog-2005 / SystemVerilog constructs only.")
VHDL_TOOLS = ("Your file is analysed with GHDL 4.1 (`ghdl -a --std=08 -fsynopsys -frelaxed`) and simulated against a reference "
              "implementation with seeded random stimulus (every output compared cycle-exact). Use synthesizable VHDL-2008.")


def apply_repl(text, repls, what):
    for r in repls:
        n = text.count(r["from"])
        if n != r.get("count", 1):
            raise SystemExit("%s: replacement %r matches %d times" % (what, r["from"], n))
        text = text.replace(r["from"], r["to"])
    return text


def extract_module(text, name):
    """Return one module (with any header comment block immediately above it) from a multi-module file."""
    m = re.search(r"^module\s+%s\b.*?^endmodule\b[^\n]*\n" % re.escape(name), text, re.S | re.M)
    if not m:
        raise SystemExit("module %s not found" % name)
    return m


def verilog_header(text, top):
    m = re.search(r"^\s*module\s+%s\b" % re.escape(top), text, re.M)
    i = m.start()
    depth = 0
    j = text.index("(", i)
    # skip a parameter list #( ... ) and the port list ( ... ) up to the terminating ';'
    k = i
    while True:
        ch = text[k]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0 and k > j:
            break
        k += 1
    return text[:k + 1]


def vhdl_header(text, top):
    m = re.search(r"^\s*end\s+(entity\s*)?(%s\s*)?;" % re.escape(top), text[re.search(r"entity\s+%s\s+is" % re.escape(top), text, re.I).start():], re.I | re.M)
    start = re.search(r"entity\s+%s\s+is" % re.escape(top), text, re.I).start()
    return text[:start + m.end()]


def build_sections(cfg, td, gsrc, hdr):
    """Section jobs: start/s<n><ext> is the golden with only section n emptied (the other sections are given, so the
    worker's self-check runs on a complete module), skeleton<ext> has every section empty (the base a project merges
    sections into). Every register/wire a section drives is compared by name in the grader and shown by the
    self-check (task.json sections[].drives: [{name, width}])."""
    lang = cfg["language"]
    if lang == "vhdl":
        raise SystemExit("section tasks are Verilog only")
    ext = os.path.splitext(cfg["golden_files"][0])[1]
    nums = [int(sc["n"]) for sc in cfg["sections"]]
    if sorted(nums) != SEC.numbers(gsrc, lang):
        raise SystemExit("sections %s in task.json, markers %s in the golden" % (sorted(nums), SEC.numbers(gsrc, lang)))
    skel = gsrc
    for n in nums:
        if SEC.find(gsrc, n, lang) is None:
            raise SystemExit("SECTION %d markers missing or repeated in the golden" % n)
        open(os.path.join(td, "start", "s%d%s" % (n, ext)), "w").write(SEC.empty(gsrc, n, lang))
        skel = SEC.empty(skel, n, lang)
    open(os.path.join(td, "skeleton" + ext), "w").write(skel)
    state = []
    for sc in cfg["sections"]:
        for x in sc.get("drives", []):
            if re.search(r"\b%s\b" % re.escape(x["name"]), hdr):
                continue                        # a port: every output is compared anyway
            state.append({"name": x["name"], "expr": x.get("expr", x["name"]), "width": int(x["width"])})
    cfg["equiv"]["compare_state"] = state
    cfg["selfcheck_state"] = [dict(x, compare=True) for x in state]


def write_grade_sh(td):
    g = os.path.join(td, "grade.sh")
    open(g, "w").write("#!/usr/bin/env bash\n# grade.sh <candidate file>: exit 0 = PASS; last line = JSON summary\n"
                       "exec python3 \"${STEER_BC250_BASE:-$(dirname \"$(readlink -f \"$0\")\")/../..}/lib/grade.py\" \"$(dirname \"$(readlink -f \"$0\")\")\" \"$1\" \"${@:2}\"\n")
    os.chmod(g, 0o755)


def spec_text_of(cfg, td):
    """spec.md, rebuilt from spec_from extracts ([{path, start, end, keep_start}]) when the task lists them."""
    spec = []
    for x in cfg.get("spec_from", []):
        t = K.read_source(x["path"], td)
        a = t.index(x["start"])
        b = t.index(x["end"], a + len(x["start"])) if x.get("end") else len(t)
        spec.append(t[a + (0 if x.get("keep_start") else len(x["start"])):b].strip())
    if spec:
        open(os.path.join(td, "spec.md"), "w").write("\n\n".join(spec) + "\n")
    return open(os.path.join(td, "spec.md")).read().strip() if os.path.exists(os.path.join(td, "spec.md")) else ""


def build_kat_sections(td, cfg, spec_text):
    """kat sections: the worker fills one section of a skeleton that has no reference implementation; graded by
    lib/kat_sections.py (lint against stubs + connectivity, optional per-section checker, integration command)."""
    import kat_sections as KS
    sk = K.read_source(cfg["skeleton"], td)
    open(os.path.join(td, "skeleton.v"), "w").write(sk)
    names = {}
    for m in re.finditer(r"^[ \t]*//[ \t]*SECTION[ \t]+(\d+)[ \t]*:[ \t]*([^\n]*)$", sk, re.M):
        names[int(m.group(1))] = m.group(2).strip()
    spec = KS.load_spec(cfg, td)
    cfg["sections"] = []
    for n in cfg["worker_sections"]:
        if SEC.find(sk, n) is None:
            raise SystemExit("SECTION %d markers missing or repeated in the skeleton" % n)
        if n not in spec:
            raise SystemExit("SECTION %d has no entry in the section spec" % n)
        cfg["sections"].append({"n": n, "name": names.get(n, ""), "after": spec[n].get("after", []),
                                "drives": [{"name": x} for x in spec[n].get("must_drive", [])]})
        open(os.path.join(td, "start", "s%d.v" % n), "w").write(sk)
    cfg["line_count"] = sk.count("\n")
    KS.materialize(cfg)
    prompt = ["Task: write SECTION n of the Verilog module `%s` in `%s` (the section job names n): the instances and "
              "wiring the statement below lists for that section." % (cfg["top"], cfg["golden_files"][0]), "",
              "Grading: the file is assembled (the skeleton, every section already finished in this project, and your "
              "section), linted with Verilator against the real port lists of every module it instantiates, and your "
              "section's connections are checked against the statement: every instance, port connection, assign, and "
              "every net the section must drive." + (" Finally the whole file runs in an integration simulation."
                                                     if (cfg["kat"].get("integration") or {}).get("cmd") else ""), "",
              "Rules: write only between the markers of your section; every cross-section wire is already declared at "
              "the top; do not declare new ones unless the statement says so.", "",
              H.repository_rules(cfg, td, "the section check passes"), "",
              "Specification:", "", spec_text]
    open(os.path.join(td, "prompt.md"), "w").write("\n".join(prompt) + "\n")
    for d in ("selfcheck", "kat", "golden", "mutants"):
        if os.path.isdir(os.path.join(td, d)) and not os.listdir(os.path.join(td, d)):
            os.rmdir(os.path.join(td, d))
    json.dump(cfg, open(os.path.join(td, "task.json"), "w"), indent=1)
    write_grade_sh(td)
    print("built", cfg["id"], "(kat sections %s)" % cfg["worker_sections"], "lines", cfg["line_count"])


def build_kat(td, cfg):
    """kat task: the worker writes the module; a known-answer checker grades it (lib/gen_kat.py). Private files are
    checked to live outside the repository and are never copied; the rest is copied into the task (kat/, start/,
    golden/ when a reference implementation is given) so the task does not change when its source does."""
    k = cfg["kat"]
    gname, top = cfg["golden_files"][0], cfg["top"]
    if cfg["language"] != "verilog":
        raise SystemExit("kat tasks are Verilog only")
    for d in ("start", "kat", "selfcheck"):
        shutil.rmtree(os.path.join(td, d), ignore_errors=True)
        os.makedirs(os.path.join(td, d))
    # golden/ and mutants/ of a kat task hold promoted references (a worker-written passing module, hand-made
    # behaviour mutants): never removed here; only the files generated from "reference" below are rewritten
    for d in ("golden", "mutants"):
        os.makedirs(os.path.join(td, d), exist_ok=True)
    probs = K.check_private(cfg, td) + K.check_derived(cfg, td)
    if probs:
        raise SystemExit("kat task %s:\n  " % cfg["id"] + "\n  ".join(probs))
    if k.get("mode") == "sections":
        return build_kat_sections(td, cfg, spec_text_of(cfg, td))
    if cfg.get("start"):
        start = K.read_source(cfg["start"], td)
    else:                                       # the reference implementation's header with an empty body
        hdr = verilog_header(K.read_source(cfg["reference"], td), top)
        start = hdr + "\nendmodule\n" + ("`default_nettype wire\n" if "`default_nettype none" in hdr else "")
    open(os.path.join(td, "start", gname), "w").write(start)
    cfg["line_count"] = start.count("\n")
    spec_text = spec_text_of(cfg, td)
    if k.get("mode", "testbench") == "testbench":
        local = []
        for src in [k["tb"]] + list(k.get("support_files", [])):
            if k.get("private") and k.get("support_private") and src in k.get("support_files", []):
                continue                            # private support file: read at grading time only
            dst = os.path.join("kat", K.source_name(src))
            open(os.path.join(td, dst), "w").write(K.read_source(src, td))
            local.append(dst)
        k["tb_local"], k["support_local"] = local[0], local[1:]
        if k.get("worker_tb", True) and not (k.get("private") and k.get("support_private")):
            files = [os.path.basename(f) for f in local]
            for f in local:
                shutil.copy(os.path.join(td, f), os.path.join(td, "selfcheck", os.path.basename(f)))
            open(os.path.join(td, "selfcheck", "kat_check.sh"), "w").write(K.kat_check_sh(cfg, files))
            json.dump({"task": cfg["id"], "kind": "kat", "language": "verilog", "top": top, "files": ["kat_check.sh"] + files,
                       "hdl_check_cmd": "bash kat_check.sh"}, open(os.path.join(td, "selfcheck", "selfcheck.json"), "w"), indent=1)
        how = ("It is graded by a known-answer checker, the self-checking testbench `%s` (module `%s`), compiled with "
               "Icarus Verilog (`iverilog -g2012`) together with your file%s; the module passes when the checker prints "
               "its PASS verdict line." % (os.path.basename(local[0]), k["tb_top"],
                                           (" and " + ", ".join("`%s`" % os.path.basename(f) for f in local[1:])) if local[1:] else ""))
    else:
        sys.path.insert(0, LIB)
        import gen_vtb
        ports = gen_vtb.ports_of([os.path.join(td, "start", gname)], top, {}, [])
        json.dump({"ports": ports}, open(os.path.join(td, "ports.json"), "w"))
        open(os.path.join(td, "kat_ref_tb.v"), "w").write(K.ref_tb(cfg, ports))
        how = ("It is graded by running it cycle by cycle against a reference implementation of the same function (not "
               "provided), comparing the outputs listed in the statement.")
    for d in ("selfcheck", "kat"):
        if not os.listdir(os.path.join(td, d)):
            os.rmdir(os.path.join(td, d))
    if cfg.get("reference"):
        ref = K.read_source(cfg["reference"], td) if (cfg["reference"].startswith(("git:", "/", "~")) or
                                                      os.path.exists(os.path.join(td, cfg["reference"]))) else \
            open(os.path.join(SRC, cfg["reference"]), errors="replace").read()
        open(os.path.join(td, "golden", gname), "w").write(ref)
        for f in os.listdir(os.path.join(td, "mutants")):
            if re.fullmatch(r"m\d+\.v", f):            # generated by an earlier build from the mutants list
                os.remove(os.path.join(td, "mutants", f))
        for i, mu in enumerate(cfg.get("mutants", [])):
            open(os.path.join(td, "mutants", "m%d.v" % (i + 1)), "w").write(apply_repl(ref, mu["repl"], "mutant %d" % (i + 1)))
        if k.get("mode") == "reference" and "equiv" not in cfg:
            # the worker's self-check is recorded from the reference implementation (never from a private reference)
            cfg["equiv"] = {x: k[x] for x in ("clocks", "stim", "init", "decls", "cycles", "step") if x in k}
            cfg["equiv"]["seeds"] = k.get("seeds", [1])
    elif cfg.get("mutants"):
        raise SystemExit("kat mutants are replacements in the reference implementation: set \"reference\" or list them as mutant_notes")
    hdr = start
    prompt = ["Task: write the complete Verilog module `%s` described below." % top, "",
              "Toolchain: " + VERILOG_TOOLS.replace("simulated against a reference implementation with seeded random stimulus "
                                                    "(every output compared continuously, cycle-exact, power-up values included)",
                                                    "checked by a known-answer checker"), "",
              how, "",
              "Rules: keep the module name and port list exactly as in the start file below. Reply with the complete file.", "",
              H.repository_rules(cfg, td), "",
              "Specification:", "", spec_text, "", "Start file:", "", "```verilog", hdr.rstrip(), "```"]
    open(os.path.join(td, "prompt.md"), "w").write("\n".join(prompt) + "\n")
    json.dump(cfg, open(os.path.join(td, "task.json"), "w"), indent=1)
    write_grade_sh(td)
    probs = K.check_private(cfg, td)
    if probs:
        raise SystemExit("kat task %s after build:\n  " % cfg["id"] + "\n  ".join(probs))
    print("built", cfg["id"], "(kat, %s)" % k.get("mode", "testbench"), "lines", cfg["line_count"])


def main():
    td = os.path.abspath(sys.argv[1])
    cfg = json.load(open(os.path.join(td, "task.json")))
    if H.applies(cfg):
        cfg.setdefault("lint_wall", True)           # task.json "lint_wall": false only when the golden itself fails it
    if cfg["type"] == "kat":
        return build_kat(td, cfg)
    for d in ("golden", "start", "mutants"):
        shutil.rmtree(os.path.join(td, d), ignore_errors=True)
        os.makedirs(os.path.join(td, d))
    # golden: whole file, or one module extracted (golden_extract); golden_local = a file kept in the task directory
    # (e.g. a section-job form of a module, whose equivalence to the source is checked separately)
    if cfg.get("golden_local"):
        gsrc = open(os.path.join(td, cfg["golden_local"]), errors="replace").read()
    else:
        gsrc = open(os.path.join(SRC, cfg["source"]), errors="replace").read()
    if cfg.get("golden_lines"):
        a, b = cfg["golden_lines"]
        gsrc = "".join(gsrc.splitlines(True)[a - 1:b])
    if cfg.get("golden_extract"):
        m = extract_module(gsrc, cfg["golden_extract"])
        lead = cfg.get("golden_extract_lead", "")
        gsrc = lead + gsrc[m.start():m.end()] + cfg.get("golden_extract_tail", "")
    gname = cfg["golden_files"][0]
    open(os.path.join(td, "golden", gname), "w").write(gsrc)
    for extra in cfg.get("golden_more", []):           # further golden files copied as they are
        shutil.copy(os.path.join(SRC, extra), os.path.join(td, "golden", os.path.basename(extra)))
    for c in cfg.get("context_src", []) + cfg.get("context_hidden", []):   # context files the model is given (and both sides use)
        shutil.copy(os.path.join(SRC, c), os.path.join(td, os.path.basename(c)))
    cfg["line_count"] = gsrc.count("\n")
    lang = cfg["language"]
    fence = "vhdl" if lang == "vhdl" else ("systemverilog" if lang == "systemverilog" else "verilog")
    spec = open(os.path.join(td, "spec.md")).read().strip() if os.path.exists(os.path.join(td, "spec.md")) else ""
    t = cfg["type"]
    if t == "rebuild":
        if lang == "vhdl":
            hdr = vhdl_header(gsrc, cfg["top"])
            start = hdr + "\n\narchitecture rtl of %s is\nbegin\nend architecture;\n" % cfg["top"]
        else:
            hdr = verilog_header(gsrc, cfg["top"])
            start = hdr + "\nendmodule\n" + ("`default_nettype wire\n" if "`default_nettype none" in hdr else "")
        if cfg.get("header_override"):
            hdr = open(os.path.join(td, cfg["header_override"])).read()
            start = open(os.path.join(td, cfg["start_override"])).read()
        if cfg.get("sections"):
            build_sections(cfg, td, gsrc, hdr)
        else:
            open(os.path.join(td, "start", "start" + os.path.splitext(gname)[1]), "w").write(start)
        what = "entity" if lang == "vhdl" else "module"
        prompt = ["Task: write the complete %s %s `%s` described below." % ("VHDL" if lang == "vhdl" else "Verilog", what, cfg["top"]), ""]
        prompt += ["Toolchain: " + (VHDL_TOOLS if lang == "vhdl" else VERILOG_TOOLS), ""]
        if cfg.get("context_src"):
            prompt += ["The following files are compiled together with yours and must not be repeated in your answer: "
                       + ", ".join("`%s`" % os.path.basename(c) for c in cfg["context_src"]) + " (shown at the end).", ""]
        if cfg.get("context_hidden"):
            # too big to paste (e.g. a 570 KB microcode ROM): compiled with the answer, interface described in the spec
            prompt += ["Also compiled together with yours, but NOT shown here and not to be read in full (too large; its "
                       "interface is described in the specification): " + ", ".join("`%s`" % os.path.basename(c)
                       for c in cfg["context_hidden"]) + ".", ""]
        prompt += ["Rules: keep the given header exactly (name, generics/parameters, ports, comments). "
                   "Reply with the complete file: the given header followed by your implementation.", ""]
        if H.applies(cfg):
            prompt += ["Also required (checked after the simulation passes): " + H.rules_text(cfg, td) + ".", ""]
        prompt += ["Specification:", "", spec, "", "Header:", "", "```" + fence, hdr.rstrip(), "```"]
        if cfg.get("sections"):
            prompt += ["", "The module is split into labelled sections (`// SECTION n: <name>` ... `// END SECTION n`); "
                       "a section job is given the module with every other section written and fills one section."]
    elif t == "bugfix":
        buggy = apply_repl(gsrc, cfg["bug"], "bug")
        open(os.path.join(td, "start", "buggy" + os.path.splitext(gname)[1]), "w").write(buggy)
        prompt = ["Task: the %s file below contains exactly one bug. Find it and fix it, changing as little as possible."
                  % ("VHDL" if lang == "vhdl" else "Verilog"), ""]
        prompt += ["Toolchain: " + (VHDL_TOOLS if lang == "vhdl" else VERILOG_TOOLS), ""]
        if cfg.get("context_src"):
            prompt += ["These files are compiled together with it and are correct: "
                       + ", ".join("`%s`" % os.path.basename(c) for c in cfg["context_src"]) + " (shown at the end).", ""]
        prompt += ["Observed symptom: " + cfg["symptom"], ""]
        prompt += ["Reply with the complete corrected file.", "", "```" + fence, buggy.rstrip(), "```"]
    elif t == "refactor":
        # the starting file IS the golden (the reference for the cycle-exact compare); the statement (spec.md) says what to
        # restructure, and a structural gate (task.json "cone_check") decides whether it was done
        open(os.path.join(td, "start", "start" + os.path.splitext(gname)[1]), "w").write(gsrc)
        prompt = ["Task: restructure the %s file below for timing, as the statement says, without changing its behaviour: "
                  "every output must stay identical on every clock (cycle-exact against this very file)."
                  % ("VHDL" if lang == "vhdl" else "Verilog"), ""]
        prompt += ["Toolchain: " + (VHDL_TOOLS if lang == "vhdl" else VERILOG_TOOLS), ""]
        if cfg.get("context_src"):
            prompt += ["These files are compiled together with it and must not be changed or repeated in your answer: "
                       + ", ".join("`%s`" % os.path.basename(c) for c in cfg["context_src"]) + " (shown at the end).", ""]
        prompt += ["Statement:", "", spec, ""]
        prompt += ["Reply with the complete restructured file.", "", "```" + fence, gsrc.rstrip(), "```"]
    elif t == "testbench":
        open(os.path.join(td, "start", "trivial_tb.v"), "w").write(
            "module tb;\n    initial begin\n        $display(\"TB_PASS\");\n        $finish;\n    end\nendmodule\n")
        prompt = ["Task: write a self-checking Verilog testbench for the module `%s` below." % cfg["top"], ""]
        prompt += ["How it is run: `iverilog -g2012 -o sim tb.v %s` then `vvp -n sim`, with a 60 second wall-clock limit. "
                   "The module file is compiled alongside your testbench: do not include or copy the module. "
                   "The testbench must print the exact line `TB_PASS` and call `$finish` when every check passed, and print "
                   "`TB_FAIL` when any check fails. It is also run against subtly broken versions of the module (same "
                   "ports, wrong behaviour); a good testbench detects them by checking the outputs exactly, cycle by cycle where "
                   "the behaviour is defined. Use Icarus-compatible Verilog / SystemVerilog only (no classes, no UVM)."
                   % cfg["golden_files"][0], ""]
        if spec:
            prompt += ["Notes: " + spec, ""]
        prompt += ["Reply with the complete testbench file.", "", "```" + fence, gsrc.rstrip(), "```"]
    else:
        raise SystemExit("unknown type " + t)
    for c in cfg.get("context_src", []):
        prompt += ["", "`%s`:" % os.path.basename(c), "", "```" + fence, open(os.path.join(SRC, c), errors="replace").read().rstrip(), "```"]
    open(os.path.join(td, "prompt.md"), "w").write("\n".join(prompt) + "\n")
    # mutants: coverage mutants (equivalence must FAIL) and testbench-task mutants
    ext = os.path.splitext(gname)[1]
    for i, mu in enumerate(cfg.get("mutants", [])):
        mt = apply_repl(gsrc, mu["repl"], "mutant %d" % (i + 1))
        if mu.get("section") is not None:
            # a section mutant must change that section's body and nothing else
            if not SEC.confined(gsrc, mt, int(mu["section"]), lang):
                raise SystemExit("mutant %d is not confined to the body of SECTION %s" % (i + 1, mu["section"]))
            name = "s%d_m%d%s" % (int(mu["section"]), i + 1, ext)
        else:
            name = "m%d%s" % (i + 1, ext)
        open(os.path.join(td, "mutants", name), "w").write(mt)
    if cfg["type"] == "testbench":
        cfg.setdefault("tbtask", {})["mutants"] = ["mutants/m%d%s" % (i + 1, ext) for i in range(len(cfg.get("mutants", [])))]
    json.dump(cfg, open(os.path.join(td, "task.json"), "w"), indent=1)
    if "equiv" in cfg:
        gen = "gen_vhtb.py" if lang == "vhdl" else "gen_vtb.py"
        subprocess.run([sys.executable, os.path.join(LIB, gen), td], check=True)
    write_grade_sh(td)
    print("built", cfg["id"], "lines", cfg["line_count"])


if __name__ == "__main__":
    main()
