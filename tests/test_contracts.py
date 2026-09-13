"""M0-02 contract tests: round-trips, unknown versions, missing fields,
NaN/infinity, invalid budgets, and conflicting options.

Pure validation only: failing cases must raise before any workspace or tool
process could be created (these modules perform no I/O at all).
"""

import json
import math
import pathlib

import pytest

from silicon_env.grader import GradeResult, StepResult
from silicon_env.task import Action, Observation, TaskSpec
from silicon_env.types import (
    Budget,
    ContractError,
    GraderConfig,
    GradeStatus,
    Metric,
    Provenance,
    StepStatus,
)


def make_task() -> TaskSpec:
    return TaskSpec(
        schema_version=1,
        task_id="gcd-flow",
        task_version="0.1.0",
        source_ref="tasks/gcd@v0.1.0",
        toolchain_refs={"openroad": "v2.0-1234", "yosys": "0.33"},
        seed=42,
        allowed_actions=("read_file", "write_file", "run_tool", "submit"),
        allowed_edit_paths=("design/*.sdc", "src/*.v"),
        read_only=False,
        budgets=Budget(max_steps=10, max_wallclock_s=600.0, max_tool_calls=8),
        grader=GraderConfig(
            grader_id="rule-grader",
            grader_version="0.1.0",
            timeout_s=60.0,
            required_metrics=("area_um2",),
        ),
    )


def make_observation(step: int = 0) -> Observation:
    return Observation(
        schema_version=1,
        step_index=step,
        tool_name="openroad",
        exit_code=0,
        stdout_tail="ok",
        stderr_tail="",
        timed_out=False,
        duration_s=1.5,
    )


def make_provenance() -> Provenance:
    return Provenance(
        task_id="gcd-flow",
        task_version="0.1.0",
        seed=42,
        toolchain_refs={"openroad": "v2.0-1234"},
        grader_id="rule-grader",
        grader_version="0.1.0",
    )


def make_step_result(status: StepStatus = StepStatus.SUCCESS) -> StepResult:
    return StepResult(
        schema_version=1,
        step_index=3,
        status=status,
        reward=1.0,
        done=status in (StepStatus.SUCCESS, StepStatus.TIMEOUT, StepStatus.INFRA_ERROR),
        observation=make_observation(step=3),
        metrics=(Metric(name="area_um2", value=123.0, unit="um^2"),),
        provenance=make_provenance(),
        message="done",
    )


def make_grade_result(status: GradeStatus = GradeStatus.PASS) -> GradeResult:
    return GradeResult(
        schema_version=1,
        task_id="gcd-flow",
        task_version="0.1.0",
        status=status,
        score=0.75,
        passed=(status == GradeStatus.PASS),
        metrics=(Metric(name="area_um2", value=123.0, unit="um^2"),),
        provenance=make_provenance(),
        message="graded",
        details={"attempts": 2},
    )


# --- valid round trips ---


def test_task_spec_round_trip_dict_and_json():
    task = make_task()
    clone = TaskSpec.from_dict(task.to_dict())
    assert clone == task
    from_json = TaskSpec.from_json(task.to_json())
    assert from_json == task
    payload = json.loads(task.to_json())
    assert payload["task_id"] == "gcd-flow"
    assert payload["budgets"]["max_steps"] == 10
    assert payload["grader"]["grader_id"] == "rule-grader"


def test_action_round_trip():
    action = Action(
        schema_version=1,
        action_type="write_file",
        params={"path": "design/foo.sdc", "content": "x"},
        step_index=2,
    )
    assert Action.from_dict(action.to_dict()) == action
    assert Action.from_json(action.to_json()) == action
    make_task().validate_action(action)


def test_observation_round_trip():
    obs = make_observation(step=1)
    assert Observation.from_dict(obs.to_dict()) == obs
    assert Observation.from_json(obs.to_json()) == obs


@pytest.mark.parametrize("status", list(StepStatus))
def test_step_result_all_statuses_round_trip(status):
    done = status in (StepStatus.SUCCESS, StepStatus.TIMEOUT, StepStatus.INFRA_ERROR,
                      StepStatus.TOOL_FAILURE)
    result = StepResult(
        schema_version=1,
        step_index=0,
        status=status,
        reward=0.5,
        done=done if status not in (StepStatus.TIMEOUT, StepStatus.INFRA_ERROR) else True,
        observation=make_observation(step=0),
        metrics=(Metric(name="latency", value=2.0, unit="ns"),),
        provenance=make_provenance(),
        message=status.value,
    )
    clone = StepResult.from_json(result.to_json())
    assert clone == result
    assert clone.status == status
    assert clone.metrics[0].unit == "ns"
    assert clone.provenance is not None
    assert clone.provenance.toolchain_refs == {"openroad": "v2.0-1234"}


