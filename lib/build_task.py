#!/usr/bin/env python3
"""build_task.py <taskdir> -- materialise a task from its task.json + spec.md (+ symptom in task.json for bug fixes).

Creates: golden/ (copies of the source files from ~/steer-bc250/src), start/ (the task's starting input: blank module,
buggy file or trivial testbench), mutants/ (coverage or testbench mutants from task.json replacements),
tb_equiv.* (equivalence testbench), prompt.md (the exact user message), grade.sh, and fills line_count.
"""
import json, os, re, shutil, subprocess, sys

LIB = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.expanduser("~/steer-bc250/src")

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


def main():
    td = os.path.abspath(sys.argv[1])
    cfg = json.load(open(os.path.join(td, "task.json")))
    for d in ("golden", "start", "mutants"):
        shutil.rmtree(os.path.join(td, d), ignore_errors=True)
        os.makedirs(os.path.join(td, d))
    # golden: whole file, or one module extracted (golden_extract)
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
    for c in cfg.get("context_src", []):                # context files the model is given (and both sides use)
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
        open(os.path.join(td, "start", "start" + os.path.splitext(gname)[1]), "w").write(start)
        what = "entity" if lang == "vhdl" else "module"
        prompt = ["Task: write the complete %s %s `%s` described below." % ("VHDL" if lang == "vhdl" else "Verilog", what, cfg["top"]), ""]
        prompt += ["Toolchain: " + (VHDL_TOOLS if lang == "vhdl" else VERILOG_TOOLS), ""]
        if cfg.get("context_src"):
            prompt += ["The following files are compiled together with yours and must not be repeated in your answer: "
                       + ", ".join("`%s`" % os.path.basename(c) for c in cfg["context_src"]) + " (shown at the end).", ""]
        prompt += ["Rules: keep the given header exactly (name, generics/parameters, ports, comments). "
                   "Reply with the complete file: the given header followed by your implementation.", ""]
        prompt += ["Specification:", "", spec, "", "Header:", "", "```" + fence, hdr.rstrip(), "```"]
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
        open(os.path.join(td, "mutants", "m%d%s" % (i + 1, ext)), "w").write(apply_repl(gsrc, mu["repl"], "mutant %d" % (i + 1)))
    if cfg["type"] == "testbench":
        cfg.setdefault("tbtask", {})["mutants"] = ["mutants/m%d%s" % (i + 1, ext) for i in range(len(cfg.get("mutants", [])))]
    json.dump(cfg, open(os.path.join(td, "task.json"), "w"), indent=1)
    if "equiv" in cfg:
        gen = "gen_vhtb.py" if lang == "vhdl" else "gen_vtb.py"
        subprocess.run([sys.executable, os.path.join(LIB, gen), td], check=True)
    g = os.path.join(td, "grade.sh")
    open(g, "w").write("#!/usr/bin/env bash\n# grade.sh <candidate file>: exit 0 = PASS; last line = JSON summary\n"
                       "exec python3 \"$HOME/steer-bc250/lib/grade.py\" \"$(dirname \"$(readlink -f \"$0\")\")\" \"$1\" \"${@:2}\"\n")
    os.chmod(g, 0o755)
    print("built", cfg["id"], "lines", cfg["line_count"])


if __name__ == "__main__":
    main()
