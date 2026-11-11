---
name: hdl-check
description: One-command lint, compile, testbench simulation and synthesis check for Verilog, SystemVerilog and VHDL. Load it whenever you write or change HDL, and run it before you say the work is done.
---

# hdl-check

Run this after every change to an HDL file, and again before finishing:

```
bash ~/.config/opencode/skills/hdl-check/hdl-check.sh [--top NAME] [--tb TB_FILE] DESIGN_FILE [OTHER_FILES...]
```

- Put the file you are working on first. Add every file it depends on (VHDL packages, submodules); order does not matter.
- `--tb TB_FILE` also compiles and runs that testbench.
- `--top NAME` picks the top module or entity when there is more than one.
- `--stop-time 2ms` sets the VHDL simulation time limit (default 1ms).

It prints only problems and ends with one line, `RESULT: PASS` or `RESULT: FAIL (...)`.

What it runs:

| Language | Lint / analysis | Compile | Simulation (with --tb) | Synthesis check |
|---|---|---|---|---|
| Verilog / SV | verilator --lint-only -Wall | iverilog -g2012 | vvp, 120 s limit | yosys: latches, multiple drivers, loops, undriven nets |
| VHDL (2008) | ghdl -a, retried until dependencies resolve | (same) | ghdl -r | ghdl --synth |

Gowin GW5A primitives (PLL, OSER10, ELVDS_OBUF, DPB, ...) are found automatically: simulation uses `./prim_sim.v` or `./prim_sim.vhd` when present, else `~/gowin/IDE/simlib/gw5a/`; lint and synthesis use port stubs.

## Reading the result

- `RESULT: FAIL` means fix it before you finish. Treat any `%Error` and any inferred latch as a bug.
- Verilator warnings are worth reading (width mismatches especially) but do not fail the check.
- A passing check only proves the code is legal and the testbench you wrote passed. It does not prove the behaviour is right. Check behaviour with a testbench.

## Writing a quick self-checking testbench

Keep it short and make it decide pass or fail itself:

Verilog:
```verilog
`timescale 1ns/1ps
module tb;
  reg clk = 0; always #5 clk = ~clk;
  integer err = 0;
  // instantiate the design, drive inputs on negedge or after #1, check outputs after the clock edge
  initial begin
    // ... stimulus ...
    if (out !== EXPECTED) begin err = err + 1; $display("FAIL: out=%h expected %h", out, EXPECTED); end
    if (err == 0) $display("PASS");
    $finish;
  end
endmodule
```

VHDL: report `"FAIL: ..."` on a mismatch, `"PASS"` at the end, then `std.env.finish;`.

Rules:
- Print `FAIL` (or `ERROR`) on every mismatch and `PASS` once at the end; the tool looks for those words.
- Always end with `$finish` / `std.env.finish`, or the run stops at the time limit.
- Use `!==` in Verilog so X and Z values count as mismatches.
- Test reset, the normal case and at least one edge case (wrap-around, full/empty, maximum value).
