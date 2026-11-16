`timescale 1ns/10ps
`default_nettype wire
module tb_equiv;
    integer seed, seed0, errs, checks, known, cyc, cycles;
    reg  clk = 0;
    reg  reset = 0;
    reg  en = 0;
    reg  [7:0] duty = 0;
    wire g_pwm, c_pwm;
    wire g_period_start, c_period_start;
    pwm_gen_golden #(.PRE(1)) u_g (.clk(clk), .reset(reset), .en(en), .duty(duty), .pwm(g_pwm), .period_start(g_period_start));
    pwm_gen #(.PRE(1)) u_c (.clk(clk), .reset(reset), .en(en), .duty(duty), .pwm(c_pwm), .period_start(c_period_start));
    function [31:0] r32(input integer dummy); begin r32 = $random(seed); end endfunction
    function [63:0] r64(input integer dummy); begin r64 = {$random(seed), $random(seed)}; end endfunction
    // chance(n): true with probability n/256
    function chance(input integer n); reg [7:0] v; begin v = $random(seed); chance = (v < n); end endfunction

    initial begin #(0); forever #(5) clk = ~clk; end
    always @(posedge clk) cyc <= cyc + 1;
    always @(negedge clk) begin
        reset <= ((r32(0) % 4096) == 0);
        if (chance(4)) en <= ~en;
        if (chance(8)) duty <= r32(0);
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
        $display("MISMATCH_INPUTS cyc=%0d reset=%h en=%h duty=%h", cyc, reset, en, duty);
    end endtask
    task check; begin checks = checks + 1;
        cmp_bits_(g_pwm, c_pwm, 1, bad_t, kn_t); known = known + kn_t;
        if (bad_t) begin errs = errs + 1; if (errs == 1) show_inputs; if (errs <= 5)
            $display("MISMATCH t=%0t cyc=%0d port=pwm golden=%h candidate=%h", $time, cyc, g_pwm, c_pwm); end
        cmp_bits_(g_period_start, c_period_start, 1, bad_t, kn_t); known = known + kn_t;
        if (bad_t) begin errs = errs + 1; if (errs == 1) show_inputs; if (errs <= 5)
            $display("MISMATCH t=%0t cyc=%0d port=period_start golden=%h candidate=%h", $time, cyc, g_period_start, c_period_start); end
        cmp_bits_(u_g.pre_cnt, u_c.pre_cnt, 4, bad_t, kn_t); known = known + kn_t;
        if (bad_t) begin errs = errs + 1; if (errs == 1) show_inputs; if (errs <= 5)
            $display("MISMATCH t=%0t cyc=%0d state=pre_cnt golden=%h candidate=%h", $time, cyc, u_g.pre_cnt, u_c.pre_cnt); end
        cmp_bits_(u_g.tick, u_c.tick, 1, bad_t, kn_t); known = known + kn_t;
        if (bad_t) begin errs = errs + 1; if (errs == 1) show_inputs; if (errs <= 5)
            $display("MISMATCH t=%0t cyc=%0d state=tick golden=%h candidate=%h", $time, cyc, u_g.tick, u_c.tick); end
        cmp_bits_(u_g.cnt, u_c.cnt, 8, bad_t, kn_t); known = known + kn_t;
        if (bad_t) begin errs = errs + 1; if (errs == 1) show_inputs; if (errs <= 5)
            $display("MISMATCH t=%0t cyc=%0d state=cnt golden=%h candidate=%h", $time, cyc, u_g.cnt, u_c.cnt); end
        cmp_bits_(u_g.duty_q, u_c.duty_q, 8, bad_t, kn_t); known = known + kn_t;
        if (bad_t) begin errs = errs + 1; if (errs == 1) show_inputs; if (errs <= 5)
            $display("MISMATCH t=%0t cyc=%0d state=duty_q golden=%h candidate=%h", $time, cyc, u_g.duty_q, u_c.duty_q); end
    end endtask
    initial begin
        errs = 0; checks = 0; known = 0; cyc = 0;
        if (!$value$plusargs("seed=%d", seed)) seed = 1;
        seed0 = seed;
        if (!$value$plusargs("cycles=%d", cycles)) cycles = 8000;
        reset = 1'b0;
        en = 1'b1;
        duty = 8'd0;
        #(0.5);
        while (cyc < cycles && errs < 20) begin check; #(1.0); end
        if (errs == 0) $display("EQUIV_PASS seed=%0d cycles=%0d checks=%0d known_bits=%0d", seed0, cyc, checks, known);
        else $display("EQUIV_FAIL seed=%0d mismatching_checks=%0d first_cycle_window=%0d", seed0, errs, cyc);
        $finish;
    end
endmodule
