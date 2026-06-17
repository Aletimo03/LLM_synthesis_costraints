create_clock -name clk -period 5.0 [get_ports clk]
set_false_path -from [get_ports rst_n]
set_input_delay 0.5 -clock clk [get_ports enable]
set_output_delay 0.5 -clock clk [get_ports count]
