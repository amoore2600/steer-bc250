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
    localparam [2:0] STABLE = 3'd4;
    reg [1:0] sync = 2'b00;
    reg [2:0] cnt = 3'd0;
    always @(posedge clk) begin
        sync <= {sync[0], din};
        rise <= 1'b0;
        if (sync[1] == dout) begin
            cnt <= cnt;
        end else if (cnt == STABLE - 3'd1) begin
            cnt  <= 3'd0;
            dout <= sync[1];
            rise <= sync[1];
        end else begin
            cnt <= cnt + 3'd1;
        end
    end
endmodule
`default_nettype wire
