# GCD nangate45 area-under-timing task (M1-02)

One task: optimize physical design for the pinned GCD/nangate45 flow
without changing function or timing constraints.

## Pinned sources

- ORFS tag `26Q2`, commit `036d106273e66855cd5214d49518fd0f0df7de61`.
- Image `docker.io/openroad/orfs:26Q2@sha256:7832ae885e62933fcbfc486fbd9133f8c3bd1206d15c96e93bfad97432947b61`.
- Design config `flow/designs/nangate45/gcd/config.mk`.
- Task v0.2.0 SDC `constraint.sdc`, clock `core_clock`, period **0.60 ns**
  (fixed; not settable). This approved benchmark revision preserves upstream
  input/output delay ratios and replaces the timing-infeasible 0.46 ns target.
- RTL `flow/designs/src/gcd/gcd.v` (immutable).
- Corners: typical (`NangateOpenCellLibrary_typical.lib`).
- Endpoint: `final` (default `make` target through detailed route to final
  reports; executed in M1-03, defined here only).

Full pin details live in `../../toolchain.lock.json`.

## Config surface (the only editable file)

File: `candidate.json` (workspace-relative; the sole
`allowed_edit_paths` entry). It holds zero or more of two numeric
overrides; missing keys imply stock values.

| Knob | Min | Max | Stock | Unit | Stock source at pin |
| --- | --- | --- | --- | --- | --- |
| `PLACE_DENSITY` | 0.20 | 0.80 | 0.30 | fraction | `flow/platforms/nangate45/config.mk`: `export PLACE_DENSITY ?= 0.30` (GCD `config.mk` does not override) |
| `CORE_UTILIZATION` | 20.0 | 90.0 | 55.0 | percent | `flow/designs/nangate45/gcd/config.mk`: `export CORE_UTILIZATION ?= 55` |

Bounds are task-supported safe ranges inside the ORFS semantic ranges
(density 0-1, utilization 0-100 percent), recorded as supported by the pin.

Example stock `candidate.json`:

```json
{"CORE_UTILIZATION": 55.0, "PLACE_DENSITY": 0.3}
```

## Validation rules (`config.py`)

- Unknown keys rejected (so `CLOCK_PERIOD`, `SDC_FILE`, `VERILOG_FILES`,
  `ABC_AREA`, and friends cannot pass through this interface).
- Values must be finite JSON numbers (bools rejected); out-of-range
  values rejected.
- Strings rejected, including Tcl/Make injection fragments containing
  `; $ \`backticks\` [ ] { }` newlines, `#`, `& | ! < > \\ ' "`.
- RTL, SDC, libraries, design config, and grading inputs
  (`reports/`, `results/`) are never in `allowed_edit_paths`.

Python entry points:

```python
from silicon_env.environments.openroad import config as gcd

gcd.stock_candidate_config()          # {'PLACE_DENSITY': 0.3, ...}
gcd.validate_candidate_config({...})  # normalized floats or ContractError
gcd.make_gcd_task(seed=0)             # TaskSpec (also aliased to_task_spec)
gcd.load_task_spec()                  # TaskSpec from tasks/gcd/task.json
```

## Immutability

A candidate cannot relax the clock or alter RTL through the allowed
interface: the clock period, SDC path, RTL paths, library paths, and
endpoint are fixed facts in `task.json` (`fixed_design`) and in
`config.py` (`FIXED_*`), while `allowed_edit_paths` covers only
`candidate.json`. Protected assets are listed in `PROTECTED_ASSETS`
(hash groundwork for later tickets; no flow/metrics/grader here).

## Out of scope

Real-tool execution, metrics, and the grader land in M1-03+. A real
pinned-tool run was not attempted for this ticket (dev host constraints);
default tests are lightweight (no EDA/Docker/network/keys).
