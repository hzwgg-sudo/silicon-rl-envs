# GCD deterministic quickstart: reset to grade (M1-10)

One trustworthy environment end to end: the pinned `gcd-nangate45` task
from a clean checkout to a trusted grade, with no manual file edits.
Every step is an exact command; every output is machine-readable
(`summary.json`, `trace.jsonl`, `manifest.json`).

Pinned profile (mirror of `toolchain.lock.json`, do not float these):

- ORFS commit `036d106273e66855cd5214d49518fd0f0df7de61` (tag `26Q2`)
- Image
  `docker.io/openroad/orfs:26Q2@sha256:7832ae885e62933fcbfc486fbd9133f8c3bd1206d15c96e93bfad97432947b61`
- Supported architecture: **linux/amd64 only**
  (`platform_support`: os `linux`, arch `x86_64`/`amd64`). Apple-silicon
  hosts must use the Linux route (container/VM); native macOS EDA
  execution is out of scope.

## 0. Provision the Linux route

```bash
git init orfs
git -C orfs remote add origin https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts
git -C orfs fetch --depth 1 origin 036d106273e66855cd5214d49518fd0f0df7de61
git -C orfs checkout 036d106273e66855cd5214d49518fd0f0df7de61
export ORFS_CHECKOUT="$PWD/orfs"

docker pull 'docker.io/openroad/orfs:26Q2@sha256:7832ae885e62933fcbfc486fbd9133f8c3bd1206d15c96e93bfad97432947b61'
```

## 1. Preflight (fail closed before any flow)

```bash
python scripts/check_openroad.py --orfs-checkout "$ORFS_CHECKOUT"
echo "preflight exit: $?"
```

