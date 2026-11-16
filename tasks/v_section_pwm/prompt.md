Task: write the complete Verilog module `pwm_gen` described below.

Toolchain: Your file is checked with `verilator --lint-only -Wall` (Verilator 5.020), compiled with Icarus Verilog 12 (`iverilog -g2012`), simulated against a reference implementation with seeded random stimulus (every output compared continuously, cycle-exact, power-up values included), and synthesized with GowinSynthesis for a GW5AST-138 FPGA (Gowin's SystemVerilog-2017 mode). Use synthesizable Verilog-2005 / SystemVerilog constructs only.

Rules: keep the given header exactly (name, generics/parameters, ports, comments). Reply with the complete file: the given header followed by your implementation.

Also required (checked after the simulation passes): 0 Verilator -Wall warnings on your section's lines (a warning may be waived only around the lines that need it, with `/* verilator lint_off RULE */` before them and `/* verilator lint_on RULE */` after); the `SPDX-License-Identifier` and copyright lines at the top of the file kept (within its first 5 lines); a file that sets `` `default_nettype none `` ends with `` `default_nettype wire ``; plain ASCII only; no `initial` blocks (give a register its power-up value in its declaration: `reg [3:0] r = 4'd0;`); no dead code: an unused-bits sink lists real signals (`wire unused = &{1'b0, a[7:4]};`, never constants alone) and no empty `begin end` block (an empty case default is `default: ;`); no debug code: no `$display`, `$write`, `$monitor` or `$finish` in the module.

Specification:

pwm_gen is an 8-bit PWM generator. A prescaler makes a one-clock tick every PRE+1 clocks while en is high; a period
counter cnt counts those ticks from 0 to 255 and wraps; pwm is high while cnt is below the duty value latched at the
start of the period. Keep the header exactly as given; the grader may override PRE.

Registers (all power up as 0 through their declarations, all reset synchronously to 0 when reset is 1; reset wins over
everything else): pre_cnt (4 bits), tick, cnt (8 bits), duty_q (8 bits), and the outputs pwm and period_start.

At every rising edge of clk, when reset is 0:
- Prescaler: if en is 1 and pre_cnt == PRE, pre_cnt becomes 0 and tick becomes 1; if en is 1 and pre_cnt != PRE,
  pre_cnt becomes pre_cnt + 1 and tick becomes 0; if en is 0, pre_cnt holds and tick becomes 0.
- Period counter: if tick is 1, cnt becomes cnt + 1 (255 wraps to 0), and when cnt is 255 at that edge duty_q becomes
  duty. If tick is 0, cnt and duty_q hold.
- Output compare: pwm becomes 1 when cnt < duty_q (unsigned), else 0; period_start becomes 1 when tick is 1 and cnt is
  255, else 0.

Every right-hand side uses the values before the edge (nonblocking assignments), so pwm and period_start are one clock
behind cnt and tick.

Header:

```verilog
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 the steer-bc250 authors
// pwm_gen.v -- an 8-bit PWM generator in three labelled sections: a prescaler, the period counter with its duty
// latch, and the output compare. Every register is declared at module level, outside the sections.
module pwm_gen #(
    parameter [3:0] PRE = 4'd9
) (
    input  wire       clk,
    input  wire       reset,
    input  wire       en,
    input  wire [7:0] duty,
    output reg        pwm = 1'b0,
    output reg        period_start = 1'b0
);
```

The module is split into labelled sections (`// SECTION n: <name>` ... `// END SECTION n`); a section job is given the module with every other section written and fills one section.
