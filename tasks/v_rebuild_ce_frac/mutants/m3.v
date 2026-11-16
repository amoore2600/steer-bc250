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
    reg [W-1:0] acc = {W{1'b0}};
    wire [W:0] sum = {1'b0, acc} + {1'b0, NUM};
    initial ce = 1'b0;
    always @(posedge clk) begin
        if (reset) begin
            ce  <= 1'b0;
        end else if (sum >= {1'b0, DEN}) begin
            acc <= sum[W-1:0] - DEN;
            ce  <= 1'b1;
        end else begin
            acc <= sum[W-1:0];
            ce  <= 1'b0;
        end
    end
endmodule
