module adder(
    input clk,
    input [15:0] a,
    input [15:0] b,
    input cin,
    output reg [15:0] sum,
    output reg cout
);
    always @(posedge clk) begin
        {cout, sum} <= a + b + cin;
    end
endmodule