Exit `0` means the tools, pinned assets, architecture, and resource
floor all check out. Any `FAIL:` line is a blocker diagnosis (see
[Failure diagnosis](#failure-diagnosis)); nothing has run yet.

## 2. Reset-to-grade: scripted episode, then independent regrade

Write the task and an explicit action script (no hand-editing of
workspace files at any point):

```bash
python - <<'EOF'
import json
from silicon_env.environments.openroad import config as gcd
from pathlib import Path
Path("/tmp/gcd-task.json").write_text(
    gcd.make_gcd_task(seed=7).to_json() + "\n")
actions = [
    {"action_type": "read_file",
     "params": {"path": "candidate.json"}},
    {"action_type": "write_file",
     "params": {"path": "candidate.json",
                "content": json.dumps(
                    {"PLACE_DENSITY": 0.5, "CORE_UTILIZATION": 55.0})}},
    {"action_type": "run_tool",
     "params": {"tool": "openroad-flow"}},
    {"action_type": "submit", "params": {}},
]
Path("/tmp/gcd-actions.json").write_text(json.dumps(actions, indent=2) + "\n")
EOF

python scripts/run_task.py --task /tmp/gcd-task.json \
    --actions /tmp/gcd-actions.json --output-dir /tmp/gcd-out
echo "run exit: $?"
cat /tmp/gcd-out/summary.json
```

Without `ORFS_CHECKOUT` at the pinned commit this fails closed as
infrastructure (exit `3`) but still persists machine-readable outputs.
With the pinned checkout it runs the fixed `final` endpoint and the
independent clean-room evaluator produces the grade.

Regrade the saved submission without re-running the agent episode
(trusted re-run from `candidate.json` bytes only):

```bash
python scripts/grade_task.py --submission-dir /tmp/gcd-out
echo "grade exit: $?"
```

Exit codes (both commands): `0` pass, `2` invalid submission or
grading failure, `3` infrastructure or usage failure.

## 3. Baseline generation (verified record, stock x3)

```bash
python scripts/generate_openroad_baseline.py \
    --orfs-checkout "$ORFS_CHECKOUT" \
    --output /tmp/gcd-baseline.json \
    --seed 7 --timeout-s 7200
```

Exits nonzero on any validity failure or unexplained metric drift, and
writes nothing on failure (never fabricated values). To score with it,
point grading at this record (see the release-gate step below).

## 4. Deterministic release gate (opt-in, real pinned toolchain)

```bash
SILICON_RUN_GCD_E2E=1 \
ORFS_CHECKOUT="$ORFS_CHECKOUT" \
SILICON_GCD_BASELINE=/tmp/gcd-baseline.json \
python -m pytest tests/integration/test_gcd_e2e.py -v
```

This runs three fresh episodes with the same seed + actions and checks
identical semantic trace hashes, metrics within the
declared tolerances, equal rewards, stock/legal/invalid handling,
tamper rejection, and budget exhaustion. Everything skips by default;
without `SILICON_RUN_GCD_E2E=1` + `ORFS_CHECKOUT` each test reports
its blocked reason instead of running.

The same gate logic runs with fakes in the default fast suite (no
EDA/Docker/network):

```bash
python -m pytest tests/test_gcd_release_gate.py -q
```

The manual CI route (`.github/workflows/openroad-integration.yml`,
workflow_dispatch only, single worker, 7-day compact artifact
retention) runs steps 0-4 on a provisioned Linux worker.

## 5. Larger Linux machine

Same commands, with an explicit resource floor check first:

```bash
python scripts/check_openroad.py --orfs-checkout "$ORFS_CHECKOUT" \
    --min-ram-gb 8 --min-cpus 4
python scripts/generate_openroad_baseline.py \
    --orfs-checkout "$ORFS_CHECKOUT" \
    --output /tmp/gcd-baseline.json \
    --seed 7 --timeout-s 7200
SILICON_RUN_GCD_E2E=1 \
ORFS_CHECKOUT="$ORFS_CHECKOUT" \
SILICON_GCD_BASELINE=/tmp/gcd-baseline.json \
python -m pytest tests/integration/test_gcd_e2e.py -v
```

Record the measured wallclock and peak RSS from the baseline run into
the table below when a provisioned run completes.

## Measured RAM/runtime

| Step | Wallclock | Peak RSS | Machine | Status |
| --- | --- | --- | --- | --- |
| Preflight | TBD | n/a | Mac arm64, 8 GB RAM | Blocked: container daemon stopped, image is linux/amd64 |
| Stock flow x3 (baseline) | TBD-unverified | TBD-unverified | TBD | Blocked: no EDA/Linux run attempted on dev host |
| Release gate (3 episodes + grading) | TBD-unverified | TBD-unverified | TBD | Blocked: same as above |
| Reference GCD run (`resources.reference_run`) | TBD-unverified | TBD-unverified | TBD | See `toolchain.lock.json`: do not promise 8 GB support before measuring |

No values are claimed until a real pinned run measures them. Binary
tool versions are likewise TBD until a pinned-image run probes them.

## Failure diagnosis

| Symptom | Meaning | Where to look |
| --- | --- | --- |
| `FAIL: tool 'openroad' not found on PATH` | EDA tools absent; use the Linux route with the pinned image | Preflight output; `toolchain.lock.json` `tools` |
| `FAIL: required asset missing: ...` | Checkout is not the pinned commit or is incomplete | Re-fetch `036d1062...` per step 0 |
| `FAIL: unsupported OS/architecture` | Host is not linux/amd64 (e.g. Mac arm64) | Run on the Linux route; Mac runs stay skipped/TBD |
| `FAIL: insufficient RAM/CPUs` | Below the reference-run floor | Larger machine (step 5) |
| `run exit: 2` (`invalid_submission`) | Bad candidate content (unknown key, out-of-range value, injection string, malformed JSON) | `summary.json` `message`, `trace.jsonl` step event; workspace is NOT mutated |
| `run exit: 3` (`infra_error`) | Missing `ORFS_CHECKOUT`, bad paths, non-empty `--output-dir`, tool crash | stderr `error:` line, `summary.json` `status`, `manifest.json` |
| Gate reports `no parseable final metrics` | Report discovery found nothing usable under the flow workdir (possible on-disk layout drift) | Flow scratch logs; `flow.py` declared artifact contract |
| Gate reports metric drift | Runs disagree beyond tolerances | Per-run traces/manifests in the uploaded gate evidence (7-day retention) |
| `baseline-invalid` reason code | Baseline record is `TBD-unverified` or pins drifted | Generate a verified baseline (step 3); never score against TBD |

Tampering cannot improve a grade by construction: the evaluator
re-runs from `candidate.json` bytes in a fresh scratch, agent-visible
numbers are never read, and forged files inside the submission are
rejected (`unauthorized-file` / `protected-asset`, reward `0.0`).
