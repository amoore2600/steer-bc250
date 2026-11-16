Task: write the complete Verilog module `debounce` described below.

Toolchain: Your file is checked with `verilator --lint-only -Wall` (Verilator 5.020), compiled with Icarus Verilog 12 (`iverilog -g2012`), checked by a known-answer checker, and synthesized with GowinSynthesis for a GW5AST-138 FPGA (Gowin's SystemVerilog-2017 mode). Use synthesizable Verilog-2005 / SystemVerilog constructs only.

It is graded by a known-answer checker, the self-checking testbench `tb_debounce.v` (module `tb_debounce`), compiled with Icarus Verilog (`iverilog -g2012`) together with your file; the module passes when the checker prints its PASS verdict line.

Rules: keep the module name and port list exactly as in the start file below. Reply with the complete file.

Repository rules (checked)
After the known-answer checker passes, the file is checked again by the repository's own gates, so a design that is only functionally right is rejected.
- Lint: `verilator --lint-only -Wall` must report 0 warnings (any warning fails, not only errors):
  - WIDTHEXPAND / WIDTHTRUNC: size every operand explicitly: sized literals (8'd0, never a bare 0 or 1 in an expression that has a width); zero-extend a narrow operand yourself ({5'd0, bit}); select or sign-extend the bits you need from a wider value instead of assigning it whole.
  - UNUSEDSIGNAL / UNUSEDPARAM: every declared signal bit, input and localparam must be used. Bits you really do not use (high bits of a loop counter or an integer, an operand slice, an unused input) go into one sink wire, `wire unused = &{1'b0, <those signals or bit-slices>};`, listing exactly the unused bits; or are not declared at all.
  - CASEINCOMPLETE: every `case` statement has a `default:` branch, also when the listed values cover every value you expect.
  - BLKSEQ: non-blocking assignments (<=) in clocked always blocks; blocking (=) only in `always @*`.
  - A warning may be waived only around the lines that need it: `/* verilator lint_off RULE */` before them and `/* verilator lint_on RULE */` after, each naming one rule.
- Header: keep the `SPDX-License-Identifier` and copyright lines of the start file within the first 5 lines.
- Footer: a file that sets `` `default_nettype none `` ends with `` `default_nettype wire `` after endmodule.
- ASCII only: no non-ASCII character anywhere in the file, comments included.
- No debug code: no `$display`, `$write`, `$monitor` or `$finish` in the module.
- Power-up values: no `initial` blocks; give a register its power-up value in its declaration (`reg [3:0] r = 4'd0;`).

Specification:

debounce cleans up a bouncing switch input.

Ports: clk (clock), din (the raw switch, asynchronous to clk), dout (the debounced level), rise (a one-clock pulse when
dout goes from 0 to 1). Every register powers up as 0 through its declaration; there is no reset input.

At every rising edge of clk (every right-hand side reads the values before the edge):
- A two-flip-flop synchroniser: sync[0] takes din, sync[1] takes sync[0].
- A 3-bit counter cnt counts the clocks in a row in which sync[1] differed from dout:
  - if sync[1] equals dout: cnt becomes 0;
  - else if cnt is 3 (the 4th such clock): cnt becomes 0 and dout becomes sync[1];
  - else: cnt becomes cnt + 1.
- rise becomes 1 exactly when dout changes from 0 to 1 at this edge, else 0.

So a level on din that lasts fewer than 4 clocks never reaches dout, and a level that lasts long enough reaches dout 6
clocks after din changed (2 synchroniser clocks, then 4 clocks of counting).

Start file:

```verilog
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 the steer-bc250 authors
// debounce.v -- switch debouncer: din is synchronised by two flip-flops; dout takes the synchronised value once it has
// differed from dout for 4 clocks in a row, and rise is high for the one clock in which dout goes from 0 to 1.
`default_nettype none
module debounce (
    input  wire clk,
    input  wire din,
    output reg  dout = 1'b0,
    output reg  rise = 1'b0
);
endmodule
`default_nettype wire
```
