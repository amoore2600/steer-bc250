// SPDX-License-Identifier: MIT
// Copyright (c) 2026 the steer-bc250 authors
// tb_debounce.v -- known-answer checker for debounce. Observes the ports only. din changes at falling edges; the
// outputs are compared at every falling edge (before din changes) with a cycle model of the statement.
// Phases: glitches of 1 to 3 clocks (must never reach dout), stable levels of 4 to 12 clocks (must pass after the
// two-flip-flop synchroniser and the 4-clock wait), then random levels of 1 to 9 clocks.
// Last line: "KAT debounce: PASS" or "KAT debounce: FAIL (<n> errors)".
`timescale 1ns/1ps
`default_nettype none
module tb_debounce;
    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg din = 1'b0;
    wire dout, rise;

    debounce dut (.clk(clk), .din(din), .dout(dout), .rise(rise));

    // cycle model: s1, s2 = the synchroniser, same = clocks in a row that s2 differed from q
    reg s1 = 1'b0, s2 = 1'b0, q = 1'b0, r = 1'b0;
    integer same = 0;
    always @(posedge clk) begin
        s1 <= din;
        s2 <= s1;
        r  <= 1'b0;
        if (s2 == q) begin
            same <= 0;
        end else if (same == 3) begin
            same <= 0;
            q    <= s2;
            r    <= s2;
        end else begin
            same <= same + 1;
        end
    end

    integer errors = 0, checks = 0, i, n, seed = 7;
    always @(negedge clk) begin
        checks = checks + 1;
        if (dout !== q || rise !== r) begin
            errors = errors + 1;
            if (errors <= 10)
                $display("ERROR t=%0t: din=%b -> dout %b rise %b, expected dout %b rise %b", $time, din, dout, rise, q, r);
        end
    end

    task level(input v, input integer clocks);
        begin
            din = v;
            repeat (clocks) @(negedge clk);
        end
    endtask

    initial begin
        @(negedge clk);
        for (n = 1; n <= 3; n = n + 1) begin       // glitches high, then low after a stable high
            level(1'b1, n); level(1'b0, 12);
        end
        level(1'b1, 12);
        for (n = 1; n <= 3; n = n + 1) begin
            level(1'b0, n); level(1'b1, 12);
        end
        for (n = 4; n <= 12; n = n + 1) begin      // stable levels of every length
            level(1'b0, n); level(1'b1, n);
        end
        for (i = 0; i < 4000; i = i + 1)           // random levels
            level($random(seed), 1 + ({$random(seed)} % 9));
        if (errors == 0)
            $display("KAT debounce: PASS (%0d checks)", checks);
        else
            $display("KAT debounce: FAIL (%0d errors)", errors);
        $finish;
    end
endmodule
`default_nettype wire
