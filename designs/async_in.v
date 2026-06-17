// Asynchronous-input synchronizer (single clock domain).
// async_in arrives from outside the chip with no relationship to clk (e.g. a
// button or an off-chip strobe). It is captured through a two-flop synchronizer
// (sync1 -> sync2) before use. Because async_in has no launching clock, the
// path into sync1 must be declared a false path (set_false_path -from
// [get_ports async_in]); STA cannot otherwise assign it a meaningful setup/hold
// requirement. d is an ordinary synchronous input registered into q, so the
// design still has a normal input->register timing path to constrain.
module async_in(
    input clk,
    input rst_n,
    input d,
    input async_in,
    output reg q,
    output sync_out
);
    reg sync1, sync2;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sync1 <= 1'b0;
            sync2 <= 1'b0;
            q     <= 1'b0;
        end else begin
            sync1 <= async_in;
            sync2 <= sync1;
            q     <= d;
        end
    end

    assign sync_out = sync2;
endmodule
