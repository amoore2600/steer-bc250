#!/usr/bin/env python3
"""run_agent.py -- OpenCode agent-mode bake-off: one sandboxed OpenCode session per (model, task), graded by the
same hidden task graders as the one-shot run.

  python3 run_agent.py --run-id <id> --model <model-id> [--tasks t1,t2] [--timeout 1200]

Per task, sequentially: build a fresh workspace (only what the one-shot prompt exposed: the starting / buggy / DUT
file, context files, TASK.md; git-initialised), start the host-side LLM forwarder, run
`opencode-cli run --standalone --auto --format json` inside agent/sandbox.sh (bubblewrap) inside lib/heavy.sh (heavy
lane, 4G memory cap, wall-clock timeout), record the event stream and git diff, copy the produced file out, grade it
with the task's grade.sh, append one line to results/agent_<id>/results.jsonl (fsynced).  Resumable: recorded pairs
are skipped.  summary.md compares each task with the one-shot run (--oneshot, default full38_20261028).
"""
import argparse, collections, datetime, glob, hashlib, json, os, re, shutil, subprocess, sys, time

BASE = os.environ.get("STEER_BC250_BASE") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import run_api  # noqa: E402  (thermal(), category(), load_tasks())
sys.path.append(os.path.join(BASE, "lib"))
import sections as SEC  # noqa: E402
import hygiene as HYG  # noqa: E402

OPENCODE = "/opt/OpenCode/resources/opencode-cli"
AGENT = os.path.join(BASE, "agent")
HEAVY = os.path.join(BASE, "lib", "heavy.sh")
INFER_HOST = os.environ.get("BAKEOFF_INFER_HOST", "localhost")
INFER_PORT = os.environ.get("BAKEOFF_INFER_PORT", "8080")
GOWIN_SIMLIB = os.path.join(os.path.expanduser(os.environ.get("GOWIN_HOME", "~/gowin/IDE")), "simlib", "gw5a")

TOOLS_NOTE = """You are working in a sandbox: the current directory /srv/work is the only writable place that is kept.
The HDL tools installed here are verilator (5.020), iverilog / vvp (Icarus 12), ghdl (4.1, mcode) and yosys (0.33).
Use them: lint and compile your file, and write your own throwaway testbenches in this directory to simulate it.
Gowin tools are NOT installed here (vendor synthesis runs only at grading)."""


def output_file(cfg):
    return "tb.v" if cfg["type"] == "testbench" else cfg["golden_files"][0]


def is_kat_sections(cfg):
    return cfg["type"] == "kat" and (cfg.get("kat") or {}).get("mode") == "sections"


def section_cfg(cfg, n):
    """The config of one section job of a section task: id <task>@s<n> (its own workspace, results row and resume key),
    graded with --section=<n>."""
    n = int(n)
    if not any(int(x["n"]) == n for x in cfg.get("sections", [])):
        raise SystemExit("task %s has no SECTION %d" % (cfg["id"], n))
    c = dict(cfg)
    c["task_id"], c["id"], c["section"] = cfg["id"], "%s@s%d" % (cfg["id"], n), n
    return c


def task_cfg_from_dir(td, section=None):
    """A task's config read now from <td>/task.json and <td>/prompt.md (what grade.sh in td reads), as a section job's
    config when section is given."""
    cfg = json.load(open(os.path.join(td, "task.json")))
    cfg["dir"] = td
    cfg["prompt"] = open(os.path.join(td, "prompt.md")).read()
    return section_cfg(cfg, section) if section is not None else cfg


def split_task_name(name):
    """'task@s2' / 'task@2' -> ('task', 2); 'task' -> ('task', None)."""
    if "@" in name:
        t, sec = name.split("@", 1)
        return t, int(sec.lstrip("s"))
    return name, None


def selfcheck_dir(cfg):
    """tasks/<id>/selfcheck/ when the task has a generated self-check testbench (lib/gen_selfcheck.py), else None."""
    d = os.path.join(cfg["dir"], "selfcheck")
    return d if os.path.isfile(os.path.join(d, "selfcheck.json")) else None


def section_file_job(cfg, section_file=True):
    """True when this job runs in section-file mode: a section job (rebuild sections or kat sections), Verilog, with
    section_file on. The worker then edits only section_s<n>.v, the body of its section."""
    return bool(section_file) and cfg.get("section") is not None and cfg["language"] != "vhdl" and \
        (cfg["type"] == "rebuild" or is_kat_sections(cfg))


ASSEMBLED = ".assembled"

SPLICE_PY = """#!/usr/bin/env python3
# section_splice.py -- assembles your section into the full module for the self-check. Do not edit.
#   python3 section_splice.py         writes .assembled/<module file>
#   python3 section_splice.py --map   copies stdin to stdout, pointing line numbers of the assembled file at your files
import os, sys
N, FILE, SECFILE, LANG, MODE = @CFG@
HERE = os.path.dirname(os.path.abspath(__file__))
@SECTIONS@


def _main():
    asm_path = os.path.join(HERE, ".assembled", FILE)
    if MODE == "section_file":     # your file holds only the section's body; the rest is context/<module file>
        base = open(os.path.join(HERE, "context", FILE), errors="replace").read()
    else:                          # your file is the whole module; only SECTION N of it is taken
        base = open(os.path.join(HERE, ".section_base", FILE), errors="replace").read()
    try:
        mine = open(os.path.join(HERE, SECFILE), errors="replace").read()
    except OSError:
        mine = ""
    if sys.argv[1:] == ["--map"]:
        try:
            asm = open(asm_path, errors="replace").read()
        except OSError:
            asm = None
        for line in sys.stdin:
            if asm and MODE == "section_file":
                line = map_assembled_lines(line, asm, base, mine, N, ".assembled/" + FILE, SECFILE, "context/" + FILE, LANG)
            elif asm:
                line = map_whole_lines(line, asm, mine, N, ".assembled/" + FILE, SECFILE, LANG)
            sys.stdout.write(line)
        return 0
    if MODE == "section_file":
        full, prob = splice_file(base, N, mine, LANG)
    else:
        full, prob = splice_whole(base, N, mine, LANG)
        oc = outside_changes(base, mine, N, LANG) if not prob else None
        if oc and (oc[0] or oc[1]):
            print("NOTE: " + whole_file_note(N, *oc))
    if prob:
        print("FAIL: %s: %s" % (SECFILE, prob))
        print("RESULT: FAIL (section file)" if MODE == "section_file" else "RESULT: FAIL (section markers)")
        return 1
    os.makedirs(os.path.dirname(asm_path), exist_ok=True)
    if os.path.exists(asm_path):
        os.chmod(asm_path, 0o644)
        os.remove(asm_path)
    open(asm_path, "w").write(full)
    os.chmod(asm_path, 0o444)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
"""


