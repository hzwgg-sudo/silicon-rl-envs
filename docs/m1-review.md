# M1 independent review — 2026-09-14

Reviewed `codex/m1-openroad-gcd` starting at `2fc64f4`, against issues #10–#19,
the production CLI/evaluator path, and the pinned ORFS/OpenROAD/OpenSTA source.
The original 437-passing-test suite did not establish a working real-tool path.

## Corrected findings

| Priority | Finding | Correction |
| --- | --- | --- |
| P1 | All runs wrote into the same ORFS output tree. A second Make invocation could reuse stock or another candidate's artifacts. | A fresh WORK_HOME for every probe, baseline run and grade; a real Git/Make regression proves repeat runs and changed parameters produce independent results. |
| P1 | Production grading always passed empty reports to the parser. Discovery guessed filenames and could select stale/intermediate reports. | One shared reader for exact final timing/area paths, with timestamp and path checks; used by baseline generation, evaluator and gate. |
| P1 | DRC and unconstrained-path evidence required by the grader was never collected from real output. | Parse the pinned router DRC metric and run a trusted final-stage OpenSTA unconstrained-endpoint check. Missing evidence remains unknown and cannot pass grading. |
| P1 | Failed flows reaching final artifacts could still produce valid parsed metrics. | Flow status now gates metric validity, independently of report content. |
| P1 | The default backend was a host process; CI pulled Docker without executing EDA inside it. Independent grading lacked the required container boundary. | Per-invocation restricted ContainerRunner adapter: digest pin, no network, non-root, read-only root, dropped capabilities, 1 CPU/4 GiB. Only disposable source/output copies are mounted. CI and CLI use this path. |
| P1 | Preflight checked file presence but not the source revision or modified inputs. | Verify Git root, exact commit and clean flow inputs, including ignored settings files; reject broken binary probes. |
| P1 | Matching failed/zero-reward episodes could satisfy the real gate; enabled invalid checkouts/baselines could skip. | Require successful grades and a positive stock score; explicit misconfiguration fails the enabled gate. |
| P2 | Make's `-j1` and OMP setting did not limit OpenROAD's own thread count. The router seed control was overlooked. | Pass NUM_CORES=1 and detailed-router OR_SEED (task seed modulo 2**31); record remaining uncontrolled stages. |
| P2 | Search treated every successful flow as feasible and had no reliable final metrics in its observation. | Expose parsed final metrics and require area, timing and completion evidence before selecting a candidate. |
| P2 | CLI could only load the unverified packaged baseline; the quickstart scored before generating one. | Honor SILICON_GCD_BASELINE in run and regrade; document baseline-first execution with suitable task deadlines. |
| P2 | Unit conversion could overflow finite inputs into infinite metrics marked valid. | Reject overflow and keep serialized metrics finite. |
| P2 | Built distributions omitted JSON task assets and scripts. | Include manifests, lockfile, baseline and evidence scripts; verify an installed wheel outside the source checkout. |

The profile records elapsed time, but does **not** substitute host Docker-client
RSS for actual EDA memory. Container peak RSS remains unmeasured.

## Verification

- Full default suite: 455 passed, 13 opt-in EDA/container checks skipped.
- Ruff and `git diff --check`: clean.
- Shell wrapper syntax: checked with `bash -n`.
- Distribution: wheel built and installed to a temporary directory; packaged
  task manifest, lockfile and evidence hook loaded outside the repository.
- Regressions include real Git and Make execution, source tampering, repeat-run
  isolation, stale reports, failed-flow scoring, DRC/unconstrained evidence,
  overflow, CLI baseline selection, agent timing feasibility and container
  mount/profile construction. No mock result is presented as EDA evidence.

## Still required before M1 completion

Run [the corrected quickstart](gcd-quickstart.md) or the manual integration
workflow on Linux x86_64 with Docker. Actual image/tool compatibility, stock
area/timing, successful zero-negative-slack grading, baseline repeatability,
agent runs, semantic trace determinism and EDA RAM/runtime remain unverified.
The new container adapter and final evidence hook also require that real run.
Do not merge on the strength of the default suite alone or relax timing and
tolerance gates to manufacture a pass.

The container adapter copies the trusted flow tree per run; its disk/time cost
is not yet measured. The public task seed controls detailed routing only.

## Pinned source references

- [ORFS Make targets and artifact names](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/blob/036d106273e66855cd5214d49518fd0f0df7de61/flow/Makefile)
- [Output roots and NUM_CORES](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/blob/036d106273e66855cd5214d49518fd0f0df7de61/flow/scripts/variables.mk)
- [Final timing/area report generation](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/blob/036d106273e66855cd5214d49518fd0f0df7de61/flow/scripts/report_metrics.tcl)
- [Detailed routing and OR_SEED](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/blob/036d106273e66855cd5214d49518fd0f0df7de61/flow/scripts/detail_route.tcl)
- [GCD DRC metric key](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/blob/036d106273e66855cd5214d49518fd0f0df7de61/flow/designs/nangate45/gcd/rules-base.json)
- [OpenSTA check_setup return semantics](https://github.com/The-OpenROAD-Project/OpenSTA/blob/43177bba8f5f88dfb7dc35795242080a4fe2e986/search/Search.tcl)

## Subsequent real qualification

Run 34887304175 completed all three stock flows in restricted containers.
They matched exactly at area 903.336 um², WNS −0.04544 ns, TNS −0.737691 ns,
DRC 0 and unconstrained endpoints 0. The 0.46 ns task is timing-infeasible.
Actual reports exposed `wns max` / `tns max` text formatting and precision
loss from rounded reports. The parser now uses full-precision final JSON,
and baseline validation rejects every run that fails fixed task constraints.
Binary versions and measured RSS/runtime are recorded in the lockfile and
[m1-stock-qualification.json](m1-stock-qualification.json).

The pending benchmark clock decision prevents a verified baseline and merge;
the stock failure is never reclassified as success. Earlier TBD statements
about binary probes and stock-flow execution are superseded by this evidence.
