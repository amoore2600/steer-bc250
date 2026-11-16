#!/usr/bin/env python3
"""hygiene.py -- the checks a port's own gates apply to a module beyond simulation, shared by the grader (grade.py,
kat_sections.py), the pre-gate (steer_agent.py) and the worker's self-check (hygiene_check.py, generated from this
file by run_agent.install_hygiene):

  lint_wall   `verilator --lint-only -Wall` gives 0 warnings in the candidate (in a section job: 0 warnings on lines
              inside the job's section). A warning waived in the file with a `verilator lint_off <RULE>` ...
              `verilator lint_on <RULE>` comment pair is not reported by Verilator and so does not count; a lint_off
              that names no rule, or has no matching lint_on, is itself a problem. DECLFILENAME never counts (the
              graded copy is not named after its module). task.json "lint_wall": false turns this check off.
  pinned      task.json "pinned_names": [{"name", "width"}]: each signal exists at module scope with that width
              (benches read them by hierarchical name). Probed with iverilog: $bits(dut.<name>) == width, the module
              built with its default parameters (or task.json "pinned_params", not the equivalence test's).
  hygiene     missing_header  an SPDX-License-Identifier line / a copyright line in the first 5 lines, each required
                              only when the task's golden or start file has one;
              nettype_leak    a file that sets `default_nettype none must end with `default_nettype wire;
              non_ascii       no character outside ASCII;
              initial_blocks  no `initial` block (Gowin may drop them on silicon; use declaration initialisers) unless
                              task.json "allow_initial": true.
              dead_code       no unused-bits sink (`wire unused... = &{...};`, also |, ^ and assign forms) whose
                              concatenation holds only constants (no signal or parameter name), and no empty
                              `begin end` block (an empty `if (...) begin end`, an empty case item); an empty case
                              default is written `default: ;`. task.json "allow_dead_code": true turns it off.
              debug_left      no `$display`, `$write`, `$monitor` (and their b/h/o forms) or `$finish` in the code
                              (comments and strings ignored) unless task.json "allow_display": true; and, when task.json
                              "lint_waivers" is a list, no `verilator lint_off RULE` for a RULE outside that list (absent
                              list: any named, paired lint_off is allowed, see lint_wall).

Every function below the CORE marker uses only the standard library: run_agent copies them into the workspace."""
import glob, json, os, re, subprocess

HYGIENE_KEYS = ("missing_header", "nettype_leak", "non_ascii", "initial_blocks", "dead_code", "debug_left")
# CORE (copied into the worker's hygiene_check.py)
WALL_SKIP = ("DECLFILENAME",)
HEAD_LINES = 5


def wall_flags():
    return ["--lint-only", "-Wall", "-Wno-fatal", "-Wno-DECLFILENAME"]


def strip_comments_keep_lines(text):
    """Comments blanked, line numbers kept (a block comment keeps its newlines)."""
    t = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    t = re.sub(r"//[^\n]*", "", t)
    return re.sub(r'"(?:\\.|[^"\\\n])*"', '""', t)


def in_span(line, span):
    return span is None or span[0] <= line <= span[1]


def wall_warnings(lines, fname, span=None):
    """[(line, warning text)] of the Verilator -Wall warnings located in file fname (matched by basename), inside
    span (first, last line) when given. The first line of each warning only."""
    rx = re.compile(r"^%%Warning-([A-Z0-9_]+):\s*(?:\S*/)?%s:(\d+):" % re.escape(os.path.basename(fname)))
    out = []
    for l in lines:
        m = rx.match(l)
        if m and m.group(1) not in WALL_SKIP and in_span(int(m.group(2)), span):
            out.append((int(m.group(2)), l.rstrip()))
    return out


WAIVER = re.compile(r"(?://|/\*)\s*verilator\s+lint_(off|on)\b[ \t]*([A-Za-z0-9_]*)")