def spliced_job(cfg):
    """A section job whose self-check and pre-gate run on a module assembled by the harness (Verilog section jobs)."""
    return cfg.get("section") is not None and cfg["language"] != "vhdl" and (cfg["type"] == "rebuild" or is_kat_sections(cfg))


def selfcheck_command(cfg, splice=False):
    """The provided self-check's command line. For a spliced section job it names the assembled module
    .assembled/<file> in place of the module file."""
    scd = selfcheck_dir(cfg)
    cmd = json.load(open(os.path.join(scd, "selfcheck.json")))["hdl_check_cmd"] if scd else "python3 section_check.py"
    gname = cfg["golden_files"][0]
    if splice and scd:
        cmd, k = re.subn(r"(?<![\w./-])%s(?![\w.-])" % re.escape(gname), ASSEMBLED + "/" + gname, cmd)
        if k != 1:
            raise SystemExit("self-check command of %s names %s %d times" % (cfg["id"], gname, k))
    return cmd


HYGIENE_PY = """#!/usr/bin/env python3
# hygiene_check.py -- the grader's checks after the simulation: Verilator -Wall warnings, the file header, the
# `default_nettype setting, non-ASCII characters, initial blocks, dead code (constant-only unused sinks, empty
# begin/end blocks), and internal signals the spec pins by name. Do not edit.
#   python3 hygiene_check.py           prints the problems as FAIL: lines; exit 1 when there is any
#   python3 hygiene_check.py --merge   copies the self-check output from stdin and adds the problems before its RESULT line
import os, re, subprocess, sys, tempfile
CFG = @CFG@
HERE = os.path.dirname(os.path.abspath(__file__))
@SECTIONS@
@CORE@


def _path(x):
    return os.path.join(HERE, os.path.expanduser(x))


def problems():
    f = os.path.join(HERE, CFG["file"])
    try:
        text = open(f, errors="replace").read()
    except OSError:
        return ["%s is missing" % CFG["file"]], 1
    span = body_span_lines(text, CFG["section"], CFG["lang"]) if CFG["section"] is not None else None
    out, n = [], 0
    if CFG["lint_wall"]:
        p = subprocess.run(["verilator"] + wall_flags() + ["--top-module", CFG["top"]] + CFG["lint_extra"]
                           + [_path(x) for x in CFG["lint_files"]] + [f], capture_output=True, text=True, cwd=HERE)
        ww = wall_warnings((p.stdout + p.stderr).splitlines(), f, span)
        if ww:
            out.append("lint -Wall: " + ww[0][1].replace(HERE + "/", "") + "  (the grader allows 0 -Wall warnings%s)"
                       % (" in your section" if span else ""))
            if len(ww) > 1:
                out.append("lint -Wall: %d more warning(s)%s, e.g. %s" % (len(ww) - 1, " in your section" if span else "",
                                                                         ww[1][1].replace(HERE + "/", "")[:160]))
        wp = waiver_problems(text, span)
        out += [m for _, m in wp]
        n += len(ww) + len(wp)
    hp = hygiene_problems(text, CFG["need_spdx"], CFG["need_copyright"], CFG["allow_initial"], span,
                          CFG.get("allow_dead_code", False), CFG.get("allow_display", False), CFG.get("lint_waivers"))
    out += [m for _, _, m in hp]
    n += len(hp)
    if CFG["pinned"]:
        d = tempfile.mkdtemp(prefix="pin_probe_")
        try:
            pf = os.path.join(d, "steer_pin_probe.v")
            open(pf, "w").write(pinned_probe_text(CFG["top"], CFG["pinned"], CFG["params"]))
            p = subprocess.run(["iverilog", "-g2012", "-o", os.path.join(d, "p.vvp"), "-s", "steer_pin_probe", pf]
                               + [_path(x) for x in CFG["pin_files"]] + [f], capture_output=True, text=True)
            o = p.stdout + p.stderr
            if p.returncode == 0:
                q = subprocess.run(["vvp", "-n", os.path.join(d, "p.vvp")], capture_output=True, text=True, cwd=d, timeout=120)
                o = q.stdout + q.stderr
            pp, _ = pinned_parse(o, CFG["pinned"])   # a design that does not compile is reported by the self-check
            out += pp
            n += len(pp)
        finally:
            for x in os.listdir(d):
                os.remove(os.path.join(d, x))
            os.rmdir(d)
    return out, n


def report(probs):
    print("== lint -Wall and file checks (the grader runs them after the simulation)")
    for x in probs[:8]:
        print("FAIL: " + x)
    if len(probs) > 8:
        print("FAIL: (%d more)" % (len(probs) - 8))


def main():
    if sys.argv[1:] != ["--merge"]:
        probs, n = problems()
        if probs:
            report(probs)
            print("RESULT: FAIL (lint/file checks)")
            return 1
        print("RESULT: PASS")
        return 0
    probs = None
    for line in sys.stdin:
        if line.startswith("RESULT:") and probs is None:
            probs, n = problems()
            if probs:
                report(probs)
                if line.startswith("RESULT: PASS"):
                    print("FAIL: %d lint/file problems (everything else passed)" % n)
                    print("RESULT: FAIL (lint/file checks; everything else passed)")
                    continue
        sys.stdout.write(line)
    if probs is None:
        probs, n = problems()
        if probs:
            report(probs)
    sys.stdout.flush()
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main())
"""


