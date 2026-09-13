"""M0-06 environment tests: toy reset/step/submit lifecycle, invalid
actions, budget truncation, double reset, and instance independence."""

from pathlib import Path

import pytest

from silicon_env.environment import EnvironmentError
from silicon_env.environments.toy import (
    TOY_FILE,
    TOY_INITIAL_TEXT,
    TOY_TARGET_TEXT,
    ToyEnvironment,
    grade_candidate,
    make_toy_task,
)
from silicon_env.task import Action
from silicon_env.types import GradeStatus, StepStatus


def make_env(tmp_path: Path, **kw) -> ToyEnvironment:
    kw.setdefault("work_root", tmp_path / "work")
    return ToyEnvironment(**kw)


def act(action_type: str, params: dict | None = None, step_index: int = 0) -> Action:
    return Action(
        schema_version=1, action_type=action_type, params=dict(params or {}), step_index=step_index
    )


def run_sequence(env: ToyEnvironment, actions: list[Action]):
    return [env.step(a) for a in actions]


# --- happy path ------------------------------------------------------------


def test_successful_edit_and_submit(tmp_path):
    env = make_env(tmp_path)
    try:
        obs0 = env.reset(make_toy_task(seed=7))
        assert obs0.step_index == 0
        assert "note.txt" in obs0.stdout_tail

        read = env.step(act("read_file", {"path": TOY_FILE}))
        assert read.status == StepStatus.SUCCESS
        assert not read.done
        assert read.observation.stdout_tail == TOY_INITIAL_TEXT

        write = env.step(act("write_file", {"path": TOY_FILE, "content": TOY_TARGET_TEXT}))
        assert write.status == StepStatus.SUCCESS
        assert not write.done

        grade = env.submit()
        assert grade.status == GradeStatus.PASS
        assert grade.passed and grade.score == 1.0
        assert grade.provenance is not None and grade.provenance.seed == 7
        assert env.done
    finally:
        env.close()


