`timescale 1ns/10ps
`default_nettype wire
module tb_equiv;
    integer seed, seed0, errs, checks, known, cyc, cycles;
    reg  clk = 0;
    reg  reset = 0;
    wire g_ce, c_ce;
    ce_frac_golden #(.W(8), .NUM(5), .DEN(12)) u_g (.clk(clk), .reset(reset), .ce(g_ce));
    ce_frac #(.W(8), .NUM(5), .DEN(12)) u_c (.clk(clk), .reset(reset), .ce(c_ce));
    function [31:0] r32(input integer dummy); begin r32 = $random(seed); end endfunction
    function [63:0] r64(input integer dummy); begin r64 = {$random(seed), $random(seed)}; end endfunction
    // chance(n): true with probability n/256
    function chance(input integer n); reg [7:0] v; begin v = $random(seed); chance = (v < n); end endfunction

    initial begin #(0); forever #(5) clk = ~clk; end
    always @(posedge clk) cyc <= cyc + 1;
    always @(negedge clk) begin
        if (chance(1)) reset <= 1'b1; else reset <= 1'b0;
    end
    integer kn_t; reg bad_t;
    task automatic cmp_bits_(input [1023:0] g, input [1023:0] c, input integer w, output reg bad, output integer kn);
        integer i;
        begin bad = 0; kn = 0;
            if (g === c) begin if (^g[1023:0] !== 1'bx) kn = w; end
            else for (i = 0; i < w; i = i + 1) if (g[i] === 1'b0 || g[i] === 1'b1) begin
                kn = kn + 1; if (c[i] !== g[i]) bad = 1; end
        end
    endtask
    task show_inputs; begin
        $display("MISMATCH_INPUTS cyc=%0d reset=%h", cyc, reset);
    end endtask
    task check; begin checks = checks + 1;
        cmp_bits_(g_ce, c_ce, 1, bad_t, kn_t); known = known + kn_t;
        if (bad_t) begin errs = errs + 1; if (errs == 1) show_inputs; if (errs <= 5)
            $display("MISMATCH t=%0t cyc=%0d port=ce golden=%h candidate=%h", $time, cyc, g_ce, c_ce); end
    end endtask
    initial begin
        errs = 0; checks = 0; known = 0; cyc = 0;
        if (!$value$plusargs("seed=%d", seed)) seed = 1;
        seed0 = seed;
        if (!$value$plusargs("cycles=%d", cycles)) cycles = 8000;
        reset = 1'b0;
        #(0.5);
        while (cyc < cycles && errs < 20) begin check; #(1.0); end
        if (errs == 0) $display("EQUIV_PASS seed=%0d cycles=%0d checks=%0d known_bits=%0d", seed0, cyc, checks, known);
        else $display("EQUIV_FAIL seed=%0d mismatching_checks=%0d first_cycle_window=%0d", seed0, errs, cyc);
        $finish;
    end
endmodule
