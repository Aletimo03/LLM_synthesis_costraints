// Clock-domain-crossing 2-flop synchronizer.
// data_in is launched in the clk_a domain (src) and safely captured into the
// clk_b domain through a two-flop synchronizer (sync1 -> sync2). clk_a and clk_b
// are asynchronous, so the src -> sync1 crossing must be declared a false path
// (set_clock_groups -asynchronous, or set_false_path between the two clocks);
// otherwise STA times an impossible cross-domain relationship.
module cdc_sync(
    input clk_a,
    input clk_b,
    input rst_n,
    input data_in,
    output data_out
);
    reg src;            // clk_a domain
    reg sync1, sync2;   // clk_b domain synchronizer

    always @(posedge clk_a or negedge rst_n) begin
        if (!rst_n)
            src <= 1'b0;
        else
            src <= data_in;
    end

    always @(posedge clk_b or negedge rst_n) begin
        if (!rst_n) begin
            sync1 <= 1'b0;
            sync2 <= 1'b0;
        end else begin
            sync1 <= src;
            sync2 <= sync1;
        end
    end

    assign data_out = sync2;
endmodule
