# Trusted POST_FINAL_REPORT hook, after final extraction and timing.
# OpenSTA check_setup returns true iff the selected checks have no errors.
set evidence_path $::env(REPORTS_DIR)/6_unconstrained.rpt
set constrained [check_setup -unconstrained_endpoints > $evidence_path]
set evidence_file [open $evidence_path a]
puts $evidence_file "unconstrained_check_passed: $constrained"
close $evidence_file
