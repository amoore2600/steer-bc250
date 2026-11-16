---
name: hdl-lessons
description: Rules learned from graded HDL runs (what made past modules fail and what fixed them). Load it before you write or fix any Verilog module from a spec, and follow it.
---

# hdl-lessons

Short rules from real graded runs. Each one cost a past worker a failed self-check or a wasted turn.

## Workflow

1. Read the whole spec (TASK.md) once. Find: every register (width, reset value), every output (registered or
   combinational), the exact edge conditions, and the worked example.
2. Write the WHOLE file with one Write call. Keep the given header exactly (name, ports, comments).
3. Run `bash run_selfcheck.sh` at once. Never end a turn without running it after your last change.
4. Read only the FIRST failure line: which output, which cycle, expected vs got. XOR expected and got to find the
   wrong bit(s). If only one or two bits are wrong, the fix is one spec row: check that bit in every row of the
   spec's table before changing anything else. Find the spec rule that sets those bits. Fix that rule only.
5. Write the whole file again (do not use Edit on the graded file: it fails when code repeats, and a half-applied
   edit leaves broken text). Run the self-check again. Repeat until `RESULT: PASS`.
6. Never give up while the self-check runs: a failing check with few wrong bits means you are close. A refusal is never a final answer: if a task
   looks long, write the module section by section from TASK.md, but always end with a Write and a self-check run.

7. Code you put in a reply is NOT saved. Only a Write call changes the file; after showing a fix, write it.
8. Lint warnings are clues: an unused parameter means its spec rule is missing from your code; a narrow value
   read as signed means the value is too narrow.
9. Keep any fix the spec states literally, even if the first failure does not move yet; then look at the next one.
10. Before chasing a late failure, check your registers against the spec's worked example cycle by cycle.

## Verilog-2005 rules (lint and synthesis)

- Declare every `reg` and `wire` at module level. No declarations inside `always`, `if` or `begin`/`end` blocks
  (a named block is the only exception). `reg x = expr;` inside a block is a one-time initialiser, not logic.
- Registers: one `always @(posedge clk)` with nonblocking `<=`. Combinational logic: `assign`, or one
  `always @*` with blocking `=` that assigns every output on every path (else you get a latch). Prefer `assign`:
  an `always @*` whose inputs never change after time 0 is never run in simulation, so its output stays X.
- Do not keep `*_next` copies of registers unless the spec needs them; compute from the OLD register values, as
  nonblocking assignments already do.
- Signed arithmetic: both operands must be signed, or the product is unsigned. Write
  `$signed(a) * $signed({1'b0, b})` for signed times unsigned. Use `>>>` (with a signed operand) for an
  arithmetic shift right. Size the product wide enough before you shift.
- A narrow value's top bit becomes the sign bit under `$signed`. Zero-extend into the full width first:
  `wire signed [31:0] xv = $signed({5'd0, xs3, 18'd0});`, never `$signed(narrow_wire)` when the value is unsigned.
- Multiply by a small constant with shifts and adds (`x*3` = `(x << 1) + x`), never `*`: each `*` can cost a DSP block.
- `(expr)[hi:lo]` is not Verilog: put the expression in a named wire, then slice the wire.
- Slicing a signed product keeps its sign only if you extend it: `{{2{prod[57]}}, prod[57:28]}`, not zero-extension
  or `>>`.
- Keep widths explicit: `9'd255`, `8'hFF`. A 9-bit counter wraps 0x1FF + 1 = 0 by itself.
- Reset: if the spec says synchronous reset, it goes inside the `posedge clk` block and wins over everything else.
- Power-up values: if the spec gives one for a register with no reset, use an initial value (`reg [7:0] x = 8'd0;`
  at module level).

## Reading specs

- Bit lists are written bit 7 first. In a list like `sw1 & {1, 1, l2, r2, ...}`, a `1` keeps that bit of the
  switch byte, a name ANDs that bit with the signal. Copy every row literally; check the bit you are failing in
  every row.
- When the spec says "using the OLD values", every right-hand side reads the registers before the edge.
- Combinational outputs change in the same cycle as their inputs; registered outputs change one clock later.
  A one-clock-late output means an extra register stage: remove it.
- A "strobe" or enable is high for one clock; act only on clocks where it is 1.
- When a field comes from a byte of a wider word (e.g. `sw1 = dsw[15:8]`), check you index the right byte; a
  wrong-byte error shows as one bit wrong in some rows only.

## What not to do

- Do not write side files (debug testbenches, copies) next to the graded file; the grader only reads the graded
  file.
- Do not change the testbench or the self-check script.
- Do not paste or ask for the reference implementation; the spec is complete.
- Do not add a register stage the spec does not show. Use the register's current value where the spec says so, not a
  `*_next` copy (that double-counts an increment).
