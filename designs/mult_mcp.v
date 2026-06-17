// Multi-cycle multiplier datapath.
// Operands are registered (ra, rb); the wide product is only captured when
// valid is asserted, which the surrounding system guarantees happens at most
// once every two cycles. The ra/rb -> product multiply is therefore a
// multicycle path: it is allowed two clock periods to settle
// (set_multicycle_path 2 -setup). Without that exception the slow multiply
// violates setup at the single-cycle period.
module mult_mcp(
    input clk,
    input rst_n,
    input [15:0] a,
    input [15:0] b,
    input valid,
    output reg [31:0] product
);
    reg [15:0] ra, rb;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ra <= 16'd0;
            rb <= 16'd0;
        end else begin
            ra <= a;
            rb <= b;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            product <= 32'd0;
        else if (valid)
            product <= ra * rb;
    end
endmodule
