create_clock -name clk -period 5.0 [get_ports clk]
set_input_delay 0.5 -clock clk [get_ports a]
set_output_delay 0.5 -clock clk [get_ports y]
