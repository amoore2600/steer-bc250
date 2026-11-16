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
    // (write this section here)
    // END SECTION 1

    // SECTION 2: period counter
    // cnt counts ticks and wraps from 255 to 0; on the tick that wraps it, duty is latched into duty_q.
    // (write this section here)
    // END SECTION 2

    // SECTION 3: output compare
    // pwm is high while cnt < duty_q (one clock later); period_start marks the tick that wraps cnt.
    // (write this section here)
    // END SECTION 3
endmodule
