# Ibex nangate45 area-under-timing task (M2-01)

One task: optimize physical design for the pinned Ibex/nangate45 flow
without changing function or timing constraints. GCD behavior, constraints,
baseline identity, and reward formula are unchanged; this task reuses the
same ORFS profile pin through a narrow task-specific config.

## Upstream qualification (pinned commit, network inspection 2026-09-16)

Pinned ORFS commit `036d106273e66855cd5214d49518fd0f0df7de61` (tag `26Q2`)
**does contain** the Ibex/nangate45 design -- no pin change was needed:

- `flow/designs/nangate45/ibex/config.mk` -- `DESIGN_NICKNAME = ibex`,
  `DESIGN_NAME = ibex_core`, `PLATFORM = nangate45`,
  `CORE_UTILIZATION ?= 50`, `PLACE_DENSITY_LB_ADDON = 0.20`,
  `TNS_END_PERCENT = 100`, `SYNTH_HDL_FRONTEND = slang`.
- `flow/designs/nangate45/ibex/constraint.sdc` -- `current_design
  ibex_core`, clock `core_clock` on port `clk_i`, period **2.2 ns**,
  IO ratio 0.2 (same shape as the GCD SDC, different clock/port/period).
- `flow/designs/nangate45/ibex/rules-base.json` -- present.
- `flow/designs/src/ibex_sv/*.sv` -- 20 SystemVerilog files
  (`ibex_alu`, `ibex_compressed_decoder`, `ibex_controller`, `ibex_core`,
  `ibex_counter`, `ibex_cs_registers`, `ibex_csr`, `ibex_decoder`,
  `ibex_ex_block`, `ibex_fetch_fifo`, `ibex_id_stage`, `ibex_if_stage`,
  `ibex_load_store_unit`, `ibex_multdiv_fast`, `ibex_multdiv_slow`,
  `ibex_pkg`, `ibex_pmp`, `ibex_prefetch_buffer`, `ibex_register_file_ff`,
  `ibex_wb_stage`) consumed via `sort(wildcard .../*.sv)`, plus the
  synthesis shim `syn/rtl/prim_clock_gating.v`; the lowRISC prim vendor
  tree is a Verilog *include* dir only.
- `flow/designs/src/ibex_sv/LICENSE` -- Apache-2.0 (lowRISC Ibex).
- `flow/platforms/nangate45/config.mk` -- `PLACE_DENSITY ?= 0.30`
  (Ibex `config.mk` does not override it).

## Pinned sources

- ORFS tag `26Q2`, commit `036d106273e66855cd5214d49518fd0f0df7de61`
  (same profile pin as GCD; verified via raw-file reads and the GitHub
  API at the pinned commit).