def waiver_problems(text, span=None):
    """[(line, message)]: a `verilator lint_off` that names no rule, or whose rule has no later `lint_on` of the same
    rule. Only lint_off lines inside span count."""
    opened, probs = {}, []
    for i, line in enumerate(text.splitlines(), 1):
        for m in WAIVER.finditer(line):
            kind, rule = m.group(1), m.group(2)
            if kind == "off":
                if not rule:
                    probs.append((i, "line %d: `verilator lint_off` must name the one warning it waives "
                                     "(e.g. `/* verilator lint_off WIDTHTRUNC */`) and be closed by a lint_on" % i))
                else:
                    opened.setdefault(rule, []).append(i)
            elif rule and opened.get(rule):
                opened[rule].pop()
            elif not rule:
                opened.clear()
    for rule, ls in opened.items():
        for i in ls:
            probs.append((i, "line %d: `verilator lint_off %s` has no matching `verilator lint_on %s` after the waived "
                             "lines" % (i, rule, rule)))
    return sorted(p for p in probs if in_span(p[0], span))


def header_needs(texts):
    """(SPDX line required, copyright line required): each when any reference text has it in its first lines."""
    spdx = cpy = False
    for t in texts:
        head = (t or "").splitlines()[:HEAD_LINES]
        spdx |= any("SPDX-License-Identifier" in l for l in head)
        cpy |= any(re.search(r"(?i)\bcopyright\b", l) for l in head)
    return spdx, cpy


SINK = re.compile(r"(?:\b(?:wire|logic)\b(?:\s+signed\b)?(?:\s*\[[^\]]*\])?|\bassign\b)\s*(\w*[Uu][Nn][Uu][Ss][Ee][Dd]\w*)"
                  r"\s*=\s*~?[&|^]\s*\{")
LITERAL = re.compile(r"\b\d+\s*'\s*[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ_?]+|'\s*[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ_?]+|'[01xXzZ]"
                     r"|\b\d[\d_]*(?:\.\d+)?\b")
EMPTY_BLOCK = re.compile(r"\bbegin\b(?:\s*:\s*\w+)?\s*\bend\b")


def dead_code(code, span=None):
    """[(line, message)] in comment-stripped code: an unused-bits sink whose concatenation names no signal or
    parameter (only literals), and empty `begin end` blocks. Only lines inside span count."""
    probs = []
    for m in SINK.finditer(code):
        k, depth = m.end() - 1, 0
        while k < len(code):
            depth += {"{": 1, "}": -1}.get(code[k], 0)
            if depth == 0:
                break
            k += 1
        body = code[m.end():k]
        line = code.count("\n", 0, m.start()) + 1
        if in_span(line, span) and not re.search(r"[A-Za-z_]\w*", LITERAL.sub(" ", re.sub(r"\$\w+", " ", body))):
            probs.append((line, "line %d: the unused-bits sink `%s` holds only constants, so it sinks nothing: list the "
                                "unused signals or bit-slices in it, or remove it" % (line, m.group(1))))
            break
    for m in EMPTY_BLOCK.finditer(code):
        line = code.count("\n", 0, m.start()) + 1
        if in_span(line, span):
            probs.append((line, "line %d: empty `begin end` block (dead code): remove it with the `if`/`else` that holds "
                                "it; an empty case default is written `default: ;`" % line))
            break
    return probs


DEBUG_TASK = re.compile(r"\$(?:display|write|monitor)[bho]?\b|\$finish\b")


def debug_left(code, text, allow_display=False, lint_waivers=None, span=None):
    """[(line, message)]: the first simulation-output or $finish call in comment-stripped code (unless allow_display),
    and the first `verilator lint_off RULE` in text (waivers live in comments) whose RULE is not in lint_waivers (only
    when lint_waivers is a list). Only lines inside span count."""
    probs = []
    if not allow_display:
        for i, l in enumerate(code.splitlines(), 1):
            m = DEBUG_TASK.search(l)
            if m and in_span(i, span):
                probs.append((i, "line %d: `%s` is debug code (simulation output or $finish) left in the module: remove "
                                 "it" % (i, m.group(0))))
                break
    if lint_waivers is not None:
        allowed = set(lint_waivers)
        for i, l in enumerate(text.splitlines(), 1):
            bad = [m.group(2) for m in WAIVER.finditer(l) if m.group(1) == "off" and m.group(2) and m.group(2) not in allowed]
            if bad and in_span(i, span):
                probs.append((i, "line %d: `verilator lint_off %s` waives a warning this task does not allow to be "
                                 "waived (allowed: %s): fix the code instead" % (
                                     i, bad[0], ", ".join(sorted(allowed)) or "none")))
                break
    return probs


