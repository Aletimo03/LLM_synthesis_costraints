module shift_reg(
    input clk,
    input rst_n,
    input serial_in,
    output [7:0] parallel_out,
    output serial_out
);
    reg [7:0] sr;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) sr <= 8'd0;
        else        sr <= {sr[6:0], serial_in};
    end
    assign parallel_out = sr;
    assign serial_out = sr[7];
endmodule
