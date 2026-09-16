# GCD Rewards (M1-06)

Correctness-gated area scoring for the `gcd-nangate45` task
(`silicon_env/environments/openroad/grader.py`, stdlib-only pure
functions). Area improvements count only when the fixed design is
preserved: final-route completion, required checks, and timing bounds
must all hold, and the immutable-input evidence must match the
approved baseline.

## Formula (explicit, bounded)

For a **valid** candidate:

```
reward = 0.5 + 0.5 * clamp((baseline_area - candidate_area) / baseline_area, -1, 1)
```

For an **invalid** candidate: `reward = 0.0` (fail closed).

`score == reward` always. The clamp keeps every trainable reward in
`[0, 1]`: a huge regression (candidate area >= 2x baseline) clamps to
`0.0`; the upper clamp binds only at the degenerate limit
(candidate area -> 0).

## Gates (all must pass for `valid`)

| Gate | Rule | Failure code |
| --- | --- | --- |
| Metrics validity | `metrics.valid` true | `invalid-metrics` |
| Finite area | candidate area finite and > 0 | `nonfinite-metric` |
| Finite slack | WNS/TNS finite | `nonfinite-metric` |
| Timing | WNS >= 0.0 ns and TNS >= 0.0 ns | `timing-violation` |
| Final route | `routed_ok` true | `route-incomplete` |
| DRC | `drc_count == 0` (unknown fails closed) | `drc-dirty` / `missing-evidence` |
| Unconstrained | `unconstrained_paths == 0` (unknown fails closed) | `unconstrained-paths` / `missing-evidence` |
| Baseline trust | `validate_baseline` passes, area > 0 finite | `baseline-invalid` |
| Protected hash | `evidence.protected_hash` == baseline | `hash-mismatch` / `missing-evidence` |
| Toolchain refs | supplied `orfs_commit` / `image_pinned_ref` match | `hash-mismatch` |
| Evidence agreement | supplied check fields agree with metrics | `evidence-mismatch` |

Sign convention: OpenSTA reports negative slack on violation, so the
fixed bounds (`WNS_MIN_NS = 0.0`, `TNS_MIN_NS = 0.0`) require no
violation. A metrics-valid TNS is `<= 0` by construction, so the TNS
gate requires exactly zero. Bounds are absolute task constants, never
taken from the baseline. Unknown DRC/unconstrained indicators are
never defaulted to clean. The `TBD-unverified` placeholder baseline
fails scoring use (`baseline-invalid`).

`feasibility` reports the design constraints (routing, checks, timing)
while `valid` additionally requires baseline and evidence trust: a
hash-tampered run over a good design is feasible but not valid.
`area_delta = baseline_area - candidate_area` (raw, `um^2`) and
`reason_codes` are returned separately from the reward.

## Infrastructure exclusion

`grade_infra_error(reason)` carries **no trainable reward**: `reward`
is `0.0` but the grade is marked `excluded_from_aggregates=True` with
status `infra_error` (reason `infra-error-excluded`). Exclude these
grades from training aggregates -- they are not `0.0`s earned by a
bad design.

## Examples (baseline area 1000 um^2)

| Candidate | Outcome |
| --- | --- |
| Stock (1000 um^2, WNS/TNS 0, routed, DRC 0) | `valid`, reward `0.5`, `ok` |
| Better (900 um^2, all gates pass) | `valid`, reward `0.55` |
| Worse (1100 um^2, all gates pass) | `valid`, reward `0.45` |
| Smaller but WNS -0.05 ns | `invalid`, reward `0.0`, `timing-violation` |
| 3x area (all gates pass) | `valid`, reward `0.0` (clamped) |
| Hash-tampered evidence | `invalid`, reward `0.0`, `hash-mismatch` |
| Infra failure | excluded, `infra-error-excluded` |