def hygiene_problems(text, need_spdx=False, need_copyright=False, allow_initial=False, span=None, allow_dead_code=False,
                     allow_display=False, lint_waivers=None):
    """[(key, line, message)] of the cheap file checks (keys: HYGIENE_KEYS). In a section job span limits the
    non-ASCII, initial-block, dead-code and debug checks to the section's lines; the header and nettype checks see the
    whole file."""
    probs = []
    lines = text.splitlines()
    head = lines[:HEAD_LINES]
    if need_spdx and not any("SPDX-License-Identifier:" in l for l in head):
        probs.append(("missing_header", 1, "the file must start with its licence line `// SPDX-License-Identifier: ...` "
                                            "(within the first %d lines), as the given file does" % HEAD_LINES))
    if need_copyright and not any(re.search(r"(?i)\bcopyright\b", l) for l in head):
        probs.append(("missing_header", 1, "the file must keep its copyright line `// Copyright (c) ...` within the "
                                            "first %d lines, as the given file does" % HEAD_LINES))
    code = strip_comments_keep_lines(text)
    nets = [(code.count("\n", 0, m.start()) + 1, m.group(1)) for m in
            re.finditer(r"`default_nettype\s+(\w+)", code)]
    if any(v == "none" for _, v in nets):
        ends = [m.start() for m in re.finditer(r"\bendmodule\b", code)]
        last_end = code.count("\n", 0, ends[-1]) + 1 if ends else 0
        if not (nets[-1][1] == "wire" and nets[-1][0] >= last_end):
            probs.append(("nettype_leak", nets[-1][0], "the file sets `default_nettype none, so it must end with "
                                                       "`default_nettype wire after endmodule (otherwise the setting "
                                                       "leaks into every file compiled after it)"))
    for i, l in enumerate(lines, 1):
        if in_span(i, span):
            bad = [c for c in l if ord(c) > 127]
            if bad:
                probs.append(("non_ascii", i, "line %d has a non-ASCII character (U+%04X); use plain ASCII only"
                              % (i, ord(bad[0]))))
                break
    if not allow_initial:
        for i, l in enumerate(code.splitlines(), 1):
            if in_span(i, span) and re.search(r"\binitial\b", l):
                probs.append(("initial_blocks", i, "line %d: `initial` block; Gowin synthesis may drop it on silicon: give "
                              "the register its power-up value in its declaration (`reg [3:0] r = 4'd0;`) instead" % i))
                break
    if not allow_dead_code:
        probs += [("dead_code", i, msg) for i, msg in dead_code(code, span)]
    probs += [("debug_left", i, msg) for i, msg in debug_left(code, text, allow_display, lint_waivers, span)]
    return probs


def pinned_message(p):
    w = int(p["width"])
    return "internal signal %s [%d:0] required by the spec is missing or has the wrong width" % (p["name"], w - 1)


def pinned_probe_text(top, pinned, params=None):
    """A probe module instantiating <top> as dut and printing PINNED_WIDTH <name> <bits> for each pinned name whose
    $bits differs from its width ($bits of a missing name is 0 in Icarus Verilog; dut.<name> names module scope only)."""
    pm = " #(" + ", ".join(".%s(%s)" % (k, v) for k, v in params.items()) + ")" if params else ""
    L = ["`timescale 1ns/1ps", "`default_nettype wire", "module steer_pin_probe;", "    %s%s dut ();" % (top, pm),
         "    initial begin"]
    for p in pinned:
        L.append('        if ($bits(dut.%s) != %d) $display("PINNED_WIDTH %s %%0d", $bits(dut.%s));'
                 % (p["name"], int(p["width"]), p["name"], p["name"]))
    L += ['        $display("PINNED_DONE");', "        $finish;", "    end", "endmodule", ""]
    return "\n".join(L)


def pinned_parse(out, pinned):
    """(problems, error): pinned names whose width differs (or that are missing), or an error when the probe did not
    run to its end."""
    if "PINNED_DONE" not in out:
        bad = [p for p in pinned if re.search(r"\bdut\.%s\b" % re.escape(p["name"]), out)]
        if bad:
            return [pinned_message(p) for p in bad], None
        return [], "the pinned-name probe did not run: " + " | ".join(out.strip().splitlines()[:2])[:200]
    got = dict((m.group(1), int(m.group(2))) for m in re.finditer(r"^PINNED_WIDTH (\S+) (\d+)", out, re.M))
    return [pinned_message(p) for p in pinned if p["name"] in got], None
