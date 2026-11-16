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