@pytest.mark.parametrize("status", list(GradeStatus))
def test_grade_result_all_statuses_round_trip(status):
    result = GradeResult(
        schema_version=1,
        task_id="gcd-flow",
        task_version="0.1.0",
        status=status,
        score=1.0 if status == GradeStatus.PASS else 0.0,
        passed=(status == GradeStatus.PASS),
        metrics=(Metric(name="area_um2", value=10.0, unit="um^2"),),
        provenance=make_provenance(),
        message=status.value,
        details={"status": status.value},
    )
    clone = GradeResult.from_json(result.to_json())
    assert clone == result
    assert clone.status == status
    assert clone.details == {"status": status.value}


# --- unknown versions ---


@pytest.mark.parametrize(
    "cls,factory",
    [
        (TaskSpec, make_task),
        (Action, lambda: Action(schema_version=1, action_type="noop", step_index=0)),
        (Observation, make_observation),
        (StepResult, make_step_result),
        (GradeResult, make_grade_result),
    ],
)
def test_unknown_schema_version_rejected(cls, factory):
    payload = factory().to_dict()
    payload["schema_version"] = 999
    with pytest.raises(ContractError, match="unsupported schema_version"):
        cls.from_dict(payload)
    bad_json = json.dumps(payload)
    with pytest.raises(ContractError, match="unsupported schema_version"):
        cls.from_json(bad_json)


def test_unknown_action_type_rejected():
    action = Action(schema_version=1, action_type="noop", step_index=0).to_dict()
    action["action_type"] = "teleport"
    with pytest.raises(ContractError, match="unknown action_type"):
        Action.from_dict(action)


def test_unknown_step_status_rejected():
    payload = make_step_result().to_dict()
    payload["status"] = "exploded"
    with pytest.raises(ContractError, match="unknown step status"):
        StepResult.from_dict(payload)


def test_unknown_grade_status_rejected():
    payload = make_grade_result().to_dict()
    payload["status"] = "exploded"
    with pytest.raises(ContractError, match="unknown grade status"):
        GradeResult.from_dict(payload)


# --- missing / unknown fields ---


@pytest.mark.parametrize(
    "factory,cls,missing",
    [
        (make_task, TaskSpec, "task_id"),
        (
            lambda: Action(schema_version=1, action_type="noop", step_index=0),
            Action,
            "action_type",
        ),
        (make_observation, Observation, "tool_name"),
        (make_step_result, StepResult, "reward"),
        (make_grade_result, GradeResult, "score"),
    ],
)
def test_missing_required_field_rejected(factory, cls, missing):
    payload = factory().to_dict()
    del payload[missing]
    with pytest.raises(ContractError, match="missing required fields"):
        cls.from_dict(payload)


@pytest.mark.parametrize(
    "factory,cls",
    [
        (make_task, TaskSpec),
        (lambda: Action(schema_version=1, action_type="noop", step_index=0), Action),
        (make_observation, Observation),
        (make_step_result, StepResult),
        (make_grade_result, GradeResult),
    ],
)
def test_unknown_fields_rejected(factory, cls):
    payload = factory().to_dict()
    payload["bogus_field"] = 1
    with pytest.raises(ContractError, match="unknown fields"):
        cls.from_dict(payload)


# --- NaN / infinity ---


def test_nan_inf_reward_and_score_rejected():
    for bad in (math.nan, math.inf, -math.inf):
        payload = make_step_result().to_dict()
        payload["reward"] = bad
        with pytest.raises(ContractError, match="finite"):
            StepResult.from_dict(payload)
        payload = make_grade_result().to_dict()
        payload["score"] = bad
        with pytest.raises(ContractError, match="finite"):
            GradeResult.from_dict(payload)


def test_nan_inf_metric_value_rejected():
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ContractError, match="finite"):
            Metric(name="area_um2", value=bad, unit="um^2").validate()


def test_nan_inf_budget_rejected():
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ContractError, match="finite"):
            Budget(max_steps=5, max_wallclock_s=bad, max_tool_calls=5).validate()


def test_nan_inf_json_text_rejected():
    for token in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ContractError):
            StepResult.from_json(
                '{"schema_version": 1, "reward": ' + token + "}"
            )


