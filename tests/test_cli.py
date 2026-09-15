"""M0-08 CLI tests: real subprocess runs of scripts/run_task.py and
scripts/grade_task.py covering success, bad args, output collision, and
regrading of a saved toy submission."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from silicon_env.environments.toy import TOY_TARGET_TEXT, make_toy_task
from silicon_env.trace import verify_run

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = REPO_ROOT / "scripts" / "run_task.py"
GRADE_SCRIPT = REPO_ROOT / "scripts" / "grade_task.py"


def write_task_and_actions(tmp_path: Path, *, seed: int = 0, passing: bool = True):
    task_path = tmp_path / "task.json"
    actions_path = tmp_path / "actions.json"
    task_path.write_text(make_toy_task(seed=seed).to_json() + "\n", encoding="utf-8")
    content = TOY_TARGET_TEXT if passing else "wrong\n"
    actions = [
        {"action_type": "write_file", "params": {"path": "note.txt", "content": content}},
        {"action_type": "submit", "params": {}},
    ]
    actions_path.write_text(json.dumps(actions) + "\n", encoding="utf-8")
    return task_path, actions_path


def run_cli(script: Path, *argv: str, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, str(script), *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
        timeout=120,
    )


def test_run_task_success_produces_trace_and_grade(tmp_path):
    task_path, actions_path = write_task_and_actions(tmp_path, seed=3)
    out = tmp_path / "out"
    proc = run_cli(
        RUN_SCRIPT,
        "--task",
        str(task_path),
        "--actions",
        str(actions_path),
        "--output-dir",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr
    assert "passed=true" in proc.stdout
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] is True
    assert summary["status"] == "pass"
    assert summary["score"] == 1.0
    assert (out / "trace.jsonl").is_file()
    assert (out / "manifest.json").is_file()
    assert (out / "submission" / "note.txt").read_text(encoding="utf-8") == TOY_TARGET_TEXT
    manifest = verify_run(out)
    assert manifest["passed"] is True


def test_run_task_failing_episode_exits_two(tmp_path):
    task_path, actions_path = write_task_and_actions(tmp_path, passing=False)
    out = tmp_path / "out"
    proc = run_cli(
        RUN_SCRIPT,
        "--task",
        str(task_path),
        "--actions",
        str(actions_path),
        "--output-dir",
        str(out),
    )
    assert proc.returncode == 2, proc.stderr
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] is False


def test_run_task_bad_paths_are_concise_nonzero(tmp_path):
    missing = tmp_path / "nope.json"
    out = tmp_path / "out"
    proc = run_cli(
        RUN_SCRIPT,
        "--task",
        str(missing),
        "--actions",
        str(missing),
        "--output-dir",
        str(out),
    )
    assert proc.returncode != 0
    assert "error:" in proc.stderr
    assert len(proc.stderr.strip().splitlines()) <= 5


def test_run_task_malformed_actions_are_concise_nonzero(tmp_path):
    task_path, _ = write_task_and_actions(tmp_path)
    bad = tmp_path / "bad-actions.json"
    bad.write_text('[{"action_type": 42}]', encoding="utf-8")
    out = tmp_path / "out"
    proc = run_cli(
        RUN_SCRIPT,
        "--task",
        str(task_path),
        "--actions",
        str(bad),
        "--output-dir",
        str(out),
    )
    assert proc.returncode != 0
    assert "error:" in proc.stderr


def test_run_task_output_collision_is_infra_error(tmp_path):
    task_path, actions_path = write_task_and_actions(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing.txt").write_text("taken", encoding="utf-8")
    proc = run_cli(
        RUN_SCRIPT,
        "--task",
        str(task_path),
        "--actions",
        str(actions_path),
        "--output-dir",
        str(out),
    )
    assert proc.returncode == 3
    assert "not empty" in proc.stderr


def test_grade_task_regrades_saved_submission(tmp_path):
    task_path, actions_path = write_task_and_actions(tmp_path, seed=5)
    out = tmp_path / "out"
    proc = run_cli(
        RUN_SCRIPT,
        "--task",
        str(task_path),
        "--actions",
        str(actions_path),
        "--output-dir",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr
    grade_proc = run_cli(GRADE_SCRIPT, "--submission-dir", str(out))
    assert grade_proc.returncode == 0, grade_proc.stderr
    grade = json.loads((out / "grade.json").read_text(encoding="utf-8"))
    assert grade["passed"] is True
    assert grade["status"] == "pass"
    assert "passed=true" in grade_proc.stdout


def test_grade_task_failing_submission_exits_two(tmp_path):
    task_path, actions_path = write_task_and_actions(tmp_path, passing=False)
    out = tmp_path / "out"
    assert (
        run_cli(
            RUN_SCRIPT,
            "--task",
            str(task_path),
            "--actions",
            str(actions_path),
            "--output-dir",
            str(out),
        ).returncode
        == 2
    )
    grade_proc = run_cli(GRADE_SCRIPT, "--submission-dir", str(out))
    assert grade_proc.returncode == 2


def test_non_toy_task_id_rejected_with_clear_error(tmp_path):
    task_path, actions_path = write_task_and_actions(tmp_path)
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    payload["task_id"] = "eda-real-task"
    task_path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "out"
    proc = run_cli(
        RUN_SCRIPT,
        "--task",
        str(task_path),
        "--actions",
        str(actions_path),
        "--output-dir",
        str(out),
    )
    assert proc.returncode == 3
    assert "unsupported task_id" in proc.stderr
    assert "gcd-nangate45" in proc.stderr
