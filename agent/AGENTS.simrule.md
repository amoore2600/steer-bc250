# Verification rule
Lint is not enough: before you finish, prove the behaviour in simulation.
- For a module you wrote or fixed: write a small self-checking testbench (tb_<name>.v or tb_<name>.vhd) that applies reset and drives the inputs through the behaviour described in TASK.md, and checks the outputs, including power-up values and exact cycle timing. Run it (iverilog -g2012 + vvp for Verilog, ghdl -a/-e/-r for VHDL). If it fails, fix the module and run it again. Finish only when it passes.
- For a testbench task: run your testbench against the provided module and confirm it prints TB_PASS before you finish.
- Leave the requested file in place; extra tb_* files you create are fine.
