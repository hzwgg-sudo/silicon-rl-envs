"""Shared contract primitives for silicon_env (M0-02).

Stdlib-only runtime: dataclasses + json + enum + math. No I/O, no subprocess,
no workspace creation -- validation here is pure and side-effect free.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

_TASK_ID_RE = re.compile(r"[a-z0-9][a-z0-9\-_]*")
_TASK_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


class ContractError(ValueError):
    """Raised when a contract dict/JSON payload fails strict validation."""


class StepStatus(str, Enum):
    """Per-step outcome. Each terminal condition is represented separately."""

    SUCCESS = "success"
    INVALID_SUBMISSION = "invalid_submission"
    TOOL_FAILURE = "tool_failure"
    TIMEOUT = "timeout"
    INFRA_ERROR = "infra_error"


class GradeStatus(str, Enum):
    """Episode-level grading outcome. Success splits into pass/fail."""

    PASS = "pass"
    FAIL = "fail"
    INVALID_SUBMISSION = "invalid_submission"
    TOOL_FAILURE = "tool_failure"
    TIMEOUT = "timeout"
    INFRA_ERROR = "infra_error"


TERMINAL_STEP_STATUSES = frozenset({StepStatus.TIMEOUT, StepStatus.INFRA_ERROR})
TERMINAL_GRADE_STATUSES = frozenset(
    {
        GradeStatus.PASS,
        GradeStatus.FAIL,
        GradeStatus.INVALID_SUBMISSION,
        GradeStatus.TOOL_FAILURE,
        GradeStatus.TIMEOUT,
        GradeStatus.INFRA_ERROR,
    }
)


def check_schema_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"schema_version must be an int, got {type(value).__name__}")
    if value not in SUPPORTED_SCHEMA_VERSIONS:
        raise ContractError(
            f"unsupported schema_version {value!r}; "
            f"supported versions: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    return value


def require_nonempty_str(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field_name} must be a non-empty string")
    return value


def require_task_id(value: Any) -> str:
    value = require_nonempty_str(value, field_name="task_id")
    if not _TASK_ID_RE.fullmatch(value):
        raise ContractError(
            f"task_id {value!r} must match {_TASK_ID_RE.pattern!r} "
            "(lowercase slug, digits, '-' and '_' only)"
        )
    return value


def require_task_version(value: Any) -> str:
    value = require_nonempty_str(value, field_name="task_version")
    if not _TASK_VERSION_RE.fullmatch(value):
        raise ContractError(
            f"task_version {value!r} must look like 'MAJOR.MINOR.PATCH' (e.g. '0.1.0')"
        )
    return value


def require_seed(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"seed must be an int, got {type(value).__name__}")
    if not 0 <= value < 2**63:
        raise ContractError(f"seed must satisfy 0 <= seed < 2**63, got {value!r}")
    return value


def require_finite_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field_name} must be a number, got {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"{field_name} must be finite, got {value!r}")
    return result


def require_positive_finite_float(value: Any, *, field_name: str) -> float:
    result = require_finite_float(value, field_name=field_name)
    if result <= 0:
        raise ContractError(f"{field_name} must be > 0, got {value!r}")
    return result


def require_nonnegative_finite_float(value: Any, *, field_name: str) -> float:
    result = require_finite_float(value, field_name=field_name)
    if result < 0:
        raise ContractError(f"{field_name} must be >= 0, got {value!r}")
    return result


def require_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{field_name} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise ContractError(f"{field_name} must be > 0, got {value!r}")
    return value


def require_nonnegative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{field_name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ContractError(f"{field_name} must be >= 0, got {value!r}")
    return value


def require_bool(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{field_name} must be a bool, got {type(value).__name__}")
    return value


def require_str_list(value: Any, *, field_name: str, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{field_name} must be a list of strings")
    for item in value:
        if not isinstance(item, str):
            raise ContractError(f"{field_name} must be a list of strings")
    if not allow_empty and not value:
        raise ContractError(f"{field_name} must not be empty")
    return list(value)


def require_toolchain_refs(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ContractError("toolchain_refs must be an object mapping tool name to version")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ContractError("toolchain_refs keys must be non-empty strings")
        if not isinstance(item, str) or not item.strip():
            raise ContractError(f"toolchain_refs[{key!r}] must be a non-empty version string")
        out[key] = item
    return out


def assert_json_value(value: Any, *, field_name: str = "value") -> Any:
    """Validate that a value is representable as strict JSON (finite floats only)."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{field_name} must be finite, got {value!r}")
        return value
    if isinstance(value, list):
        for i, item in enumerate(value):
            assert_json_value(item, field_name=f"{field_name}[{i}]")
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{field_name} keys must be strings")
            assert_json_value(item, field_name=f"{field_name}[{key!r}]")
        return value
    raise ContractError(
        f"{field_name} must be a JSON value "
        f"(null/bool/number/string/array/object), got {type(value).__name__}"
    )


