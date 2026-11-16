#!/usr/bin/env python3
"""sections.py -- labelled sections of a skeleton module, for section jobs.

A section is the text between a line `// SECTION <n>: <name>` and a line `// END SECTION <n>` (`--` comments in VHDL).
Without an END line (the open form) a section runs to the next
`// SECTION` line or to `endmodule`. The markers stay in every file; a job fills the body of one section; the grader
splices that body into the golden. Emptying a section keeps the comment lines at the top of its body (its spec).
"""
import re

EMPTY_BODY = "    // (write this section here)\n"


def _rx(n, cm):
    c = re.escape(cm)
    return re.compile(r"^[ \t]*%s[ \t]*SECTION[ \t]+%d[ \t]*:[^\n]*\n(.*?)^[ \t]*%s[ \t]*END[ \t]+SECTION[ \t]+%d\b[^\n]*$"
                      % (c, n, c, n), re.M | re.S)


def comment_of(language):
    return "--" if language == "vhdl" else "//"


def _open_rx(cm):
    c = re.escape(cm)
    return re.compile(r"^[ \t]*%s[ \t]*SECTION[ \t]+(\d+)[ \t]*:[^\n]*\n" % c, re.M)


def find(text, n, language="verilog"):
    """(start, end) of section n's body in text, or None when the markers are missing or repeated."""
    cm = comment_of(language)
    ms = list(_rx(n, cm).finditer(text))
    if len(ms) == 1:
        return ms[0].start(1), ms[0].end(1)
    if ms:
        return None
    heads = [m for m in _open_rx(cm).finditer(text) if int(m.group(1)) == n]
    if len(heads) != 1 or re.search(r"^[ \t]*%s[ \t]*END[ \t]+SECTION[ \t]+%d\b" % (re.escape(cm), n), text, re.M):
        return None
    start = heads[0].end()
    nxt = _open_rx(cm).search(text, start)
    end_kw = re.compile(r"^[ \t]*(endmodule|end[ \t]+architecture)\b", re.M | re.I).search(text, start)
    ends = [m.start() for m in (nxt, end_kw) if m]
    if not ends:
        return None
    end = min(ends)
    while end > start and text[end - 1] == "\n" and end - 1 > start and text[end - 2] == "\n":
        end -= 1                                   # leave the blank line before the next marker outside the body
    return start, end


def body(text, n, language="verilog"):
    span = find(text, n, language)
    return None if span is None else text[span[0]:span[1]]


def replace(text, n, new_body, language="verilog"):
    span = find(text, n, language)
    if span is None:
        raise ValueError("SECTION %d markers missing or repeated" % n)
    if new_body and not new_body.endswith("\n"):
        new_body += "\n"
    return text[:span[0]] + new_body + text[span[1]:]


def empty(text, n, language="verilog"):
    cm = comment_of(language)
    b = body(text, n, language)
    if b is None:
        raise ValueError("SECTION %d markers missing or repeated" % n)
    lead = []
    for l in b.splitlines(True):
        if l.strip().startswith(cm):
            lead.append(l)
        else:
            break
    return replace(text, n, "".join(lead) + EMPTY_BODY.replace("//", cm), language)


def numbers(text, language="verilog"):
    c = re.escape(comment_of(language))
    return sorted({int(m.group(1)) for m in re.finditer(r"^[ \t]*%s[ \t]*SECTION[ \t]+(\d+)[ \t]*:" % c, text, re.M)})


def confined(golden, mutant, n, language="verilog"):
    """True when mutant differs from golden only inside section n's body."""
    g, m = find(golden, n, language), find(mutant, n, language)
    if g is None or m is None:
        return False
    return golden[:g[0]] == mutant[:m[0]] and golden[g[1]:] == mutant[m[1]:] and golden[g[0]:g[1]] != mutant[m[0]:m[1]]


# ---- section-file mode: the worker edits only the section's body in a small file; the harness splices it back
PLACEHOLDER = "    %s <<< your section goes here, see %s >>>\n"


def section_file_name(n, ext=".v"):
    return "section_s%d%s" % (n, ext)


def marker_line(text, n, language="verilog"):
    """The `// SECTION n: ...` line of text (without its newline), or None."""
    c = re.escape(comment_of(language))
    m = re.search(r"^[ \t]*%s[ \t]*SECTION[ \t]+%d[ \t]*:[^\n]*$" % (c, n), text, re.M)
    return m.group(0) if m else None


def section_file_text(base, n, language="verilog"):
    """The small file a worker edits: the section's marker line, then its current body (spec comments included)."""
    mk, b = marker_line(base, n, language), body(base, n, language)
    if mk is None or b is None:
        raise ValueError("SECTION %d markers missing or repeated" % n)
    return mk + "\n" + b


def context_text(base, n, fname, language="verilog"):
    """The full file with section n's body replaced by one placeholder line naming the worker's file."""
    return replace(base, n, PLACEHOLDER % (comment_of(language), fname), language)


