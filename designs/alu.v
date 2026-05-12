module alu(
    input clk,
    input [7:0] a,
    input [7:0] b,
    input [2:0] op,
    output reg [7:0] result,
    output reg zero
);
    always @(posedge clk) begin
        case (op)
            3'd0: result <= a + b;
            3'd1: result <= a - b;
            3'd2: result <= a & b;
            3'd3: result <= a | b;
            3'd4: result <= a ^ b;
            3'd5: result <= a << 1;
            3'd6: result <= a >> 1;
            default: result <= 8'd0;
        endcase
        zero <= (result == 8'd0);
    end
endmodule