def reject_unknown_fields(data: Mapping[str, Any], known: frozenset[str], *, what: str) -> None:
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ContractError(f"{what} has unknown fields: {unknown}")


def require_fields(data: Mapping[str, Any], required: frozenset[str], *, what: str) -> None:
    missing = sorted(set(required) - set(data))
    if missing:
        raise ContractError(f"{what} is missing required fields: {missing}")


def dumps_strict(payload: Any) -> str:
    """Serialize to JSON, rejecting NaN/Infinity (allow_nan=False)."""
    try:
        return json.dumps(payload, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ContractError(f"payload is not strict-JSON serializable: {exc}") from exc


def loads_strict(text: str) -> Any:
    """Parse JSON, rejecting NaN/Infinity/Object-duplicates via strict hooks."""

    def _reject_constant(token: str) -> Any:
        raise ContractError(f"non-finite JSON constant {token!r} is not allowed")

    try:
        return json.loads(text, parse_constant=_reject_constant)
    except ContractError:
        raise
    except (ValueError, TypeError) as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc


def loads_dict_strict(text: str, *, what: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ContractError(f"{what} JSON text must be a string")
    payload = loads_strict(text)
    if not isinstance(payload, dict):
        raise ContractError(f"{what} JSON must decode to an object")
    return payload


@dataclass(frozen=True)
class Metric:
    """A single scalar measurement with explicit units."""

    name: str
    value: float
    unit: str

    def validate(self) -> None:
        require_nonempty_str(self.name, field_name="metric.name")
        require_finite_float(self.value, field_name=f"metric[{self.name}].value")
        require_nonempty_str(self.unit, field_name=f"metric[{self.name}].unit")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {"name": self.name, "value": float(self.value), "unit": self.unit}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Metric:
        if not isinstance(data, Mapping):
            raise ContractError("metric must be an object")
        require_fields(data, frozenset({"name", "value", "unit"}), what="metric")
        reject_unknown_fields(data, frozenset({"name", "value", "unit"}), what="metric")
        metric = cls(
            name=require_nonempty_str(data["name"], field_name="metric.name"),
            value=require_finite_float(data["value"], field_name="metric.value"),
            unit=require_nonempty_str(data["unit"], field_name="metric.unit"),
        )
        metric.validate()
        return metric


@dataclass(frozen=True)
class Provenance:
    """Where a result came from; preserved verbatim through JSON round-trips."""

    task_id: str
    task_version: str
    seed: int
    toolchain_refs: Mapping[str, str] = field(default_factory=dict)
    grader_id: str = ""
    grader_version: str = ""

    def validate(self) -> None:
        require_task_id(self.task_id)
        require_task_version(self.task_version)
        require_seed(self.seed)
        require_toolchain_refs(dict(self.toolchain_refs))
        if self.grader_id != "" or self.grader_version != "":
            require_nonempty_str(self.grader_id, field_name="provenance.grader_id")
            require_nonempty_str(self.grader_version, field_name="provenance.grader_version")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "task_id": self.task_id,
            "task_version": self.task_version,
            "seed": self.seed,
            "toolchain_refs": dict(self.toolchain_refs),
            "grader_id": self.grader_id,
            "grader_version": self.grader_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Provenance:
        if not isinstance(data, Mapping):
            raise ContractError("provenance must be an object")
        known = frozenset(
            {
                "task_id",
                "task_version",
                "seed",
                "toolchain_refs",
                "grader_id",
                "grader_version",
            }
        )
        require_fields(
            data, frozenset({"task_id", "task_version", "seed"}), what="provenance"
        )
        reject_unknown_fields(data, known, what="provenance")
        provenance = cls(
            task_id=require_task_id(data["task_id"]),
            task_version=require_task_version(data["task_version"]),
            seed=require_seed(data["seed"]),
            toolchain_refs=require_toolchain_refs(data.get("toolchain_refs", {})),
            grader_id=data.get("grader_id", ""),
            grader_version=data.get("grader_version", ""),
        )
        provenance.validate()
        return provenance


@dataclass(frozen=True)
class Budget:
    """Episode resource limits. All values must be positive and finite."""

    max_steps: int
    max_wallclock_s: float
    max_tool_calls: int

    def validate(self) -> None:
        require_positive_int(self.max_steps, field_name="budgets.max_steps")
        require_positive_finite_float(self.max_wallclock_s, field_name="budgets.max_wallclock_s")
        require_positive_int(self.max_tool_calls, field_name="budgets.max_tool_calls")
        if self.max_tool_calls > self.max_steps:
            raise ContractError(
                "budgets.max_tool_calls "
                f"({self.max_tool_calls}) must not exceed budgets.max_steps "
                f"({self.max_steps}): at most one tool call per step"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "max_steps": self.max_steps,
            "max_wallclock_s": float(self.max_wallclock_s),
            "max_tool_calls": self.max_tool_calls,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Budget:
        if not isinstance(data, Mapping):
            raise ContractError("budgets must be an object")
        known = frozenset({"max_steps", "max_wallclock_s", "max_tool_calls"})
        require_fields(data, known, what="budgets")
        reject_unknown_fields(data, known, what="budgets")
        budget = cls(
            max_steps=require_positive_int(data["max_steps"], field_name="budgets.max_steps"),
            max_wallclock_s=require_positive_finite_float(
                data["max_wallclock_s"], field_name="budgets.max_wallclock_s"
            ),
            max_tool_calls=require_positive_int(
                data["max_tool_calls"], field_name="budgets.max_tool_calls"
            ),
        )
        budget.validate()
        return budget


@dataclass(frozen=True)
class GraderConfig:
    """How an episode is graded. Timeout must fit inside the task wallclock budget."""

    grader_id: str
    grader_version: str
    timeout_s: float
    required_metrics: tuple[str, ...] = ()

    def validate(self, *, max_wallclock_s: float | None = None) -> None:
        require_nonempty_str(self.grader_id, field_name="grader.grader_id")
        require_nonempty_str(self.grader_version, field_name="grader.grader_version")
        require_positive_finite_float(self.timeout_s, field_name="grader.timeout_s")
        require_str_list(list(self.required_metrics), field_name="grader.required_metrics")
        if len(set(self.required_metrics)) != len(self.required_metrics):
            raise ContractError("grader.required_metrics must not contain duplicates")
        if max_wallclock_s is not None and self.timeout_s > float(max_wallclock_s):
            raise ContractError(
                f"grader.timeout_s ({self.timeout_s}) must not exceed "
                f"budgets.max_wallclock_s ({max_wallclock_s})"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "grader_id": self.grader_id,
            "grader_version": self.grader_version,
            "timeout_s": float(self.timeout_s),
            "required_metrics": list(self.required_metrics),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GraderConfig:
        if not isinstance(data, Mapping):
            raise ContractError("grader must be an object")
        known = frozenset({"grader_id", "grader_version", "timeout_s", "required_metrics"})
        require_fields(
            data, frozenset({"grader_id", "grader_version", "timeout_s"}), what="grader"
        )
        reject_unknown_fields(data, known, what="grader")
        config = cls(
            grader_id=require_nonempty_str(data["grader_id"], field_name="grader.grader_id"),
            grader_version=require_nonempty_str(
                data["grader_version"], field_name="grader.grader_version"
            ),
            timeout_s=require_positive_finite_float(
                data["timeout_s"], field_name="grader.timeout_s"
            ),
            required_metrics=tuple(
                require_str_list(data.get("required_metrics", []),
                                field_name="grader.required_metrics")
            ),
        )
        config.validate()
        return config
