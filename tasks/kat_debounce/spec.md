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
