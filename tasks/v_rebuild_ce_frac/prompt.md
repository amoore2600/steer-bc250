Task: write the complete Verilog module `ce_frac` described below.

Toolchain: Your file is checked with `verilator --lint-only -Wall` (Verilator 5.020), compiled with Icarus Verilog 12 (`iverilog -g2012`), simulated against a reference implementation with seeded random stimulus (every output compared continuously, cycle-exact, power-up values included), and synthesized with GowinSynthesis for a GW5AST-138 FPGA (Gowin's SystemVerilog-2017 mode). Use synthesizable Verilog-2005 / SystemVerilog constructs only.

Rules: keep the given header exactly (name, generics/parameters, ports, comments). Reply with the complete file: the given header followed by your implementation.

Also required (checked after the simulation passes): 0 Verilator -Wall warnings (a warning may be waived only around the lines that need it, with `/* verilator lint_off RULE */` before them and `/* verilator lint_on RULE */` after); the `SPDX-License-Identifier` and copyright lines at the top of the file kept (within its first 5 lines); a file that sets `` `default_nettype none `` ends with `` `default_nettype wire ``; plain ASCII only; no dead code: an unused-bits sink lists real signals (`wire unused = &{1'b0, a[7:4]};`, never constants alone) and no empty `begin end` block (an empty case default is `default: ;`); no debug code: no `$display`, `$write`, `$monitor` or `$finish` in the module.

Specification:

ce_frac makes a clock enable that is high on exactly NUM out of every DEN cycles of clk, spread evenly by a
first-order accumulator. With the defaults (NUM = 12291, DEN = 171686) and a 50 MHz clk, the enable rate is
50 MHz x 12291 / 171686 = about 3.5795 MHz.

Parameters: W (register width), NUM and DEN (W-bit unsigned). Assume 0 < NUM < DEN < 2^W. Keep the header exactly as
given; the grader may override the parameters.

Registers: an accumulator acc (W bits) and the output ce. Both power up as 0 (use initial values).

At every rising edge of clk:
- If reset is 1: acc becomes 0 and ce becomes 0 (synchronous reset, active high).
- Otherwise let sum = acc + NUM, computed with W+1 bits so it cannot overflow.
  - If sum >= DEN: acc becomes sum - DEN and ce becomes 1.
  - Else: acc becomes sum and ce becomes 0.

So ce is a registered output: it is 1 for the one clock after each edge where the accumulator wrapped. acc always
stays below DEN. Example with NUM = 3, DEN = 8 from acc = 0: acc goes 3, 6, 1 (ce = 1), 4, 7, 2 (ce = 1), 5, 0 (ce = 1),
3, ... so ce is 1 on 3 of every 8 clocks.

Header:

```verilog
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 the steer-bc250 authors
// ce_frac.v -- fractional clock enable: ce is high for NUM of every DEN clocks of clk, spaced as evenly as a
// first-order accumulator spaces them (the defaults give about 3.5795 MHz from a 50 MHz clock).
module ce_frac #(
    parameter integer W = 18,
    parameter [W-1:0] NUM = 18'd12291,
    parameter [W-1:0] DEN = 18'd171686
) (
    input  wire clk,
    input  wire reset,
    output reg  ce
);
```