def test_submit_action_via_step_terminates(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(make_toy_task())
        env.step(act("write_file", {"path": TOY_FILE, "content": TOY_TARGET_TEXT}))
        result = env.step(act("submit", {}))
        assert result.done
        assert result.status == StepStatus.SUCCESS
        assert result.reward == 1.0
        with pytest.raises(EnvironmentError, match="terminated"):
            env.step(act("read_file", {"path": TOY_FILE}))
    finally:
        env.close()


def test_failing_submit_reports_invalid_submission(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(make_toy_task())
        result = env.step(act("submit", {}))  # still initial text
        assert result.done
        assert result.status == StepStatus.INVALID_SUBMISSION
        assert result.reward == 0.0
    finally:
        env.close()


def test_grade_candidate_pure():
    passed, _ = grade_candidate(TOY_TARGET_TEXT, expected=TOY_TARGET_TEXT)
    assert passed
    failed, _ = grade_candidate("nope\n", expected=TOY_TARGET_TEXT)
    assert not failed


# --- invalid actions --------------------------------------------------------


def test_invalid_action_returns_invalid_submission_and_consumes_step(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(make_toy_task(seed=1))
        # run_tool is not in the toy allowed_actions.
        bad = env.step(act("run_tool", {"tool": "python"}))
        assert bad.status == StepStatus.INVALID_SUBMISSION
        assert not bad.done
        assert env.budget_tracker is not None
        assert env.budget_tracker.steps_used == 1
        # Missing required param is also invalid, not a raise.
        bad2 = env.step(act("read_file", {}))
        assert bad2.status == StepStatus.INVALID_SUBMISSION
        assert env.budget_tracker.steps_used == 2
        # Protected-path write maps to invalid as well.
        bad3 = env.step(act("write_file", {"path": "other.txt", "content": "x"}))
        assert bad3.status == StepStatus.INVALID_SUBMISSION
    finally:
        env.close()


def test_step_index_monotonic_and_validated(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(make_toy_task())
        r1 = env.step(act("read_file", {"path": TOY_FILE}))
        r2 = env.step(act("read_file", {"path": TOY_FILE}))
        assert r2.step_index == r1.step_index + 1
        assert r2.observation.step_index == r2.step_index
        r2.validate()
    finally:
        env.close()


# --- misuse -----------------------------------------------------------------


def test_actions_before_reset_fail_clearly(tmp_path):
    env = make_env(tmp_path)
    try:
        with pytest.raises(EnvironmentError, match="reset"):
            env.step(act("read_file", {"path": TOY_FILE}))
        with pytest.raises(EnvironmentError, match="reset"):
            env.submit()
    finally:
        env.close()


def test_actions_after_termination_fail_clearly_and_close_idempotent(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(make_toy_task())
        env.step(act("write_file", {"path": TOY_FILE, "content": TOY_TARGET_TEXT}))
        env.submit()
        with pytest.raises(EnvironmentError, match="terminated"):
            env.step(act("read_file", {"path": TOY_FILE}))
        with pytest.raises(EnvironmentError, match="terminated"):
            env.submit()
        env.close()
        env.close()  # idempotent
        assert env.closed
        with pytest.raises(EnvironmentError, match="closed"):
            env.reset(make_toy_task())
    finally:
        env.close()


# --- budgets -----------------------------------------------------------------


def test_budget_truncation_returns_timeout_done(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(make_toy_task(max_steps=2))
        first = env.step(act("read_file", {"path": TOY_FILE}))
        assert first.status == StepStatus.SUCCESS
        assert not first.done
        second = env.step(act("read_file", {"path": TOY_FILE}))
        assert second.status == StepStatus.SUCCESS
        assert not second.done
        third = env.step(act("read_file", {"path": TOY_FILE}))
        assert third.status == StepStatus.TIMEOUT
        assert third.done
        assert third.observation.timed_out
    finally:
        env.close()


def test_wallclock_exhaustion_blocks_dispatch(tmp_path):
    class Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    env = make_env(tmp_path, clock=clock)
    try:
        env.reset(make_toy_task(max_steps=10, max_wallclock_s=5.0))
        clock.now = 5.0  # exactly at the boundary
        result = env.step(act("read_file", {"path": TOY_FILE}))
        assert result.status == StepStatus.TIMEOUT
        assert result.done
    finally:
        env.close()


# --- reset / isolation --------------------------------------------------------


def test_reset_twice_starts_fresh_episode(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(make_toy_task(seed=3))
        env.step(act("write_file", {"path": TOY_FILE, "content": "edited\n"}))
        assert env.workspace is not None
        first_root = env.workspace.root
        assert first_root.is_dir()

        obs = env.reset(make_toy_task(seed=3))
        assert not first_root.exists()  # old workspace cleaned up
        assert env.workspace is not None
        assert env.workspace.root != first_root
        assert env.workspace.read_text(TOY_FILE) == TOY_INITIAL_TEXT
        assert obs.step_index == 0
        assert env.budget_tracker is not None
        assert env.budget_tracker.steps_used == 0
    finally:
        env.close()


def test_independent_simultaneous_instances(tmp_path):
    env_a = make_env(tmp_path / "a")
    env_b = make_env(tmp_path / "b")
    try:
        env_a.reset(make_toy_task(seed=1))
        env_b.reset(make_toy_task(seed=2))
        assert env_a.workspace is not None and env_b.workspace is not None
        assert env_a.workspace.root != env_b.workspace.root
        env_a.step(act("write_file", {"path": TOY_FILE, "content": "aaa\n"}))
        assert env_a.workspace.read_text(TOY_FILE) == "aaa\n"
        assert env_b.workspace.read_text(TOY_FILE) == TOY_INITIAL_TEXT
        grade_b = env_b.submit()
        assert grade_b.status == GradeStatus.FAIL
        # A still active after B terminated.
        read = env_a.step(act("read_file", {"path": TOY_FILE}))
        assert read.observation.stdout_tail == "aaa\n"
    finally:
        env_a.close()
        env_b.close()


def test_determinism_same_seed_and_sequence(tmp_path):
    actions = [
        act("read_file", {"path": TOY_FILE}),
        act("write_file", {"path": TOY_FILE, "content": TOY_TARGET_TEXT}),
    ]

    def run_once(work: Path):
        env = make_env(work)
        try:
            obs0 = env.reset(make_toy_task(seed=42))
            results = run_sequence(env, actions)
            grade = env.submit()
            return (
                obs0.stdout_tail,
                [(r.status, r.observation.stdout_tail, r.done) for r in results],
                (grade.status, grade.score),
            )
        finally:
            env.close()

    first = run_once(tmp_path / "w1")
    second = run_once(tmp_path / "w2")
    assert first == second