def install_hygiene(cfg, ws, out, splice=False):
    """hygiene_check.py (read-only): lib/hygiene.py's checks on the worker's module (the assembled module of a
    section job), with the task's settings. Only for Verilog modules; returns True when installed."""
    import inspect
    if not HYG.applies(cfg):
        return False
    gname = cfg["golden_files"][0]
    L = cfg.get("lint") or {}
    if is_kat_sections(cfg):
        top, files = L.get("top", cfg["top"]), ["kat_stubs.v"]
        extra = ["-Iinc"] + list(L.get("defines", [])) + list(L.get("flags", []))
        pin_files = []
    else:
        top = cfg["top"]
        files = [os.path.basename(c) for c in cfg.get("context_src", []) + cfg.get("context_hidden", [])
                 if c.endswith((".v", ".sv"))]
        pin_files = list(files) + (["prim_sim.v"] if cfg.get("gowin") else [])
        if cfg.get("gowin"):
            files = ["~/.config/opencode/skills/hdl-check/gw_prim_stubs.v"] + files
        extra = []
    need_spdx, need_cpy = HYG.task_needs(cfg, cfg["dir"])
    conf = {"file": (ASSEMBLED + "/" + gname) if splice else out, "section": int(cfg["section"]) if splice else None,
            "lang": cfg["language"], "top": top, "lint_wall": HYG.lint_wall_on(cfg), "lint_files": files,
            "lint_extra": extra, "need_spdx": need_spdx, "need_copyright": need_cpy,
            "allow_initial": bool(cfg.get("allow_initial")), "allow_dead_code": bool(cfg.get("allow_dead_code")),
            "allow_display": bool(cfg.get("allow_display")), "lint_waivers": HYG.lint_waivers(cfg),
            "pinned": cfg.get("pinned_names") or [], "params": cfg.get("pinned_params") or {}, "pin_files": pin_files}
    hp = os.path.join(ws, "hygiene_check.py")
    if os.path.exists(hp):
        os.chmod(hp, 0o644)
    open(hp, "w").write(HYGIENE_PY.replace("@CFG@", repr(conf)).replace("@SECTIONS@", inspect.getsource(SEC))
                        .replace("@CORE@", HYG.core_source()))
    os.chmod(hp, 0o444)
    return True


def hygiene_rules_text(cfg, out):
    """The TASK.md paragraph on the checks hygiene_check.py runs (the grader's, after the simulation)."""
    return ("After the simulation passes, the grader also requires, and `bash run_selfcheck.sh` checks the same "
            "(`hygiene_check.py`, do not edit): " + HYG.rules_text(cfg, cfg["dir"]) + ".")


def selfcheck_script(cfg, out, cmd, snapshot=True, splice=False, hygiene=False):
    """run_selfcheck.sh: optionally saves the design file as checked (.sc/NNNN, the run id is printed); for a spliced
    section job it first assembles the module (section_splice.py) and maps line numbers back to the worker's files.
    With hygiene, the output passes through hygiene_check.py --merge, which adds the -Wall and file-check problems
    before the RESULT line (a PASS with problems becomes RESULT: FAIL)."""
    gname = cfg["golden_files"][0]
    s = ["#!/bin/bash", "# Runs the provided self-check %s. Do not edit." % (
        ("on the module assembled from context/%s and %s" % (gname, out) if out != gname else
         "on the module assembled from SECTION %s of %s" % (cfg.get("section"), out)) if splice else "against your design"),
         "cd \"$(dirname \"$0\")\" || exit 2"]
    if snapshot:
        s.append("mkdir -p .sc; n=$(printf %%04d $(ls .sc | wc -l)); cp %s .sc/$n 2>/dev/null" % out)
    done = "echo \"(selfcheck run id $n)\"; " if snapshot else ""
    hyg = " | python3 hygiene_check.py --merge" if hygiene else ""
    if splice:
        s += ["python3 section_splice.py || { %sexit 1; }" % done,
              "%s 2>&1%s | python3 section_splice.py --map" % (cmd, hyg)]
    else:
        s += ["%s 2>&1%s" % (cmd, hyg) if hygiene else cmd]
    if hygiene:
        s += ["ps=(\"${PIPESTATUS[@]}\"); rc=${ps[0]}; [ \"${ps[1]}\" = 0 ] || rc=1"]
    else:
        s += ["rc=${PIPESTATUS[0]}" if splice else "rc=$?"]
    s += ([done.rstrip("; ")] if snapshot else []) + ["exit $rc"]
    return "\n".join(s) + "\n"


def install_splice(cfg, ws, n, fname, mode):
    """section_splice.py (read-only) and, for kat sections, section_spec.json pointing at the assembled module."""
    import inspect
    gname = cfg["golden_files"][0]
    sp = os.path.join(ws, "section_splice.py")
    open(sp, "w").write(SPLICE_PY.replace("@CFG@", repr((n, gname, fname, cfg["language"], mode)))
                        .replace("@SECTIONS@", inspect.getsource(SEC)))
    os.chmod(sp, 0o444)
    spec = os.path.join(ws, "section_spec.json")
    if os.path.exists(spec):                          # kat sections: section_check.py reads the assembled module
        d = json.load(open(spec))
        d["file"] = ASSEMBLED + "/" + gname
        os.chmod(spec, 0o644)
        json.dump(d, open(spec, "w"), indent=1)
        os.chmod(spec, 0o444)


def make_section_file(cfg, ws, n):
    """Section-file mode: the workspace's full module file becomes context/<file> (read-only, section n replaced by a
    placeholder line) and section_s<n>.v (the section's marker line and its current body). Returns the section file."""
    gname = cfg["golden_files"][0]
    full = open(os.path.join(ws, gname), errors="replace").read()
    os.remove(os.path.join(ws, gname))
    fname = SEC.section_file_name(n, os.path.splitext(gname)[1])
    open(os.path.join(ws, fname), "w").write(SEC.section_file_text(full, n, cfg["language"]))
    os.makedirs(os.path.join(ws, "context"), exist_ok=True)
    ctx = os.path.join(ws, "context", gname)
    open(ctx, "w").write(SEC.context_text(full, n, fname, cfg["language"]))
    os.chmod(ctx, 0o444)
    install_splice(cfg, ws, n, fname, "section_file")
    return fname


def make_section_base(cfg, ws, n):
    """Whole-file mode for a section job: the start file is kept as .section_base/<file> (read-only); the self-check
    takes only SECTION n from the worker's file into it, as the grader does."""
    gname = cfg["golden_files"][0]
    os.makedirs(os.path.join(ws, ".section_base"), exist_ok=True)
    b = os.path.join(ws, ".section_base", gname)
    shutil.copy(os.path.join(ws, gname), b)
    os.chmod(b, 0o444)
    install_splice(cfg, ws, n, gname, "whole")
    return gname


