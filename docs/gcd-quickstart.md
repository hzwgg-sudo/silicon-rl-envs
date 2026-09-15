# GCD deterministic quickstart

M1's real-tool release gate is **not yet verified**. The host commands below launch
flows inside the pinned Linux amd64 image. Pulling the image alone does not put
EDA tools on the host. Do not interpret the default unit tests as a GCD run.

## 0. Prepare a Linux x86_64 host

From the silicon-rl-envs repository on a Linux host with Docker running:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
export ORFS_IMAGE='docker.io/openroad/orfs:26Q2@sha256:7832ae885e62933fcbfc486fbd9133f8c3bd1206d15c96e93bfad97432947b61'
docker pull "$ORFS_IMAGE"
git init orfs
git -C orfs remote add origin https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts
git -C orfs fetch --depth 1 origin 036d106273e66855cd5214d49518fd0f0df7de61
git -C orfs checkout 036d106273e66855cd5214d49518fd0f0df7de61
export ORFS_CHECKOUT="$PWD/orfs"
mkdir -p gcd-evidence
```

Run the Python commands on the host. Preflight probes binaries inside the
pinned image. Every flow, including each independent grade, launches a new
restricted container: no network, non-root UID, read-only root filesystem,
dropped capabilities, 1 CPU and 4 GiB memory limit. Only disposable copies of
trusted flow sources and fresh outputs are mounted. The agent workspace,
original source checkout and Docker socket are never mounted.

The source checkout pins flow inputs; tools come from the digest-pinned
image. Preflight rejects a wrong Git revision or modified/extra flow inputs.
Source-copy disk overhead and actual EDA peak memory remain unmeasured.

## 1. Preflight and baseline (before scoring)

```bash
python scripts/check_openroad.py --orfs-checkout "$ORFS_CHECKOUT"
python scripts/generate_openroad_baseline.py \
  --orfs-checkout "$ORFS_CHECKOUT" \
  --output "$PWD/gcd-evidence/baseline.json" --seed 7 --timeout-s 7200
export SILICON_GCD_BASELINE="$PWD/gcd-evidence/baseline.json"
```

Baseline generation performs three independent stock runs. Every invocation
has its own `WORK_HOME`, final artifacts, and reports. The record includes
probed tool versions and total elapsed time. EDA peak RSS stays unknown until
measured inside the container; Docker-client RSS is not substituted. Generation fails
closed on invalid metrics or drift. A verified baseline measures the stock
reference; it does not waive the grader's zero-negative-slack and correctness
gates. No measured stock timing or reward is promised before this runs.

## 2. Reset, inspect, edit, run, submit, and independently regrade

```bash
python - <<'PY'
import json
from dataclasses import replace
from pathlib import Path
from silicon_env.environments.openroad import config as gcd
root = Path("gcd-evidence")
task = gcd.make_gcd_task(seed=7, max_wallclock_s=14400)
task = replace(task, grader=replace(task.grader, timeout_s=7200))
(root / "task.json").write_text(task.to_json() + "\n")
actions = [
    {"action_type": "read_file", "params": {"path": "candidate.json"}},
    {"action_type": "write_file", "params": {
        "path": "candidate.json",
        "content": json.dumps({"PLACE_DENSITY": 0.5, "CORE_UTILIZATION": 55})}},
    {"action_type": "run_tool", "params": {"tool": "openroad-flow"}},
    {"action_type": "submit", "params": {}},
]
(root / "actions.json").write_text(json.dumps(actions))
PY
python scripts/run_task.py --task gcd-evidence/task.json \
  --actions gcd-evidence/actions.json --output-dir gcd-evidence/episode
cat gcd-evidence/episode/summary.json
python scripts/grade_task.py --submission-dir gcd-evidence/episode
```

Both CLI commands honor `SILICON_GCD_BASELINE`. The default packaged record
is intentionally unverified and cannot score. Output directories must be
fresh. Exit codes: `0` pass, `2` invalid submission/grading failure,
`3` infrastructure/usage failure.

## 3. Deterministic release gate

```bash
SILICON_RUN_GCD_E2E=1 GATE_SEED=7 GATE_TIMEOUT_S=7200 \
python -m pytest tests/integration/test_gcd_e2e.py -v \
  --basetemp="$PWD/gcd-evidence/gate"
```

The gate compares three episodes' semantic traces, metrics, and rewards,
then checks stock/legal/invalid candidates, forgery rejection, and budgets.
An explicitly enabled gate with an invalid checkout or baseline fails;
ordinary tests skip real EDA runs. Task seeds are forwarded to detailed routing via OR_SEED (modulo 2**31);
other stages have no explicit seed control wired by the adapter. `NUM_CORES=1` and `make -j1` constrain
tool and build parallelism separately.

The manual `openroad-integration` GitHub Actions workflow runs on a Linux host and launches
the same per-invocation restricted containers.
It retains the baseline and compact traces/manifests for seven days.

## Report and evidence contract

Paths under each invocation's `outputs/`:

- `reports/nangate45/gcd/default/6_finish.rpt`: final WNS/TNS.
- `logs/nangate45/gcd/default/6_report.log`: final design cell area.
- `logs/nangate45/gcd/default/5_2_route.json`: routed DRC count.
- `reports/nangate45/gcd/default/6_unconstrained.rpt`: the trusted final
  hook's OpenSTA unconstrained-endpoint check.
- `results/nangate45/gcd/default/6_final.{gds,def,v}`: required fresh final artifacts.

Missing, stale, malformed or failed-flow evidence cannot yield a passing
grade. Report paths and commands were checked against the pinned upstream
source, including [ORFS report generation](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/blob/036d106273e66855cd5214d49518fd0f0df7de61/flow/scripts/report_metrics.tcl)
and [OpenSTA setup checks](https://github.com/The-OpenROAD-Project/OpenSTA/blob/43177bba8f5f88dfb7dc35795242080a4fe2e986/search/Search.tcl).
They still require a real pinned-image run.

## Qualification status

[Linux run 34887304175](https://github.com/hzwgg-sudo/silicon-rl-envs/actions/runs/34887304175)
completed stock routing three times with identical metrics: area 903.336 um²,
WNS −0.04544 ns, TNS −0.737691 ns, DRC 0, unconstrained endpoints 0.
The fixed 0.46 ns task therefore fails timing and cannot produce an approved
baseline. The release gate remains blocked on benchmark feasibility.

OpenROAD/Yosys versions are now probed and pinned. Stock end-to-end flow
elapsed times were 82.58, 83.67 and 83.21 seconds. Maximum GNU-time child RSS
was 0.783 GiB (not whole-cgroup memory). The runner enforced 1 CPU and a 4 GiB
memory cap. See [the measured qualification record](m1-stock-qualification.json).

The production parser uses full-precision `6_report.json` timing and standard
cell area; rounded text must not conceal small negative slack. Real reports
are retained in `tests/fixtures/openroad/real-26Q2` for regression tests.
A benchmark-specification decision is required before changing the pinned
clock target; the zero-negative-slack grader has not been relaxed.
