# Ibex task v0.1.0: pinned copy of the upstream Ibex/nangate45 SDC at the
# locked ORFS commit (flow/designs/nangate45/ibex/constraint.sdc).
# design ibex_core, clock core_clock on port clk_i, period 2.20 ns (fixed;
# not settable). Upstream input/output delay ratio (0.2) retained verbatim.
current_design ibex_core

set clk_name core_clock
set clk_port_name clk_i
set clk_period 2.2
set clk_io_pct 0.2

set clk_port [get_ports $clk_port_name]

create_clock -name $clk_name -period $clk_period $clk_port

set non_clock_inputs [all_inputs -no_clocks]

set_input_delay [expr $clk_period * $clk_io_pct] -clock $clk_name $non_clock_inputs
set_output_delay [expr $clk_period * $clk_io_pct] -clock $clk_name [all_outputs]
