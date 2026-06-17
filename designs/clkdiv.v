// Divide-by-2 clock divider feeding a downstream register.
// clk_div is a toggle flip-flop that halves clk; q is sampled on clk_div.
// The downstream domain is only timed correctly if clk_div is declared a
// generated clock (create_generated_clock -divide_by 2 sourced from clk).
// Without it, the q register is untimed (no launch/capture clock).
module clkdiv(
    input clk,
    input rst_n,
    input d,
    output reg q
);
    reg clk_div;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            clk_div <= 1'b0;
        else
            clk_div <= ~clk_div;
    end

    always @(posedge clk_div or negedge rst_n) begin
        if (!rst_n)
            q <= 1'b0;
        else
            q <= d;
    end
endmodule
