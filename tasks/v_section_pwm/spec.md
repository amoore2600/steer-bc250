pwm_gen is an 8-bit PWM generator. A prescaler makes a one-clock tick every PRE+1 clocks while en is high; a period
counter cnt counts those ticks from 0 to 255 and wraps; pwm is high while cnt is below the duty value latched at the
start of the period. Keep the header exactly as given; the grader may override PRE.

Registers (all power up as 0 through their declarations, all reset synchronously to 0 when reset is 1; reset wins over
everything else): pre_cnt (4 bits), tick, cnt (8 bits), duty_q (8 bits), and the outputs pwm and period_start.

At every rising edge of clk, when reset is 0:
- Prescaler: if en is 1 and pre_cnt == PRE, pre_cnt becomes 0 and tick becomes 1; if en is 1 and pre_cnt != PRE,
  pre_cnt becomes pre_cnt + 1 and tick becomes 0; if en is 0, pre_cnt holds and tick becomes 0.
- Period counter: if tick is 1, cnt becomes cnt + 1 (255 wraps to 0), and when cnt is 255 at that edge duty_q becomes
  duty. If tick is 0, cnt and duty_q hold.
- Output compare: pwm becomes 1 when cnt < duty_q (unsigned), else 0; period_start becomes 1 when tick is 1 and cnt is
  255, else 0.

Every right-hand side uses the values before the edge (nonblocking assignments), so pwm and period_start are one clock
behind cnt and tick.