def section_file_what(cfg, n, fname):
    gname = cfg["golden_files"][0]
    sec = [x for x in cfg["sections"] if int(x["n"]) == n][0]
    drives = ", ".join("`%s`" % x["name"] for x in sec.get("drives", [])) or "see the statement"
    kat = is_kat_sections(cfg)
    return ("`%s` is YOUR file and the only graded file: the body of SECTION %d (%s) of the module `%s`, and nothing "
            "else. Its first line is the section's marker line (`// SECTION %d: ...`); keep that line exactly as it is "
            "and write the section's %s below it. %sNo module header, no `endmodule`, no other SECTION markers in it.\n\n"
            "`context/%s` is the whole module, READ-ONLY, with every other section in place and the line "
            "`// <<< your section goes here, see %s >>>` where your section goes. Read it for the declarations and the "
            "other sections; never edit, copy or rewrite it. The harness puts the body of `%s` in place of that line "
            "(`python3 section_splice.py` writes the assembled module to `.assembled/%s`); the self-check and the grading "
            "run on the assembled module, and line numbers in their messages point at `%s` or `context/%s`. Where the "
            "statement below talks about the whole file, its header or the section markers, the harness handles that: you "
            "write only `%s`. Every register and wire is already declared at the top of the module, so do not declare new "
            "ones. This section must drive: %s." % (
                fname, n, sec["name"], cfg["top"], n, "instances and wiring" if kat else "code",
                "The comment lines right under the marker line are the section's specification: keep them. " if kat else "",
                gname, fname, fname, gname, fname, gname, fname, drives))


