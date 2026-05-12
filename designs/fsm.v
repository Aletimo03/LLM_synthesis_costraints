module fsm(
    input clk,
    input rst_n,
    input start,
    input done_in,
    output reg busy,
    output reg [1:0] state_out
);
    localparam IDLE = 2'd0, RUN = 2'd1, WAIT = 2'd2, DONE = 2'd3;
    reg [1:0] state, next_state;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) state <= IDLE;
        else        state <= next_state;
    end

    always @(*) begin
        next_state = state;
        case (state)
            IDLE: if (start) next_state = RUN;
            RUN:  next_state = WAIT;
            WAIT: if (done_in) next_state = DONE;
            DONE: next_state = IDLE;
        endcase
    end

    always @(*) begin
        busy = (state != IDLE);
        state_out = state;
    end
endmodule
