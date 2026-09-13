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
pip install -e .        # runtime only (stdlib, no extra deps)
pip install -e .[dev]   # with pytest + ruff for development
```

## Local task execution (toy-only, M0-08)

Run a task with an explicit action script, then regrade the saved submission.
Toy adapter only (`toy-text-edit`); other task IDs are rejected with a clear
error. No agent loop, network, EDA tools, or UI.

```bash
# 1. Write a task file and an explicit action script.
/tmp/silicon-venv/bin/python - <<'EOF'
import json
from silicon_env.environments.toy import TOY_TARGET_TEXT, make_toy_task
Path = __import__("pathlib").Path
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

# 3. Regrade the saved submission with the pure grader.
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

## Test

```bash
pytest
```

Default tests require no EDA tools, Docker, network access, or API keys.

## Lint

```bash
ruff check .
```
