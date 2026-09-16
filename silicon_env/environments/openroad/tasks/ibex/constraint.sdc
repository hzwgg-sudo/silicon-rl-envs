# Ibex task v0.2.0: pinned copy of the upstream Ibex/nangate45 SDC at the
# locked ORFS commit (flow/designs/nangate45/ibex/constraint.sdc), with the
# clock margined to 2.30 ns (fixed; not settable). v0.1.0 carried the
# upstream 2.20 ns period verbatim, but the measured stock run (Linux,
# pinned image+commit, run 35054041797) closed at WNS -0.0159 ns / TNS
# -0.0315 ns -- i.e. the 2.20 ns spec demands a 2.2158 ns min period and is
# ~16 ps too tight for the zero-negative-slack grader. v0.2.0 keeps the
# upstream shape (design ibex_core, clock core_clock on port clk_i, IO
# ratio 0.2 via the same expr) and only re-times the period to 2.30 ns
# (~3.8% margin over 2.2158 ns, robust to detailed-route seed variation).
# Mirrors the GCD v0.1.0 -> v0.2.0 precedent (0.46 ns -> fixed 0.60 ns).
current_design ibex_core

set clk_name core_clock
set clk_port_name clk_i
set clk_period 2.3
set clk_io_pct 0.2

set clk_port [get_ports $clk_port_name]

create_clock -name $clk_name -period $clk_period $clk_port

set non_clock_inputs [all_inputs -no_clocks]

set_input_delay [expr $clk_period * $clk_io_pct] -clock $clk_name $non_clock_inputs
set_output_delay [expr $clk_period * $clk_io_pct] -clock $clk_name [all_outputs]