- Image `docker.io/openroad/orfs:26Q2@sha256:7832ae885e62933fcbfc486fbd9133f8c3bd1206d15c96e93bfad97432947b61`.
- Design config `flow/designs/nangate45/ibex/config.mk`.
- Task v0.1.0 SDC `constraint.sdc` (pinned copy of the upstream Ibex SDC;
  its own file because the clock -- 2.20 ns on `clk_i` -- differs from
  GCD's 0.60 ns copy), clock `core_clock`, period **2.20 ns**
  (fixed; not settable).
- RTL: the 21 files listed in `task.json` (`fixed_design.rtl`), immutable.
- Corners: typical (`NangateOpenCellLibrary_typical.lib`).
- Endpoint: `final`, through detailed route and final reports.

Full shared-pin details live in `../../toolchain.lock.json`; Ibex
`required_assets` live in `task.json` and `ibex_config.py`
(`REQUIRED_ASSETS`) so the shared lockfile's GCD-scoped `design` /
`required_assets` sections -- and GCD lock verification -- are untouched.

## Config surface (the only editable file)

File: `candidate_ibex.json` (workspace-relative; the sole
`allowed_edit_paths` entry, and a different filename from GCD's
`candidate.json` so scratch dirs, outputs, and baseline fingerprints stay
separate). It holds zero or more of two numeric overrides; missing keys
imply stock values.

| Knob | Min | Max | Stock | Unit | Stock source at pin |
| --- | --- | --- | --- | --- | --- |
| `PLACE_DENSITY` | 0.20 | 0.80 | 0.30 | fraction | `flow/platforms/nangate45/config.mk`: `export PLACE_DENSITY ?= 0.30` (Ibex `config.mk` does not override) |
| `CORE_UTILIZATION` | 20.0 | 90.0 | 50.0 | percent | `flow/designs/nangate45/ibex/config.mk`: `export CORE_UTILIZATION ?= 50` |

Bounds reuse GCD's task-supported safe ranges inside the ORFS semantic
ranges (density 0-1, utilization 0-100 percent). The `CORE_UTILIZATION`
stock of 50 (vs GCD's 55) is the one documented adjustment, demanded by
the upstream Ibex defaults.

Example stock `candidate_ibex.json`:

```json
{"CORE_UTILIZATION": 50.0, "PLACE_DENSITY": 0.3}
```

## Validation rules (`ibex_config.py`)

Same semantics as GCD's `config.py` (unknown keys rejected, finite JSON
numbers only, injection strings rejected, RTL/SDC/clock/library overrides
impossible through this interface). No `if design == ...` branches were
added to the generic core (`flow.py`, `evaluator.py`, `grader.py`,
`environment.py`); this task is a separate narrow module.

Python entry points:

```python
from silicon_env.environments.openroad import ibex_config as ibex

ibex.stock_candidate_config()          # {'PLACE_DENSITY': 0.3, ...}
ibex.validate_candidate_config({...})  # normalized floats or ContractError
ibex.make_ibex_task(seed=0)             # TaskSpec (also aliased to_task_spec)
ibex.load_task_spec()                  # TaskSpec from tasks/ibex/task.json
```

## Resource / preflight requirements (estimate; measurement in M2-02)

- Preliminary stock measurement (M2-01, Linux x86_64, pinned image+commit,
  `make DESIGN_CONFIG=./designs/nangate45/ibex/config.mk`,
  `WORK_HOME` fresh, `NUM_CORES=1`, `OR_SEED=0`, 4-CPU/16 GiB host):
  [run 35054041797](https://github.com/hzwgg-sudo/silicon-rl-envs/actions/runs/35054041797)
  reached the `final` endpoint with exit 0 in **1591 s wallclock**,
  DRC **0**, design area **30029 um²** (stdcell), but setup
  **WNS −0.0159 ns / TNS −0.0315 ns** (4 violations, fmax 451.3 MHz):
  the initial 2.20 ns spec is ~16 ps too tight for a zero-slack baseline.
  This is a calibration input to M2-02 clock selection (new task version),
  not a flow incompatibility; the `reference_run.status` stays
  `TBD-unverified` until M2-02's three-run qualification (which also
  records peak RSS — the GNU-time file missed upload in this probe).
  Artifacts: `ibex-stock-probe` (7-day retention).
- No Ibex stock run has been executed yet: `reference_run.status` is
  `TBD-unverified` and no `baseline.json` is created here (M2-02 scores it).
  The 7200 s wall-clock budget is an estimate for a full RISC-V core flow,
  not a measurement.
- Preflight floor (same as the shared lock): Linux x86_64 host (pinned
  image is linux/amd64 only), digest-pinned image, ORFS checkout at the
  pinned commit containing every `required_assets` entry, tools
  `openroad`/`yosys`/`make`, minimum 2 CPUs / 4 GiB RAM. 8+ CPUs and 16+
  GiB are recommended for real runs until M2-02 measures the reference.
- Scale signal: GCD at the same pin measured ~102 s wall / ~0.78 GiB
  child-RSS on a 1-CPU container; Ibex is a far larger design, so expect
  substantially longer wallclock and higher peak RSS.

## Licenses

- ORFS: BSD-3-Clause (upstream).
- Ibex RTL (`flow/designs/src/ibex_sv`): Apache-2.0 (lowRISC).
- Nangate45 platform files: see `flow/platforms/nangate45/LICENSE` in the
  pinned checkout. This repo vendors no PDK files, GDS, or LEF/LIB blobs.

## Real-tool stock run (Linux; not executed in M2-01)

In a pinned checkout on the Linux route, from the flow workdir with the
digest-pinned image:

```sh
make DESIGN_CONFIG=./designs/nangate45/ibex/config.mk
```

with `WORK_HOME=<scratch>/outputs`, `NUM_CORES=1`, `FLOW_VARIANT=default`,
and the `final` target/evidence -- the same shape as the GCD stock run
(`flow/Makefile`, `DESIGN_CONFIG=./designs/nangate45/gcd/config.mk`),
ending at the `final` endpoint (post-detailed-route reports). Expected
endpoint: `final` with routed, DRC-clean, zero-slack reports; measured
numbers are recorded by M2-02.