# END CORE


def task_needs(cfg, td):
    """(need_spdx, need_copyright) from the task's golden and start files."""
    texts = []
    for f in [os.path.join(td, "golden", g) for g in cfg.get("golden_files", [])[:1]] + \
            sorted(glob.glob(os.path.join(td, "start", "*"))):
        try:
            texts.append(open(f, errors="replace").read())
        except OSError:
            pass
    if not texts and os.path.exists(os.path.join(td, "skeleton.v")):
        texts.append(open(os.path.join(td, "skeleton.v"), errors="replace").read())
    return header_needs(texts)


def applies(cfg):
    """The checks apply to Verilog / SystemVerilog modules (not testbench tasks, not VHDL)."""
    return cfg.get("type") != "testbench" and cfg.get("language") in ("verilog", "systemverilog")


def lint_wall_on(cfg):
    return applies(cfg) and bool(cfg.get("lint_wall", True))


# task.json keys that decide the checks below; a config taken from a task.json carries exactly these
CHECK_KEYS = ("lint_wall", "allow_initial", "allow_dead_code", "allow_display", "lint_waivers", "pinned_names",
              "pinned_params")


def checks_cfg(cfg, tj):
    """cfg with its check keys (CHECK_KEYS) replaced by those of the task.json dict tj (a key tj lacks is removed)."""
    c = dict(cfg)
    for k in CHECK_KEYS:
        if k in tj:
            c[k] = tj[k]
        else:
            c.pop(k, None)
    return c


def settings(cfg, td):
    """The check settings a config implies (the same keys as the worker's hygiene_check.py CFG)."""
    need_spdx, need_cpy = task_needs(cfg, td)
    return {"lint_wall": lint_wall_on(cfg), "need_spdx": need_spdx, "need_copyright": need_cpy,
            "allow_initial": bool(cfg.get("allow_initial")), "allow_dead_code": bool(cfg.get("allow_dead_code")),
            "allow_display": bool(cfg.get("allow_display")), "lint_waivers": lint_waivers(cfg),
            "pinned": cfg.get("pinned_names") or [], "params": cfg.get("pinned_params") or {}}


def lint_waivers(cfg):
    """task.json "lint_waivers" as a sorted list, or None when the task does not set it (then any named, paired
    lint_off is allowed)."""
    w = cfg.get("lint_waivers")
    return None if w is None else sorted(str(x) for x in w)


def run_pinned(cfg, W, files, run, top=None):
    """The pinned-name probe compiled with iverilog against files (the candidate and what it needs). Returns
    (problems, error)."""
    pinned = cfg.get("pinned_names") or []
    if not pinned:
        return [], None
    pf = os.path.join(W, "steer_pin_probe.v")
    open(pf, "w").write(pinned_probe_text(top or cfg["top"], pinned, cfg.get("pinned_params")))
    vvp = os.path.join(W, "pin_probe.vvp")
    rc, out, _ = run(["iverilog", "-g2012", "-o", vvp, "-s", "steer_pin_probe", pf] + list(files), 300)
    if rc == 0:
        rc, out, _ = run(["vvp", "-n", vvp], 120, cwd=W)
    return pinned_parse(out, pinned)