def section_file_body(text, n, language="verilog"):
    """(body, problem) of a worker's section file: a leading `SECTION n:` marker line and a trailing `END SECTION n`
    line are dropped; any other SECTION marker, a module/endmodule line, or an empty file is a problem."""
    cm = re.escape(comment_of(language))
    lines = text.splitlines(True)
    if lines and re.match(r"^[ \t]*%s[ \t]*SECTION[ \t]+%d[ \t]*:" % (cm, n), lines[0]):
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    if lines and re.match(r"^[ \t]*%s[ \t]*END[ \t]+SECTION[ \t]+%d\b" % (cm, n), lines[-1]):
        lines = lines[:-1]
    b = "".join(lines)
    if not b.strip():
        return None, "the section file is empty"
    m = re.search(r"^[ \t]*%s[ \t]*(END[ \t]+)?SECTION[ \t]+\d+\b[^\n]*" % cm, b, re.M)
    if m:
        return None, "the section file must hold only this section's body; it contains the marker `%s`" % m.group(0).strip()
    m = re.search(r"^[ \t]*(module|endmodule|entity|architecture)\b[^\n]*", b, re.M | re.I)
    if m:
        return None, ("the section file must hold only this section's body, not the whole module (found `%s`)"
                      % m.group(0).strip()[:60])
    return b, None


def splice_file(base, n, text, language="verilog"):
    """(full text, problem): base (the full file or its context form) with section n's body taken from the section
    file text. Returns (None, problem) when the section file cannot be used."""
    b, prob = section_file_body(text, n, language)
    if prob:
        return None, prob
    try:
        return replace(base, n, b, language), None
    except ValueError as e:
        return None, str(e)


def body_span_lines(full, n, language="verilog"):
    """(first, last) 1-based line numbers of section n's body in full, or None."""
    span = find(full, n, language)
    if span is None:
        return None
    first = full.count("\n", 0, span[0]) + 1
    last = first + full[span[0]:span[1]].count("\n") - 1
    return first, last


def first_line_is_marker(text, n, language="verilog"):
    first = (text.splitlines() or [""])[0]
    return bool(re.match(r"^[ \t]*%s[ \t]*SECTION[ \t]+%d[ \t]*:" % (re.escape(comment_of(language)), n), first))


def map_assembled_lines(output, asm, ctx, sec_text, n, asm_name, fname, ctx_name, language="verilog"):
    """Rewrite `<asm_name>:<line>` references in tool output (lint, compile, simulation) to the file the worker
    reads: lines of section n's body -> `<fname>:<line>` (the section file, marker line included), other lines ->
    `<ctx_name>:<line>` (the context file, where the section is one placeholder line)."""
    a, c = body_span_lines(asm, n, language), body_span_lines(ctx, n, language)
    if a is None or c is None:
        return output
    off = 2 if first_line_is_marker(sec_text, n, language) else 1
    rx = re.compile(r"(?:[^\s:'\"`(]*/)?%s:(\d+)" % re.escape(asm_name))

    def sub(m):
        ln = int(m.group(1))
        if ln < a[0]:
            return "%s:%d" % (ctx_name, ln)
        if ln <= a[1]:
            return "%s:%d" % (fname, ln - a[0] + off)
        return "%s:%d" % (ctx_name, ln - a[1] + c[1])
    return rx.sub(sub, output)


# ---- whole-file mode (the worker edits the full module): only section n of its file counts, as in the grader
def splice_whole(base, n, text, language="verilog"):
    """(full text, problem): base with section n's body taken from the worker's full file text."""
    b = body(text, n, language)
    if b is None:
        return None, "the markers of SECTION %d must each appear exactly once" % n
    return replace(base, n, b, language), None


def outside_changes(base, text, n, language="verilog"):
    """Where the worker's full file differs from base outside section n: ([other section numbers], other text
    changed). None when section n's markers are missing in either file."""
    sb, st = find(base, n, language), find(text, n, language)
    if sb is None or st is None:
        return None
    if base[:sb[0]] == text[:st[0]] and base[sb[1]:] == text[st[1]:]:
        return [], False
    secs = [m for m in numbers(base, language) if m != n and body(base, m, language) != body(text, m, language)]

    def rest(t):
        for m in numbers(base, language):
            try:
                t = replace(t, m, "", language)
            except ValueError:
                pass
        return t
    return secs, rest(base) != rest(text)


def whole_file_note(n, secs, other):
    what = ", ".join("SECTION %d" % m for m in secs) + (" and lines outside the sections" if other and secs else
                                                        "lines outside the sections" if other else "")
    return "only lines inside SECTION %d count; your changes to %s are ignored" % (n, what)


def map_whole_lines(output, asm, text, n, asm_name, fname, language="verilog"):
    """Rewrite `<asm_name>:<line>` references to the worker's full file `<fname>:<line>` (exact inside section n and
    wherever the file matches the base)."""
    a, w = body_span_lines(asm, n, language), body_span_lines(text, n, language)
    if a is None or w is None:
        return output
    rx = re.compile(r"(?:[^\s:'\"`(]*/)?%s:(\d+)" % re.escape(asm_name))

    def sub(m):
        ln = int(m.group(1))
        if ln < a[0]:
            return "%s:%d" % (fname, ln)
        if ln <= a[1]:
            return "%s:%d" % (fname, ln - a[0] + w[0])
        return "%s:%d" % (fname, ln - a[1] + w[1])
    return rx.sub(sub, output)