def build_workspace(cfg, ws, agents_md=None, provide_tests=False, section_file=False):
    td = cfg["dir"]
    os.makedirs(ws)
    gname = cfg["golden_files"][0]
    secfile = section_file_job(cfg, section_file)
    splice = spliced_job(cfg)
    if cfg["type"] == "rebuild" and cfg.get("section") is not None:
        n = int(cfg["section"])
        src = os.path.join(td, "start", "s%d%s" % (n, os.path.splitext(gname)[1]))
        shutil.copy(src, os.path.join(ws, gname))
        sec = [x for x in cfg["sections"] if int(x["n"]) == n][0]
        drives = ", ".join("`%s`" % x["name"] for x in sec.get("drives", []))
        what = ("`%s` is the whole module with every section written except SECTION %d (%s): the lines between "
                "`// SECTION %d: ...` and `// END SECTION %d` are empty. Write that section's code between those two "
                "marker lines and keep both markers. Only the text between the markers is graded (it is put into the "
                "reference module in place of that section); every register and wire is already declared at the top, "
                "so do not declare new ones. This section must drive: %s." % (gname, n, sec["name"], n, n, drives or "see the statement"))
    elif is_kat_sections(cfg):
        n = int(cfg["section"])
        base = os.path.join(td, "section_base.v")
        shutil.copy(base if os.path.exists(base) else os.path.join(td, "skeleton.v"), os.path.join(ws, gname))
        sec = [x for x in cfg["sections"] if int(x["n"]) == n][0]
        what = ("`%s` is the module with its other sections in place; SECTION %d (%s) is yours: write its instances and "
                "wiring between the line `// SECTION %d: ...` and the next `// SECTION` line, keeping both lines and the "
                "comment lines at the top of the section (they are its specification). Only that text is graded. It must "
                "drive: %s." % (gname, n, sec["name"], n, ", ".join("`%s`" % x["name"] for x in sec.get("drives", [])) or "see below"))
    elif cfg["type"] == "kat":
        shutil.copy(os.path.join(td, "start", gname), os.path.join(ws, gname))
        what = ("`%s` is the start file: the module's name and ports, with no behaviour yet. Write the complete module into "
                "that file, keeping the module name and the port list exactly. It is graded by a known-answer checker "
                "(see the statement below)." % gname)
    elif cfg["type"] == "rebuild":
        sk = os.path.join(td, "skeleton" + os.path.splitext(gname)[1])
        src = sk if cfg.get("sections") and os.path.exists(sk) else glob.glob(os.path.join(td, "start", "*"))[0]
        shutil.copy(src, os.path.join(ws, gname))
        what = "`%s` contains the module/entity header (name, parameters/generics, ports, comments) with an empty body. " \
               "Write the complete implementation into that file, keeping the header exactly." % gname
    elif cfg["type"] == "bugfix":
        src = glob.glob(os.path.join(td, "start", "*"))[0]
        shutil.copy(src, os.path.join(ws, gname))
        what = "`%s` contains exactly one bug. Fix it in place, changing as little as possible." % gname
    elif cfg["type"] == "refactor":
        src = glob.glob(os.path.join(td, "start", "*"))[0]
        shutil.copy(src, os.path.join(ws, gname))
        what = ("`%s` is a working module. Restructure it in place as the statement below says; its outputs must stay "
                "identical on every clock (cycle-exact against the file as given)." % gname)
    else:
        shutil.copy(os.path.join(td, "golden", gname), os.path.join(ws, gname))
        what = ("`%s` is the module under test (do not edit it). Write the self-checking testbench into `tb.v` "
                "(it will be compiled as `iverilog -g2012 -o sim tb.v %s` and run with `vvp -n sim`)." % (gname, gname))
    for c in cfg.get("context_src", []):
        shutil.copy(os.path.join(BASE, "src", c), os.path.join(ws, os.path.basename(c)))
    for c in cfg.get("context_hidden", []):                     # given, compiled, never pasted into the prompt
        dst = os.path.join(ws, os.path.basename(c))
        shutil.copy(os.path.join(BASE, "src", c), dst)
        os.chmod(dst, 0o444)
    prim_note = ""
    if cfg.get("gowin"):
        prim = "prim_sim.vhd" if cfg["language"] == "vhdl" else "prim_sim.v"
        dst = os.path.join(ws, prim)
        shutil.copy(os.path.join(GOWIN_SIMLIB, prim), dst)
        os.chmod(dst, 0o444)
        prim_note = ("`%s` is Gowin's own GW5A simulation library (read-only context, do not edit or copy it into your "
                     "answer): it defines the vendor primitives with their real ports and parameters. The hidden grader "
                     "simulates your file together with this same library%s." % (prim, " (analysed into the work library "
                     "with `ghdl -a --std=08 -fsynopsys -frelaxed`)" if prim.endswith(".vhd") else
                     " (`iverilog -g2012 ... prim_sim.v`; Verilator cannot parse it)"))
    out = output_file(cfg)
    tests_note = []
    scd = selfcheck_dir(cfg) if provide_tests else None
    if provide_tests and is_kat_sections(cfg) and cfg.get("section") is not None:
        import kat_sections
        cmd, files = kat_sections.workspace_selfcheck(cfg, td, ws, int(cfg["section"]))
        if secfile:
            out = make_section_file(cfg, ws, int(cfg["section"]))
        elif splice:
            make_section_base(cfg, ws, int(cfg["section"]))
        hyg = install_hygiene(cfg, ws, out, splice)
        wrapper = os.path.join(ws, "run_selfcheck.sh")
        open(wrapper, "w").write(selfcheck_script(cfg, out, cmd, snapshot=False, splice=splice, hygiene=hyg))
        os.chmod(wrapper, 0o555)
        tests_note = ["## Section check (provided)", "",
                      "Run `bash run_selfcheck.sh` (no arguments) from this directory. It lints %s with Verilator against "
                      "the real port lists of every module it instantiates (`kat_stubs.v`, `inc/`) and checks your section's "
                      "instances, port connections, assigns and driven nets against `section_spec.json`. It ends with "
                      "`RESULT: PASS` or `RESULT: FAIL (...)` after `FAIL:` lines naming each problem. Keep fixing until it "
                      "prints `RESULT: PASS`. Do not edit the provided files." % (
                          "the module assembled from `context/%s` and your `%s`" % (gname, out) if secfile else
                          "the module assembled from SECTION %s of `%s`" % (cfg["section"], out) if splice else "`%s`" % out), ""]
    elif scd:
        sc = json.load(open(os.path.join(scd, "selfcheck.json")))
        for f in sc["files"]:                                   # testbench + vector file only (no README / seeds)
            shutil.copy(os.path.join(scd, f), os.path.join(ws, f))
        for f in sc["files"]:                                   # provided tests are read-only for the worker
            os.chmod(os.path.join(ws, f), 0o444)
        tb, vec = sc["files"][0], sc["files"][1]
        if secfile:
            out = make_section_file(cfg, ws, int(cfg["section"]))
        elif splice:
            make_section_base(cfg, ws, int(cfg["section"]))
        hyg = install_hygiene(cfg, ws, out, splice)
        wrapper = os.path.join(ws, "run_selfcheck.sh")             # one command, no arguments to get wrong
        open(wrapper, "w").write(selfcheck_script(cfg, out, selfcheck_command(cfg, splice), snapshot=False, splice=splice,
                                                  hygiene=hyg))
        os.chmod(wrapper, 0o555)
        if sc.get("kind") == "kat":
            tests_note = ["## Known-answer checker (provided)", "",
                          "The checker that grades this task is provided: `%s`%s. Run it, exactly like this and with no "
                          "arguments, from this directory:" % (sc["files"][1], (" with " + ", ".join("`%s`" % f for f in sc["files"][2:]))
                                                              if sc["files"][2:] else ""), "",
                          "    bash run_selfcheck.sh", "",
                          "It lints `%s`, compiles it with the checker, runs it, prints the checker's FAIL lines, and ends with "
                          "`RESULT: PASS` or `RESULT: FAIL (...)`. Keep fixing `%s` until it prints `RESULT: PASS`; do not "
                          "finish on a FAIL. Do not edit the checker, its support files, `kat_check.sh` or `run_selfcheck.sh`."
                          % (out, out), ""]
        else:
            tests_note = ["## Self-check testbench (provided)", "",
                          "A self-check testbench with known-correct expected outputs is provided: `%s` (its expected values are in "
                          "`%s`). It drives `%s` with a fixed set of random inputs and compares every output, cycle by cycle, against "
                          "values recorded from a correct reference implementation. Run it, exactly like this and with no arguments, "
                          "from this directory:" % (tb, vec, cfg["top"]), "",
                          "    bash run_selfcheck.sh", "",
                          "It lints, compiles and simulates your `%s` with the testbench and ends with one line, `RESULT: PASS` or "
                          "`RESULT: FAIL (...)`. On a mismatch it prints the FIRST mismatch only: its cycle, every input at that "
                          "point, and expected / got / wrong bits for each output that differs; then each failing output with its "
                          "mismatch count. Fix the cause of that first mismatch. Keep fixing "
                          "`%s` and re-running `bash run_selfcheck.sh` until it prints `RESULT: PASS`; do not finish on a FAIL. "
                          "Do not edit `run_selfcheck.sh`, `%s` or `%s`. It covers one set of random inputs only; the final grading "
                          "uses different random inputs, so make the design correct in general, not just for these vectors."
                          % (out, out, tb, vec), ""]
    if secfile and out == gname:
        out = make_section_file(cfg, ws, int(cfg["section"]))
    elif splice and not secfile and not os.path.exists(os.path.join(ws, "section_splice.py")):
        make_section_base(cfg, ws, int(cfg["section"]))
    tools_note = TOOLS_NOTE
    if splice and not secfile:
        what += (" Only SECTION %s of `%s` is used: the self-check and the grading put that section into the file as "
                 "given (every other line comes from the starting file), so changes outside it are ignored."
                 % (cfg["section"], out))
    if secfile:
        what = section_file_what(cfg, int(cfg["section"]), out)
        tools_note = TOOLS_NOTE.replace(
            "Use them: lint and compile your file, and write your own throwaway testbenches in this directory to simulate it.",
            "`%s` alone is not a module, so never lint or compile it by itself: %s." % (
                out, "use the tools through the provided self-check (`bash run_selfcheck.sh`), which assembles the module"
                if provide_tests else "run `python3 section_splice.py`, then lint and compile `.assembled/%s`" % gname))
    if tests_note and os.path.exists(os.path.join(ws, "hygiene_check.py")):
        tests_note = tests_note[:-1] + [hygiene_rules_text(cfg, out), ""]
    if agents_md:
        shutil.copy(agents_md, os.path.join(ws, "AGENTS.md"))   # project-level rules, loaded by OpenCode with the global ones
    lines = ["# Task %s" % cfg["id"], "", what, "", tools_note, ""] + ([prim_note, ""] if prim_note else []) + [
             "Grading (hidden, after you finish): only `%s` is taken from this directory and checked exactly as the "
             "original task statement below describes. Other files you create are ignored." % out, "",
             "When the statement below says to reply with the file or a code block, write the file `%s` instead. "
             "Finish by leaving the final version of `%s` in place." % (out, out), ""] + tests_note + [
             "## Original task statement", "", cfg["prompt"].rstrip(), ""]
    open(os.path.join(ws, "TASK.md"), "w").write("\n".join(lines))
    g = ["git", "-C", ws, "-c", "user.name=bakeoff", "-c", "user.email=bakeoff@localhost"]
    subprocess.run(g[:3] + ["init", "-q"], check=True)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-q", "-m", "task start"], check=True)
    if splice:
        with open(os.path.join(ws, ".git", "info", "exclude"), "a") as f:
            f.write(ASSEMBLED + "/\n")
        # the harness's own copy of the base (the context, or the start file): the final splice and the pre-gate
        # never read the worker's copy
        shutil.copy(os.path.join(ws, "context", gname) if secfile else os.path.join(ws, ".section_base", gname),
                    os.path.join(ws, ".git", "steer_context_" + gname))
    return out


