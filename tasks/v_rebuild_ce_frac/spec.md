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
