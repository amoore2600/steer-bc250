// SPDX-License-Identifier: MIT
// Copyright (c) 2026 the steer-bc250 authors
// pwm_gen.v -- an 8-bit PWM generator in three labelled sections: a prescaler, the period counter with its duty
// latch, and the output compare. Every register is declared at module level, outside the sections.
module pwm_gen #(
    parameter [3:0] PRE = 4'd9
) (
    input  wire       clk,
    input  wire       reset,
    input  wire       en,
    input  wire [7:0] duty,
    output reg        pwm = 1'b0,
    output reg        period_start = 1'b0
);
    reg [3:0] pre_cnt = 4'd0;
    reg       tick = 1'b0;
    reg [7:0] cnt = 8'd0;
    reg [7:0] duty_q = 8'd0;

    // SECTION 1: prescaler
    // tick is high for one clock every PRE+1 clocks of en; while en is low pre_cnt holds and tick is 0.
    always @(posedge clk) begin
        if (reset) begin
            pre_cnt <= 4'd0;
            tick    <= 1'b0;
        end else if (en) begin
            if (pre_cnt == PRE) begin
                pre_cnt <= 4'd0;
                tick    <= 1'b1;
            end else begin
                pre_cnt <= pre_cnt + 4'd1;
                tick    <= 1'b0;
            end
        end else begin
            tick <= 1'b0;
        end
    end
    // END SECTION 1

    // SECTION 2: period counter
    // cnt counts ticks and wraps from 255 to 0; on the tick that wraps it, duty is latched into duty_q.
    always @(posedge clk) begin
        if (reset) begin
            cnt    <= 8'd0;
            duty_q <= 8'd0;
        end else if (tick) begin
            cnt <= cnt + 8'd1;
            if (cnt == 8'd255)
                duty_q <= duty;
        end
    end
    // END SECTION 2

    // SECTION 3: output compare
    // pwm is high while cnt < duty_q (one clock later); period_start marks the tick that wraps cnt.
    always @(posedge clk) begin
        if (reset) begin
            pwm          <= 1'b0;
            period_start <= 1'b0;
        end else begin
            pwm          <= (cnt < duty_q);
            period_start <= tick && (cnt == 8'd255);
        end
    end
    // END SECTION 3
endmodule