def grade_checks(cfg, td, W, cand, lint_lines, res, run, span=None, sim_files=(), lint_wall=None):
    """The checks after a passing simulation / known-answer run. Fills res["lint_wall"], res["hygiene"],
    res["pinned"]; on a problem fails res (stage lint_wall, hygiene or pinned) and returns False."""
    if not applies(cfg):
        return True
    text = open(cand, errors="replace").read()
    on = lint_wall_on(cfg) if lint_wall is None else lint_wall
    ww = wall_warnings(lint_lines, cand, span) if on else []
    wp = waiver_problems(text, span) if on else []
    res["lint_wall"] = {"checked": on, "warnings": len(ww), "first": ww[0][1].replace(W + "/", "")[:240] if ww else None,
                        "waiver_problems": len(wp)}
    need_spdx, need_cpy = task_needs(cfg, td)
    hp = hygiene_problems(text, need_spdx, need_cpy, bool(cfg.get("allow_initial")), span, bool(cfg.get("allow_dead_code")),
                          bool(cfg.get("allow_display")), lint_waivers(cfg))
    res["hygiene"] = {k: sum(1 for p in hp if p[0] == k) for k in HYGIENE_KEYS}
    pp, perr = run_pinned(cfg, W, list(sim_files) + [cand], run)
    res["pinned"] = pp
    if ww:
        res.fail("lint_wall", "verilator -Wall: %d warning(s)%s, the first: %s" % (
            len(ww), " in the section" if span else "", ww[0][1].replace(W + "/", "")))
        return False
    if wp:
        res.fail("lint_wall", "lint waiver: " + wp[0][1])
        return False
    if hp:
        res.fail("hygiene", "; ".join("%s: %s" % (k, m) for k, _, m in hp[:3]))
        return False
    if perr:
        res.fail("harness", perr)
        return False
    if pp:
        res.fail("pinned", "; ".join(pp[:3]))
        return False
    return True


def golden_resources(td):
    """The golden's synthesis resources ({dsp, bsram, prims}) recorded in td/validation.json by validate.py (its
    golden, or the first golden section grade: each synthesizes the same full module), or None."""
    try:
        v = json.load(open(os.path.join(td, "validation.json")))
    except (OSError, ValueError):
        return None
    for g in [v.get("golden")] + list(v.get("golden_sections") or []):
        r = ((g or {}).get("syn") or {}).get("resources")
        if r:
            return r
    return None


def dsp_limit(cfg, td):
    """(DSP blocks a candidate may use, "max_mult" | "golden") for a task with Gowin synthesis, or (None, None) when no
    limit applies: no synthesis, "allow_more_dsp": true, or neither task.json "max_mult" nor a recorded golden count."""
    if not cfg.get("gowin_syn") or cfg.get("allow_more_dsp"):
        return None, None
    if cfg.get("max_mult") is not None:
        return int(cfg["max_mult"]), "max_mult"
    g = golden_resources(td)
    return (g["dsp"], "golden") if g and g.get("dsp") is not None else (None, None)


def rules_text(cfg, td):
    """The rules of this file as one sentence for a task statement (without the final full stop)."""
    need_spdx, need_cpy = task_needs(cfg, td)
    r = []
    if lint_wall_on(cfg):
        r.append("0 Verilator -Wall warnings%s (a warning may be waived only around the lines that need it, with "
                 "`/* verilator lint_off RULE */` before them and `/* verilator lint_on RULE */` after)"
                 % (" on your section's lines" if cfg.get("section") is not None or cfg.get("sections") else ""))
    if need_spdx or need_cpy:
        r.append("the %s line%s at the top of the file kept (within its first %d lines)" % (
            " and ".join(x for x, y in (("`SPDX-License-Identifier`", need_spdx), ("copyright", need_cpy)) if y),
            "s" if need_spdx and need_cpy else "", HEAD_LINES))
    r.append("a file that sets `` `default_nettype none `` ends with `` `default_nettype wire ``")
    r.append("plain ASCII only")
    if not cfg.get("allow_initial"):
        r.append("no `initial` blocks (give a register its power-up value in its declaration: `reg [3:0] r = 4'd0;`)")
    if not cfg.get("allow_dead_code"):
        r.append("no dead code: an unused-bits sink lists real signals (`wire unused = &{1'b0, a[7:4]};`, never constants "
                 "alone) and no empty `begin end` block (an empty case default is `default: ;`)")
    if not cfg.get("allow_display"):
        r.append("no debug code: no `$display`, `$write`, `$monitor` or `$finish` in the module")
    lw = lint_waivers(cfg)
    if lw is not None:
        r.append("lint waivers (`verilator lint_off`) only for %s" % (", ".join(lw) if lw else "no rule: none at all"))
    for p in cfg.get("pinned_names") or []:
        r.append("the internal signal `%s` declared at module level, exactly [%d:0] wide (testbenches read it by name)"
                 % (p["name"], int(p["width"]) - 1))
    lim, src = dsp_limit(cfg, td)
    if lim is not None:
        r.append("at most %d DSP multiplier block%s after Gowin synthesis (%s): share a multiplier between uses as the "
                 "spec's sequencing describes, never one multiplier per use" % (
                     lim, "" if lim == 1 else "s", "the reference design's count" if src == "golden" else "task limit"))
    return "; ".join(r)


