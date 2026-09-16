# Actual pinned ORFS 26Q2 reports

Captured from stock GCD, seed 7, in Linux qualification run
https://github.com/hzwgg-sudo/silicon-rl-envs/actions/runs/34887304175
at commit 57355e28e5996e51fba55d9a197ae8aa985aa915.

The flow completed, DRC and unconstrained-endpoint checks passed, but
WNS -0.04544 ns / TNS -0.737691 ns violate the fixed 0.46 ns clock task.
These are regression fixtures, not a verified scoring baseline.
The JSON timing values preserve precision that the text report rounds away.