def classify_shell(cmd):
    c = cmd or ""
    kinds = []
    if re.search(r"\bverilator\b", c):
        kinds.append("verilator_build" if re.search(r"--(binary|cc|exe|build)\b", c) else "verilator_lint")
    if re.search(r"\bghdl\s+(-s|-a|analy)", c):
        kinds.append("ghdl_analyse")
    if re.search(r"\bghdl\s+(-e|-r|--elab-run|elab)", c):
        kinds.append("ghdl_elab_run")
    if re.search(r"\biverilog\b", c):
        kinds.append("iverilog_compile")
    if re.search(r"\bvvp\b", c):
        kinds.append("vvp_sim")
    if re.search(r"\byosys\b", c):
        kinds.append("yosys")
    return kinds or ["other_shell"]


def parse_events(path):
    ev = []
    for l in open(path, errors="replace"):
        l = l.strip()
        if l.startswith("{"):
            try:
                ev.append(json.loads(l))
            except ValueError:
                pass
    tools = collections.Counter()
    shell = collections.Counter()
    tok = collections.Counter()
    trace, final = [], ""
    steps = 0
    fin = 0
    errors = []
    for e in ev:
        t, p = e.get("type"), e.get("part", {}) or {}
        if t == "step_start":
            steps += 1
        elif t == "step_finish":
            fin += 1
            tk = p.get("tokens") or {}
            for k in ("input", "output", "reasoning"):
                tok[k] += tk.get(k) or 0
            tok["cache_read"] += (tk.get("cache") or {}).get("read") or 0
        elif t == "tool_use":
            name = p.get("tool")
            tools[name] += 1
            st = p.get("state") or {}
            inp = st.get("input") or {}
            if name in ("bash", "shell"):
                for k in classify_shell(inp.get("command")):
                    shell[k] += 1
                desc = "$ " + str(inp.get("command"))[:160]
            elif name in ("edit", "write", "patch", "multiedit"):
                desc = "%s %s" % (name, inp.get("filePath") or inp.get("path") or "")
            else:
                desc = "%s %s" % (name, json.dumps(inp)[:120])
            out = str(st.get("output") or st.get("error") or "")
            trace.append({"tool": name, "status": st.get("status"), "call": desc,
                          "out": out[:300].replace("\n", " | ")})
        elif t == "text":
            final = p.get("text") or final
        elif t == "error":
            errors.append(json.dumps(e)[:400])
    return {"steps": steps, "finished_steps": fin, "tool_calls": dict(tools), "shell_kinds": dict(shell), "tokens": dict(tok),
            "final_message": final[:2000], "trace": trace, "events": len(ev), "event_errors": errors}


