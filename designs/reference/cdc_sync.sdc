create_clock -name clk_a -period 5.0 [get_ports clk_a]
create_clock -name clk_b -period 7.0 [get_ports clk_b]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_false_path -from [get_ports rst_n]
set_input_delay 0.5 -clock clk_a [get_ports data_in]
set_output_delay 0.5 -clock clk_b [get_ports data_out]
