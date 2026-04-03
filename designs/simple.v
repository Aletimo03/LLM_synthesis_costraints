module simple(
    input clk,
    input a,
    output reg y
);
    always @(posedge clk) begin
        y <= a;
    end
endmodule