# silicon-rl-envs
RL environments for training and evaluating AI agents on real semiconductor engineering tasks.

## Requirements

- Python >= 3.10 (compatible with M1 Mac, CPU-only dev workflow, 8GB RAM friendly)
- Runtime dependencies: none (stdlib only)
- Dev dependencies: `pytest`, `ruff`

## Module layout

```text
silicon_env/              # main package
├── __init__.py           # package version
├── environments/         # environment implementations (M0 placeholder)
│   └── __init__.py
└── agents/               # agent implementations (M0 placeholder)
    └── __init__.py
tests/
└── test_imports.py       # smoke tests: all packages importable, no EDA/Docker/network/keys
```

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'  # runtime + pytest and ruff
```

## Local task execution (toy + GCD, M0-08/M1-08)

Run a task with an explicit action script, then regrade the saved submission.
Toy (`toy-text-edit`) and GCD (`gcd-nangate45`) adapters; other task IDs are
rejected with a clear error. No agent loop, network, EDA tools, or UI.
GCD flow/submit needs `ORFS_CHECKOUT` at the pinned commit on the Linux
route; without it GCD episodes fail closed as infrastructure.

```bash
# 1. Write a task file and an explicit action script.
python - <<'EOF'
import json
from silicon_env.environments.toy import TOY_TARGET_TEXT, make_toy_task
from pathlib import Path
Path("/tmp/toy-task.json").write_text(make_toy_task(seed=0).to_json() + "\n")
actions = [
    {"action_type": "read_file", "params": {"path": "note.txt"}},
    {"action_type": "write_file",
     "params": {"path": "note.txt", "content": TOY_TARGET_TEXT}},
    {"action_type": "submit", "params": {}},
]
Path("/tmp/toy-actions.json").write_text(json.dumps(actions, indent=2) + "\n")
EOF

# 2. Run one episode into a fresh output dir (writes summary.json,
#    trace.jsonl, manifest.json, submission/note.txt).
python scripts/run_task.py --task /tmp/toy-task.json \
    --actions /tmp/toy-actions.json --output-dir /tmp/toy-out
echo "run exit: $?"

# 3. Regrade the saved submission with the pure grader (toy) or the
#    independent evaluator (GCD, needs ORFS_CHECKOUT).
python scripts/grade_task.py --submission-dir /tmp/toy-out
echo "grade exit: $?"
cat /tmp/toy-out/summary.json
```

Installed console scripts `silicon-run-task` / `silicon-grade-task` expose the
same commands (`pip install -e .` first).

Outputs per run dir: `task.json`, `actions.json`, `summary.json`
(machine-readable: `passed`, `status`, `score`, `steps`, `message`, file refs),
`trace.jsonl`, `manifest.json` (verifiable with `verify_run`), and
`submission/note.txt` (the graded candidate).

Exit codes: `0` pass, `2` invalid submission or grading failure
(bad task/actions content, episode failed), `3` infrastructure or usage
failure (missing files, non-empty `--output-dir`, unsupported task ID,
I/O errors). Bad paths and malformed actions print a concise `error:` line
to stderr with a nonzero exit.

## GCD deterministic quickstart + release gate (M1-10)

`docs/gcd-quickstart.md` is the reproducible reset-to-grade flow for the
pinned `gcd-nangate45` task: provision the Linux route (ORFS @
`036d1062...`, digest-pinned image), preflight, scripted
`run_task` episode, independent regrade, verified baseline generation,
and the opt-in release gate. No manual file edits are needed.

Task **v0.2.0**, with its approved fixed **0.60 ns** clock, passed
[all six real Linux gate tests](https://github.com/hzwgg-sudo/silicon-rl-envs/actions/runs/34924691666). Stock seeds 7/8/9 matched exactly:
area **679.63 um²**, WNS/TNS **0 ns**, DRC **0**, unconstrained endpoints **0**.
Stock flow times were 101.77, 102.01 and 102.26 seconds; maximum GNU-time
child RSS was 0.781 GiB under the 1 CPU/4 GiB container profile. This RSS
measurement is not whole-cgroup memory.

Three identical episodes shared one semantic trace hash and reward 0.5.
Both scripted agents scored 0.5; bounded search used four probes and reported
no improvement. The gate also passed forged-report rejection, budget
exhaustion, legal/invalid edits, CLI execution and independent CLI regrading.
The packaged baseline contains the measured three-run capture and tool versions.

Reproduce via the manual `openroad-integration` workflow or the quickstart.
Compact Actions artifacts are retained for seven days; key measurements and
trace hashes are also archived in the repository.
The same gate logic runs with fakes in the default fast suite (no
EDA/Docker/network): `pytest tests/test_gcd_release_gate.py`.

## Test

```bash
pytest
```

Default tests require no EDA tools, Docker, network access, or API keys.

## Lint

```bash
ruff check .
```