def test_metric_requires_explicit_units():
    with pytest.raises(ContractError, match="unit"):
        Metric(name="area_um2", value=1.0, unit="").validate()
    payload = make_step_result().to_dict()
    del payload["metrics"][0]["unit"]
    with pytest.raises(ContractError, match="missing required fields"):
        StepResult.from_dict(payload)


# --- invalid budgets ---


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_steps": 0, "max_wallclock_s": 10.0, "max_tool_calls": 1},
        {"max_steps": -3, "max_wallclock_s": 10.0, "max_tool_calls": 1},
        {"max_steps": 5, "max_wallclock_s": 0.0, "max_tool_calls": 1},
        {"max_steps": 5, "max_wallclock_s": -1.0, "max_tool_calls": 1},
        {"max_steps": 5, "max_wallclock_s": 10.0, "max_tool_calls": 0},
        {"max_steps": 2, "max_wallclock_s": 10.0, "max_tool_calls": 5},
    ],
)
def test_invalid_budgets_rejected(kwargs):
    with pytest.raises(ContractError):
        Budget(**kwargs).validate()
    payload = make_task().to_dict()
    payload["budgets"] = {
        "max_steps": kwargs["max_steps"],
        "max_wallclock_s": kwargs["max_wallclock_s"],
        "max_tool_calls": kwargs["max_tool_calls"],
    }
    with pytest.raises(ContractError):
        TaskSpec.from_dict(payload)


def test_invalid_seed_and_ids_rejected():
    payload = make_task().to_dict()
    for bad_seed in (-1, 2**63, "42", 1.5, True):
        payload["seed"] = bad_seed
        with pytest.raises(ContractError):
            TaskSpec.from_dict(payload)
    payload = make_task().to_dict()
    payload["task_id"] = "Bad ID!"
    with pytest.raises(ContractError):
        TaskSpec.from_dict(payload)
    payload = make_task().to_dict()
    payload["task_version"] = "v1"
    with pytest.raises(ContractError):
        TaskSpec.from_dict(payload)


# --- conflicting options ---


def test_read_only_conflicts_with_write_actions():
    payload = make_task().to_dict()
    payload["read_only"] = True
    with pytest.raises(ContractError, match="read_only"):
        TaskSpec.from_dict(payload)


def test_read_only_allows_submit_but_no_edits_or_tool_runs():
    payload = make_task().to_dict()
    payload["allowed_actions"] = ["read_file", "submit"]
    payload["allowed_edit_paths"] = []
    payload["read_only"] = True
    task = TaskSpec.from_dict(payload)
    assert task.read_only is True
    task.validate_action(
        Action(schema_version=1, action_type="submit", params={}, step_index=0)
    )


def test_read_only_conflicts_with_edit_paths():
    payload = make_task().to_dict()
    payload["allowed_actions"] = ["read_file", "submit"]
    payload["read_only"] = True
    # edit paths still present -> conflict
    with pytest.raises(ContractError, match="read_only"):
        TaskSpec.from_dict(payload)


def test_grader_timeout_must_fit_wallclock():
    payload = make_task().to_dict()
    payload["grader"]["timeout_s"] = payload["budgets"]["max_wallclock_s"] + 1.0
    with pytest.raises(ContractError, match="must not exceed"):
        TaskSpec.from_dict(payload)


def test_terminal_step_status_requires_done():
    payload = make_step_result(status=StepStatus.TIMEOUT).to_dict()
    payload["done"] = False
    with pytest.raises(ContractError, match="requires done=true"):
        StepResult.from_dict(payload)


def test_grade_passed_must_match_status():
    payload = make_grade_result(status=GradeStatus.PASS).to_dict()
    payload["passed"] = False
    with pytest.raises(ContractError, match="conflicts with status"):
        GradeResult.from_dict(payload)
    payload = make_grade_result(status=GradeStatus.FAIL).to_dict()
    payload["passed"] = True
    with pytest.raises(ContractError, match="conflicts with status"):
        GradeResult.from_dict(payload)


def test_disallowed_action_rejected_by_task():
    task = make_task()
    action = Action(
        schema_version=1, action_type="noop", params={}, step_index=0
    )
    with pytest.raises(ContractError, match="not in allowed_actions"):
        task.validate_action(action)


# --- purity: invalid schemas have no side effects ---


def test_invalid_schemas_create_no_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(pathlib.Path(tmp_path).iterdir())
    bad = make_task().to_dict()
    del bad["task_id"]
    with pytest.raises(ContractError):
        TaskSpec.from_dict(bad)
    with pytest.raises(ContractError):
        StepResult.from_json("not json")
    assert set(pathlib.Path(tmp_path).iterdir()) == before
