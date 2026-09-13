"""Grading result contracts (M0-02): StepResult and GradeResult.

Pure validation only -- no reward formula, no EDA adapter, no plugin
registry. Rewards/scores must be finite; every metric carries explicit units;
every result carries provenance so JSON round-trips lose nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from silicon_env.task import Observation
from silicon_env.types import (
    TERMINAL_STEP_STATUSES,
    ContractError,
    GradeStatus,
    Metric,
    Provenance,
    StepStatus,
    assert_json_value,
    check_schema_version,
    dumps_strict,
    loads_dict_strict,
    reject_unknown_fields,
    require_bool,
    require_fields,
    require_finite_float,
    require_nonnegative_int,
    require_task_id,
    require_task_version,
)

STEP_RESULT_SCHEMA_VERSION = 1
GRADE_RESULT_SCHEMA_VERSION = 1


def _coerce_step_status(value: Any) -> StepStatus:
    if isinstance(value, StepStatus):
        return value
    if not isinstance(value, str):
        raise ContractError(f"status must be a string, got {type(value).__name__}")
    try:
        return StepStatus(value)
    except ValueError as exc:
        raise ContractError(
            f"unknown step status {value!r}; expected one of "
            f"{sorted(s.value for s in StepStatus)}"
        ) from exc


def _coerce_grade_status(value: Any) -> GradeStatus:
    if isinstance(value, GradeStatus):
        return value
    if not isinstance(value, str):
        raise ContractError(f"status must be a string, got {type(value).__name__}")
    try:
        return GradeStatus(value)
    except ValueError as exc:
        raise ContractError(
            f"unknown grade status {value!r}; expected one of "
            f"{sorted(s.value for s in GradeStatus)}"
        ) from exc


def _metrics_from_list(data: Any, *, what: str) -> tuple[Metric, ...]:
    if not isinstance(data, list):
        raise ContractError(f"{what}.metrics must be a list")
    metrics = tuple(Metric.from_dict(item) for item in data)
    names = [m.name for m in metrics]
    if len(set(names)) != len(names):
        raise ContractError(f"{what}.metrics must not contain duplicate names: {names}")
    return metrics


@dataclass(frozen=True)
class StepResult:
    """Outcome of a single environment step."""

    schema_version: int
    step_index: int
    status: StepStatus
    reward: float
    done: bool
    observation: Observation
    metrics: tuple[Metric, ...] = ()
    provenance: Provenance | None = None
    message: str = ""

    def validate(self) -> None:
        check_schema_version(self.schema_version)
        require_nonnegative_int(self.step_index, field_name="step_index")
        if not isinstance(self.status, StepStatus):
            raise ContractError("status must be a StepStatus")
        require_finite_float(self.reward, field_name="reward")
        require_bool(self.done, field_name="done")
        if not isinstance(self.observation, Observation):
            raise ContractError("observation must be an Observation")
        self.observation.validate()
        if self.observation.step_index != self.step_index:
            raise ContractError(
                f"observation.step_index ({self.observation.step_index}) must match "
                f"step_index ({self.step_index})"
            )
        for metric in self.metrics:
            if not isinstance(metric, Metric):
                raise ContractError("metrics must be Metric objects")
            metric.validate()
        names = [m.name for m in self.metrics]
        if len(set(names)) != len(names):
            raise ContractError(f"metrics must not contain duplicate names: {names}")
        if self.provenance is not None:
            if not isinstance(self.provenance, Provenance):
                raise ContractError("provenance must be a Provenance")
            self.provenance.validate()
        if not isinstance(self.message, str):
            raise ContractError("message must be a string")
        if self.status in TERMINAL_STEP_STATUSES and not self.done:
            raise ContractError(
                f"status {self.status.value!r} is terminal and requires done=true"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "step_index": self.step_index,
            "status": self.status.value,
            "reward": float(self.reward),
            "done": self.done,
            "observation": self.observation.to_dict(),
            "metrics": [m.to_dict() for m in self.metrics],
            "message": self.message,
        }
        if self.provenance is not None:
            payload["provenance"] = self.provenance.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StepResult:
        if not isinstance(data, Mapping):
            raise ContractError("StepResult must be an object")
        known = frozenset(
            {
                "schema_version",
                "step_index",
                "status",
                "reward",
                "done",
                "observation",
                "metrics",
                "provenance",
                "message",
            }
        )
        require_fields(
            data,
            frozenset(
                {"schema_version", "step_index", "status", "reward", "done", "observation"}
            ),
            what="StepResult",
        )
        reject_unknown_fields(data, known, what="StepResult")
        if not isinstance(data["observation"], dict):
            raise ContractError("observation must be an object")
        provenance = None
        if "provenance" in data:
            if not isinstance(data["provenance"], dict):
                raise ContractError("provenance must be an object")
            provenance = Provenance.from_dict(data["provenance"])
        result = cls(
            schema_version=check_schema_version(data["schema_version"]),
            step_index=require_nonnegative_int(data["step_index"], field_name="step_index"),
            status=_coerce_step_status(data["status"]),
            reward=require_finite_float(data["reward"], field_name="reward"),
            done=require_bool(data["done"], field_name="done"),
            observation=Observation.from_dict(data["observation"]),
            metrics=_metrics_from_list(data.get("metrics", []), what="StepResult"),
            provenance=provenance,
            message=data.get("message", ""),
        )
        if not isinstance(result.message, str):
            raise ContractError("message must be a string")
        result.validate()
        return result

    def to_json(self) -> str:
        return dumps_strict(self.to_dict())

    @classmethod
    def from_json(cls, text: str) -> StepResult:
        return cls.from_dict(loads_dict_strict(text, what="StepResult"))


@dataclass(frozen=True)
class GradeResult:
    """Episode-level grading outcome with explicit pass/fail and provenance."""

    schema_version: int
    task_id: str
    task_version: str
    status: GradeStatus
    score: float
    passed: bool
    metrics: tuple[Metric, ...] = ()
    provenance: Provenance | None = None
    message: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        check_schema_version(self.schema_version)
        require_task_id(self.task_id)
        require_task_version(self.task_version)
        if not isinstance(self.status, GradeStatus):
            raise ContractError("status must be a GradeStatus")
        require_finite_float(self.score, field_name="score")
        require_bool(self.passed, field_name="passed")
        if self.passed != (self.status == GradeStatus.PASS):
            raise ContractError(
                f"passed={self.passed} conflicts with status {self.status.value!r}: "
                "passed must be true exactly when status is 'pass'"
            )
        for metric in self.metrics:
            if not isinstance(metric, Metric):
                raise ContractError("metrics must be Metric objects")
            metric.validate()
        names = [m.name for m in self.metrics]
        if len(set(names)) != len(names):
            raise ContractError(f"metrics must not contain duplicate names: {names}")
        if self.provenance is not None:
            if not isinstance(self.provenance, Provenance):
                raise ContractError("provenance must be a Provenance")
            self.provenance.validate()
            if self.provenance.task_id != self.task_id:
                raise ContractError("provenance.task_id must match task_id")
            if self.provenance.task_version != self.task_version:
                raise ContractError("provenance.task_version must match task_version")
        if not isinstance(self.message, str):
            raise ContractError("message must be a string")
        if not isinstance(self.details, Mapping):
            raise ContractError("details must be an object")
        assert_json_value(dict(self.details), field_name="details")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "status": self.status.value,
            "score": float(self.score),
            "passed": self.passed,
            "metrics": [m.to_dict() for m in self.metrics],
            "message": self.message,
            "details": dict(self.details),
        }
        if self.provenance is not None:
            payload["provenance"] = self.provenance.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GradeResult:
        if not isinstance(data, Mapping):
            raise ContractError("GradeResult must be an object")
        known = frozenset(
            {
                "schema_version",
                "task_id",
                "task_version",
                "status",
                "score",
                "passed",
                "metrics",
                "provenance",
                "message",
                "details",
            }
        )
        require_fields(
            data,
            frozenset(
                {"schema_version", "task_id", "task_version", "status", "score", "passed"}
            ),
            what="GradeResult",
        )
        reject_unknown_fields(data, known, what="GradeResult")
        if "details" in data and not isinstance(data["details"], dict):
            raise ContractError("details must be an object")
        provenance = None
        if "provenance" in data:
            if not isinstance(data["provenance"], dict):
                raise ContractError("provenance must be an object")
            provenance = Provenance.from_dict(data["provenance"])
        result = cls(
            schema_version=check_schema_version(data["schema_version"]),
            task_id=require_task_id(data["task_id"]),
            task_version=require_task_version(data["task_version"]),
            status=_coerce_grade_status(data["status"]),
            score=require_finite_float(data["score"], field_name="score"),
            passed=require_bool(data["passed"], field_name="passed"),
            metrics=_metrics_from_list(data.get("metrics", []), what="GradeResult"),
            provenance=provenance,
            message=data.get("message", ""),
            details=dict(data.get("details", {})),
        )
        if not isinstance(result.message, str):
            raise ContractError("message must be a string")
        result.validate()
        return result

    def to_json(self) -> str:
        return dumps_strict(self.to_dict())

    @classmethod
    def from_json(cls, text: str) -> GradeResult:
        return cls.from_dict(loads_dict_strict(text, what="GradeResult"))


__all__ = [
    "STEP_RESULT_SCHEMA_VERSION",
    "GRADE_RESULT_SCHEMA_VERSION",
    "StepResult",
    "GradeResult",
]