def write_summary(rd, refs):
    """refs: one-shot runs shown next to the agent results, e.g. full38_20261028 (temperature 0) and the one-shot
    vendor-sampling run."""
    rows = run_api.load_rows(os.path.join(rd, "results.jsonl"))
    refrows = {}
    for ref in refs:
        rr = {}
        for r in run_api.load_rows(os.path.join(BASE, "results", ref, "results.jsonl")):
            rr.setdefault((r["model"], r["task"]), []).append(r)
        refrows[ref] = rr

    def simruns(rs):
        """Simulation runs (iverilog compiles, vvp, ghdl -e/-r) summed over samples."""
        t = 0
        for r in rs:
            k = r.get("shell_kinds") or {}
            t += k.get("iverilog_compile", 0) + k.get("vvp_sim", 0) + k.get("ghdl_elab_run", 0)
        return t

    agent_refs = [ref for ref in refs if any("shell_kinds" in x for v in refrows[ref].values() for x in v)]

    def kn(rs):
        return "%d/%d" % (sum(r["status"] == "PASS" for r in rs), len(rs)) if rs else "-"
    L = ["# Agent-mode bake-off: %s" % os.path.basename(rd), "",
         "Generated %s. References (one-shot, same tasks, same graders): %s. k/N = passing samples of N; "
         "pass@N = solved by any sample." % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), ", ".join(refs)), ""]
    for m in sorted({r["model"] for r in rows}):
        rs = [r for r in rows if r["model"] == m]
        tasks = sorted({r["task"] for r in rs})
        n = max(r.get("samples_total", 1) for r in rs)
        npass = sum(r["status"] == "PASS" for r in rs)
        pak = sum(any(r["status"] == "PASS" for r in rs if r["task"] == t) for t in tasks)
        L += ["## %s" % m, "",
              "Agent: mean pass rate %.1f%% (%d/%d samples), pass@%d %d/%d tasks, %d samples per task. Timeouts: %d. "
              "Total wall %.1f min. Simulation runs: %d, samples that simulated %d/%d." % (100.0 * npass / len(rs), npass, len(rs), n, pak, len(tasks), n,
                                        sum(bool(r.get("timed_out")) for r in rs), sum(r.get("wall_s") or 0 for r in rs) / 60,
                                        simruns(rs), sum(1 for r in rs if simruns([r]) > 0), len(rs))]
        for ref in refs:
            rr = [x for t in tasks for x in refrows[ref].get((m, t), [])]
            if rr:
                L.append("Reference %s: mean pass rate %.1f%% (%s), pass@any %d/%d tasks%s." % (
                    ref, 100.0 * sum(x["status"] == "PASS" for x in rr) / len(rr), kn(rr),
                    sum(any(x["status"] == "PASS" for x in refrows[ref].get((m, t), [])) for t in tasks), len(tasks),
                    (", simulation runs %d, samples that simulated %d/%d" % (simruns(rr), sum(1 for x in rr if simruns([x]) > 0), len(rr)))
                    if ref in agent_refs else ""))
        L += ["", "Sampling forced by the forwarder: %s; server /slots after each sample: %s." % (
                  sorted({json.dumps(r.get("sampling_sent"), sort_keys=True) for r in rs}),
                  sorted({json.dumps(r.get("server_slot_sampling"), sort_keys=True) for r in rs})), "",
              "| task | agent k/N | per sample (P/F, stage) | " + " | ".join(refs) + " | mean wall (s) | mean tool calls | lint / compile / sim runs (sum) | sim runs: this run" +
              "".join(" / %s" % ref for ref in agent_refs) + " | samples that simulated: this run" + "".join(" / %s" % ref for ref in agent_refs) + " | peak tctl |",
              "|" + "---|" * (9 + len(refs))]
        for t in tasks:
            ts = sorted((r for r in rs if r["task"] == t), key=lambda r: r.get("sample", 0))
            sk = collections.Counter()
            for r in ts:
                sk.update(r.get("shell_kinds") or {})
            lint = sk["verilator_lint"] + sk["ghdl_analyse"]
            comp = sk["iverilog_compile"] + sk["verilator_build"]
            sim = sk["vvp_sim"] + sk["ghdl_elab_run"]
            per = ", ".join(("P" if r["status"] == "PASS" else "%s(%s%s)" % (r["status"][0], r.get("stage"),
                             ",timeout" if r.get("timed_out") else "")) for r in ts)
            def simulated(rs):
                return "%d/%d" % (sum(1 for r in rs if simruns([r]) > 0), len(rs))
            L.append("| %s | %s | %s | %s | %.0f | %.1f | %d / %d / %d | %s | %s | %s |" % (
                t, kn(ts), per, " | ".join(kn(refrows[ref].get((m, t), [])) for ref in refs),
                sum(r.get("wall_s") or 0 for r in ts) / len(ts), sum(sum((r.get("tool_calls") or {}).values()) for r in ts) / len(ts),
                lint, comp, sim, " / ".join([str(simruns(ts))] + [str(simruns(refrows[ref].get((m, t), []))) for ref in agent_refs]),
                " / ".join([simulated(ts)] + [simulated(refrows[ref].get((m, t), [])) for ref in agent_refs]),
                max(((r.get("thermal") or {}).get("tctl_c") or 0) for r in ts)))
        L.append("")
    open(os.path.join(rd, "summary.md"), "w").write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--oneshot", default="full38_20261028", help="comma-separated one-shot runs shown as references")
    ap.add_argument("--grade-timeout", type=int, default=2400)
    ap.add_argument("--sampling", default="vendor", choices=["t0", "vendor", "vendor_high", "none"],
                    help="sampling forced on every chat request by the host-side forwarder (run_api.sampling_for)")
    ap.add_argument("--samples", type=int, default=1, help="independent samples per task (fresh workspace and session each)")
    ap.add_argument("--skills", default="on", choices=["on", "off"], help="off hides ~/.config/opencode/skills inside the sandbox")
    ap.add_argument("--preload-skills", action="store_true",
                    help="mention @hdl-check (and @gowin-primitives on Gowin tasks) in the prompt so OpenCode loads them up front")
    ap.add_argument("--project-agents", default="", help="file copied into every workspace as AGENTS.md (project rules)")
    ap.add_argument("--provide-tests", action="store_true",
                    help="copy tasks/<id>/selfcheck/ (golden-derived self-check testbench, lib/gen_selfcheck.py) into the workspace "
                         "and tell the agent in TASK.md how to run it; grading still uses the hidden test on other seeds")
    a = ap.parse_args()
    rd = os.path.join(BASE, "results", "agent_" + a.run_id)
    wsroot = os.path.join(BASE, "agent_ws", a.run_id)
    for d in ("cand", "grade", "events"):
        os.makedirs(os.path.join(rd, d), exist_ok=True)
    os.makedirs(wsroot, exist_ok=True)
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
    tasks = run_api.load_tasks([t for t in a.tasks.split(",") if t], False)
    samp = run_api.sampling_for(a.model, a.sampling)
    refs = [x for x in a.oneshot.split(",") if x]
    print("agent run %s: %s x %d tasks x %d samples, sampling %s, %d already done" % (
        a.run_id, a.model, len(tasks), a.samples, samp, len(done)), flush=True)
    for cfg, si in [(c, i) for c in tasks for i in range(a.samples)]:
        if (a.model, cfg["id"], si) in done:
            continue
        tag = "%s__%s__s%d" % (a.model, cfg["id"], si)
        base = os.path.join(wsroot, tag)
        shutil.rmtree(base, ignore_errors=True)
        ws, oc = os.path.join(base, "work"), os.path.join(base, "oc")
        sk = os.path.join(BASE, "agent_ws", "sock", hashlib.sha1(("%s/%s" % (a.run_id, tag)).encode()).hexdigest()[:10])
        shutil.rmtree(sk, ignore_errors=True)               # short path: AF_UNIX socket paths are limited to 107 bytes
        os.makedirs(oc)
        os.makedirs(sk)
        agents_md = os.path.abspath(os.path.expanduser(a.project_agents)) if a.project_agents else None
        out = build_workspace(cfg, ws, agents_md, provide_tests=a.provide_tests)
        tests_provided = bool(a.provide_tests and selfcheck_dir(cfg))
        if a.preload_skills:   # skill texts appended to the workspace AGENTS.md: always in the prompt, not left to the model
            with open(os.path.join(ws, "AGENTS.md"), "a") as f:
                for k in ["hdl-check"] + (["gowin-primitives"] if cfg.get("gowin") else []):
                    body = open(os.path.expanduser("~/.config/opencode/skills/%s/SKILL.md" % k)).read()
                    body = body.split("\n---\n", 1)[1] if body.startswith("---") else body
                    f.write("\n\n<!-- skill %s, preloaded -->\n%s" % (k, body))
            g = ["git", "-C", ws, "-c", "user.name=bakeoff", "-c", "user.email=bakeoff@localhost"]
            subprocess.run(g + ["commit", "-q", "-a", "--amend", "--no-edit"], check=True)
        occ = json.load(open(os.path.join(AGENT, "opencode.sandbox.json")))
        occ["model"] = occ["small_model"] = "bc250/" + a.model   # title requests on the model under test: no router swap
        json.dump(occ, open(os.path.join(sk, "opencode.json"), "w"), indent=2)
        reqlog = os.path.join(rd, "events", tag + ".requests.jsonl")
        fargs = [sys.executable, os.path.join(AGENT, "fwd.py"), "unix2tcp", os.path.join(sk, "llm.sock"), INFER_HOST, INFER_PORT, reqlog]
        fargs.append(json.dumps(samp) if samp else "-")
        fargs.append(str(json.load(open(os.path.join(AGENT, "opencode.sandbox.json")))["provider"]["bc250"]["models"][a.model]["limit"]["output"]))
        if agents_md:
            # logged per request: is the project rule (its second line) and the global rules' heading in the prompt
            fargs += [[l.strip() for l in open(agents_md) if l.strip()][1], "# FPGA work rules"]
        fwd = subprocess.Popen(fargs)
        for _ in range(50):
            if os.path.exists(os.path.join(sk, "llm.sock")):
                break
            time.sleep(0.1)
        if fwd.poll() is not None or not os.path.exists(os.path.join(sk, "llm.sock")):
            raise SystemExit("LLM forwarder failed to start for %s (socket %s); stopping the run" % (tag, sk))
        msg = ("Read TASK.md in the current directory (/srv/work) and complete the task it describes. Work only inside "
               "/srv/work. When you are done, the file %s must contain your final answer." % out)
        cmd = [HEAVY, str(a.timeout), os.path.join(AGENT, "sandbox.sh"), ws, oc, sk,
               OPENCODE, "run", "--standalone", "--auto", "--format", "json", "--model", "bc250/" + a.model, msg]
        rec = {"run_id": "agent_" + a.run_id, "mode": "agent", "model": a.model, "task": cfg["id"], "type": cfg["type"],
               "language": cfg["language"], "category": run_api.category(cfg["id"]), "output_file": out,
               "sample": si, "samples_total": a.samples, "sampling_profile": a.sampling, "sampling_sent": samp,
               "project_agents": os.path.basename(a.project_agents) if a.project_agents else None, "skills": a.skills,
               "preload_skills": a.preload_skills, "provide_tests": tests_provided,
               "timeout_s": a.timeout, "started": datetime.datetime.now().isoformat(timespec="seconds")}
        evp = os.path.join(rd, "events", tag + ".jsonl")
        t0 = time.time()
        with open(evp, "w") as fo, open(os.path.join(rd, "events", tag + ".stderr"), "w") as fe:
            p = subprocess.run(cmd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL,
                               env=dict(os.environ, SANDBOX_NO_SKILLS="1" if a.skills == "off" else "0"))
        t1 = time.time()
        fwd.terminate()
        try:
            fwd.wait(5)
        except subprocess.TimeoutExpired:
            fwd.kill()
        rec["wall_s"] = round(t1 - t0, 1)
        rec["exit_code"] = p.returncode
        rec["timed_out"] = p.returncode in (124, 137)
        rec["thermal"] = run_api.thermal(t0, t1)
        rq = [json.loads(l) for l in open(reqlog)] if os.path.exists(reqlog) else []
        rec["llm_requests"] = len(rq)
        rec["sampling_received"] = sorted({json.dumps({k: v for k, v in (r.get("received") or {}).items() if k not in ("model", "stream")}, sort_keys=True) for r in rq})
        rec["sampling_sent_seen"] = sorted({json.dumps({k: v for k, v in (r.get("sent") or {}).items() if k not in ("model", "stream")}, sort_keys=True) for r in rq})
        rec["server_slot_sampling"] = run_api.slot_sampling(a.model)
        if agents_md:
            rec["project_agents"] = os.path.basename(a.project_agents)
            mk = [r.get("marker_in_body") or {} for r in rq]
            proj = [l.strip() for l in open(agents_md) if l.strip()][1]
            rec["project_rule_in_requests"] = "%d/%d" % (sum(1 for m in mk if m.get(proj)), len(rq))
            rec["global_rules_in_requests"] = "%d/%d" % (sum(1 for m in mk if m.get("# FPGA work rules")), len(rq))
        ev = parse_events(evp)
        rec.update({k: ev[k] for k in ("steps", "tool_calls", "shell_kinds", "tokens", "final_message", "events", "event_errors")})
        rec["last_step_unfinished"] = ev["steps"] > ev["finished_steps"]   # the last model turn ended without a step_finish (e.g. output limit)
        json.dump(ev["trace"], open(os.path.join(rd, "events", tag + ".trace.json"), "w"), indent=1)
        g = ["git", "-C", ws, "-c", "user.name=bakeoff", "-c", "user.email=bakeoff@localhost"]
        subprocess.run(g + ["add", "-A"], capture_output=True)
        subprocess.run(g + ["commit", "-q", "-m", "agent end"], capture_output=True)
        root = subprocess.run(g + ["rev-list", "--max-parents=0", "HEAD"], capture_output=True, text=True).stdout.split()[0]
        diff = subprocess.run(g + ["diff", root, "HEAD", "--stat"], capture_output=True, text=True).stdout
        subprocess.run(g + ["diff", root, "HEAD"], stdout=open(os.path.join(rd, "events", tag + ".diff"), "w"))
        rec["diff_stat"] = diff.strip().splitlines()[-1] if diff.strip() else "no changes"
        produced = os.path.join(ws, out)
        rec["output_changed"] = out in diff
        cand = os.path.join(rd, "cand", tag + (".v" if cfg["type"] == "testbench" else os.path.splitext(out)[1]))
        if os.path.exists(produced):
            shutil.copy(produced, cand)
        else:
            open(cand, "w").close()
        rec["candidate"] = cand
        g0 = time.time()
        try:
            gp = subprocess.run([os.path.join(cfg["dir"], "grade.sh"), cand], capture_output=True, text=True, timeout=a.grade_timeout)
            open(os.path.join(rd, "grade", tag + ".log"), "w").write(gp.stdout + gp.stderr)
            gr = json.loads(gp.stdout.strip().splitlines()[-1])
        except Exception as e:
            gr = {"pass": False, "stage": "harness", "detail": "grade.sh: %s: %s" % (type(e).__name__, e)}
        rec["grade_s"] = round(time.time() - g0, 1)
        rec["grade"] = gr
        rec["pass"] = bool(gr.get("pass"))
        rec["stage"], rec["detail"] = gr.get("stage"), gr.get("detail")
        if p.returncode not in (0, 124, 137) and ev["events"] == 0:
            rec["status"] = "ERROR"                     # the agent never ran (sandbox / lane / OpenCode start failure)
        else:
            rec["status"] = "PASS" if rec["pass"] else ("ERROR" if gr.get("stage") in ("harness", "syn_pending") else "FAIL")
        rec["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        with open(rj, "a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())
        print("%s %-26s s%d %-6s %-8s wall %6.0fs steps %s tools %s%s" % (rec["finished"], cfg["id"], si, rec["status"], rec["stage"],
              rec["wall_s"], rec["steps"], sum(rec["tool_calls"].values()), " TIMEOUT" if rec["timed_out"] else ""), flush=True)
        write_summary(rd, refs)
    write_summary(rd, refs)
    print("done", flush=True)


if __name__ == "__main__":
    main()