def waiver_limit(cfg):
    """The task's lint_waivers limit as a sentence tail ('' when the task sets none)."""
    lw = lint_waivers(cfg)
    if lw is None:
        return ""
    return " This task allows waivers only for: %s." % ", ".join(lw) if lw else " This task allows no waivers."


def repository_rules(cfg, td, after="the known-answer checker passes"):
    """The "Repository rules (checked)" paragraph of a task statement, generated from the task's check settings
    (lint_wall, header needs, allow_initial, pinned_names). Section tasks: the rules apply to the section's lines,
    and the header and footer belong to the skeleton, so they are left out."""
    if not applies(cfg):
        return ""
    sec = cfg.get("section") is not None or bool(cfg.get("sections"))
    where = " on your section's lines" if sec else ""
    need_spdx, need_cpy = task_needs(cfg, td)
    L = ["Repository rules (checked)",
         "After %s, the file is checked again by the repository's own gates, so a design that is only functionally "
         "right is rejected." % after]
    if lint_wall_on(cfg):
        L += ["- Lint: `verilator --lint-only -Wall` must report 0 warnings%s (any warning fails, not only errors):" % where,
              "  - WIDTHEXPAND / WIDTHTRUNC: size every operand explicitly: sized literals (8'd0, never a bare 0 or 1 in "
              "an expression that has a width); zero-extend a narrow operand yourself ({5'd0, bit}); select or "
              "sign-extend the bits you need from a wider value instead of assigning it whole.",
              "  - UNUSEDSIGNAL / UNUSEDPARAM: every declared signal bit, input and localparam must be used. Bits you "
              "really do not use (high bits of a loop counter or an integer, an operand slice, an unused input) go "
              "into one sink wire, `wire unused = &{1'b0, <those signals or bit-slices>};`, listing exactly the unused "
              "bits; or are not declared at all.",
              "  - CASEINCOMPLETE: every `case` statement has a `default:` branch, also when the listed values cover "
              "every value you expect.",
              "  - BLKSEQ: non-blocking assignments (<=) in clocked always blocks; blocking (=) only in `always @*`.",
              "  - A warning may be waived only around the lines that need it: `/* verilator lint_off RULE */` before "
              "them and `/* verilator lint_on RULE */` after, each naming one rule." + waiver_limit(cfg)]
    if not sec:
        if need_spdx or need_cpy:
            L.append("- Header: keep the %s line%s of the start file within the first %d lines." % (
                " and ".join(x for x, y in (("`SPDX-License-Identifier`", need_spdx), ("copyright", need_cpy)) if y),
                "s" if need_spdx and need_cpy else "", HEAD_LINES))
        L.append("- Footer: a file that sets `` `default_nettype none `` ends with `` `default_nettype wire `` after "
                 "endmodule.")
    L.append("- ASCII only: no non-ASCII character anywhere%s, comments included." % (" in your section" if sec else " in the file"))
    if not cfg.get("allow_display"):
        L.append("- No debug code: no `$display`, `$write`, `$monitor` or `$finish`%s." % (" in your section" if sec else
                                                                                            " in the module"))
    if not cfg.get("allow_initial"):
        L.append("- Power-up values: no `initial` blocks; give a register its power-up value in its declaration "
                 "(`reg [3:0] r = 4'd0;`).")
    pins = cfg.get("pinned_names") or []
    if pins:
        L.append("- Internal signal names (testbenches read them by hierarchical name): declare " + ", ".join(
            "`%s` [%d:0]" % (p["name"], int(p["width"]) - 1) for p in pins) + " at module level with exactly these "
            "names and widths.")
    return "\n".join(L)


def core_source():
    """The CORE functions as source text, for the worker's hygiene_check.py."""
    src = open(os.path.abspath(__file__), errors="replace").read()
    return src[src.index("# CORE"):src.index("# END CORE")]
