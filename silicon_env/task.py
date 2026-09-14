"""Task, action, and observation contracts (M0-02).

Pure dataclass validation: importing or validating these contracts never
creates workspaces, processes, or files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from silicon_env.types import (
    SUPPORTED_SCHEMA_VERSIONS,
    Budget,
    ContractError,
    GraderConfig,
    assert_json_value,
    check_schema_version,
    dumps_strict,
    loads_dict_strict,
    reject_unknown_fields,
    require_bool,
    require_fields,
    require_finite_float,
    require_nonempty_str,
    require_nonnegative_finite_float,
    require_nonnegative_int,
    require_seed,
    require_str_list,
    require_task_id,
    require_task_version,
    require_toolchain_refs,
)

TASK_SCHEMA_VERSION = 1
ACTION_SCHEMA_VERSION = 1
OBSERVATION_SCHEMA_VERSION = 1

KNOWN_ACTION_TYPES = frozenset({"read_file", "write_file", "run_tool", "submit", "noop"})
WRITE_ACTION_TYPES = frozenset({"write_file", "run_tool", "submit"})
# Submitting a result does not mutate the workspace, so read-only tasks may
# still allow "submit" (but not workspace edits or tool runs).
READ_ONLY_FORBIDDEN_ACTIONS = frozenset({"write_file", "run_tool"})


@dataclass(frozen=True)
class TaskSpec:
    """Versioned specification of a single RL task episode."""

    schema_version: int
    task_id: str
    task_version: str
    source_ref: str
    toolchain_refs: Mapping[str, str] = field(default_factory=dict)
    seed: int = 0
    allowed_actions: tuple[str, ...] = ()
    allowed_edit_paths: tuple[str, ...] = ()
    read_only: bool = False
    budgets: Budget = field(default_factory=lambda: Budget(10, 60.0, 10))
    grader: GraderConfig = field(
        default_factory=lambda: GraderConfig("rule-grader", "0.1.0", 10.0)
    )

    def validate(self) -> None:
        check_schema_version(self.schema_version)
        require_task_id(self.task_id)
        require_task_version(self.task_version)
        require_nonempty_str(self.source_ref, field_name="source_ref")
        require_toolchain_refs(dict(self.toolchain_refs))
        require_seed(self.seed)
        actions = require_str_list(
            list(self.allowed_actions), field_name="allowed_actions", allow_empty=False
        )
        unknown_actions = sorted(set(actions) - KNOWN_ACTION_TYPES)
        if unknown_actions:
            raise ContractError(f"allowed_actions contains unknown actions: {unknown_actions}")
        if len(set(actions)) != len(actions):
            raise ContractError("allowed_actions must not contain duplicates")
        edits = require_str_list(list(self.allowed_edit_paths), field_name="allowed_edit_paths")
        if len(set(edits)) != len(edits):
            raise ContractError("allowed_edit_paths must not contain duplicates")
        from silicon_env.workspace import WorkspaceError, _compile_allowlist

        try:
            _compile_allowlist(edits)
        except (WorkspaceError, ValueError) as exc:
            raise ContractError(f"invalid allowed_edit_paths: {exc}") from exc
        require_bool(self.read_only, field_name="read_only")
        if not isinstance(self.budgets, Budget):
            raise ContractError("budgets must be a Budget")
        if not isinstance(self.grader, GraderConfig):
            raise ContractError("grader must be a GraderConfig")
        self.budgets.validate()
        self.grader.validate(max_wallclock_s=float(self.budgets.max_wallclock_s))
        if self.read_only:
            writes = sorted(set(actions) & READ_ONLY_FORBIDDEN_ACTIONS)
            if writes:
                raise ContractError(
                    f"read_only task must not allow write actions: {writes}"
                )
            if edits:
                raise ContractError(
                    "read_only task must not declare allowed_edit_paths "
                    f"(got {edits})"
                )
        if "submit" not in actions:
            raise ContractError("allowed_actions must include 'submit'")

    def validate_action(self, action: Action) -> None:
        """Check that an action is permitted by this task (pure, no side effects)."""
        if not isinstance(action, Action):
            raise ContractError("action must be an Action")
        action.validate()
        if action.action_type not in set(self.allowed_actions):
            raise ContractError(
                f"action_type {action.action_type!r} is not in allowed_actions "
                f"{sorted(self.allowed_actions)}"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "source_ref": self.source_ref,
            "toolchain_refs": dict(self.toolchain_refs),
            "seed": self.seed,
            "allowed_actions": list(self.allowed_actions),
            "allowed_edit_paths": list(self.allowed_edit_paths),
            "read_only": self.read_only,
            "budgets": self.budgets.to_dict(),
            "grader": self.grader.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskSpec:
        if not isinstance(data, Mapping):
            raise ContractError("TaskSpec must be an object")
        known = frozenset(
            {
                "schema_version",
                "task_id",
                "task_version",
                "source_ref",
                "toolchain_refs",
                "seed",
                "allowed_actions",
                "allowed_edit_paths",
                "read_only",
                "budgets",
                "grader",
            }
        )
        require_fields(data, known, what="TaskSpec")
        reject_unknown_fields(data, known, what="TaskSpec")
        spec = cls(
            schema_version=check_schema_version(data["schema_version"]),
            task_id=require_task_id(data["task_id"]),
            task_version=require_task_version(data["task_version"]),
            source_ref=require_nonempty_str(data["source_ref"], field_name="source_ref"),
            toolchain_refs=require_toolchain_refs(data["toolchain_refs"]),
            seed=require_seed(data["seed"]),
            allowed_actions=tuple(
                require_str_list(
                    data["allowed_actions"], field_name="allowed_actions", allow_empty=False
                )
            ),
            allowed_edit_paths=tuple(
                require_str_list(data["allowed_edit_paths"], field_name="allowed_edit_paths")
            ),
            read_only=require_bool(data["read_only"], field_name="read_only"),
            budgets=Budget.from_dict(data["budgets"]),
            grader=GraderConfig.from_dict(data["grader"]),
        )
        spec.validate()
        return spec

    def to_json(self) -> str:
        return dumps_strict(self.to_dict())

    @classmethod
    def from_json(cls, text: str) -> TaskSpec:
        return cls.from_dict(loads_dict_strict(text, what="TaskSpec"))


@dataclass(frozen=True)
class Action:
    """An agent-issued action. Params must be strict JSON values."""

    schema_version: int
    action_type: str
    params: Mapping[str, Any] = field(default_factory=dict)
    step_index: int = 0

    def validate(self) -> None:
        check_schema_version(self.schema_version)
        require_nonempty_str(self.action_type, field_name="action_type")
        if self.action_type not in KNOWN_ACTION_TYPES:
            raise ContractError(
                f"unknown action_type {self.action_type!r}; "
                f"known types: {sorted(KNOWN_ACTION_TYPES)}"
            )
        require_nonnegative_int(self.step_index, field_name="step_index")
        if not isinstance(self.params, Mapping):
            raise ContractError("params must be an object")
        for key in self.params:
            if not isinstance(key, str):
                raise ContractError("params keys must be strings")
        assert_json_value(dict(self.params), field_name="params")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "action_type": self.action_type,
            "params": dict(self.params),
            "step_index": self.step_index,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Action:
        if not isinstance(data, Mapping):
            raise ContractError("Action must be an object")
        known = frozenset({"schema_version", "action_type", "params", "step_index"})
        require_fields(data, known, what="Action")
        reject_unknown_fields(data, known, what="Action")
        if not isinstance(data["params"], dict):
            raise ContractError("params must be an object")
        action = cls(
            schema_version=check_schema_version(data["schema_version"]),
            action_type=require_nonempty_str(data["action_type"], field_name="action_type"),
            params=dict(data["params"]),
            step_index=require_nonnegative_int(
                data["step_index"], field_name="step_index"
            ),
        )
        action.validate()
        return action

    def to_json(self) -> str:
        return dumps_strict(self.to_dict())

    @classmethod
    def from_json(cls, text: str) -> Action:
        return cls.from_dict(loads_dict_strict(text, what="Action"))


@dataclass(frozen=True)
class Observation:
    """Raw tool/process output observed after an action (no grading inside)."""

    schema_version: int
    step_index: int
    tool_name: str
    exit_code: int
    stdout_tail: str = ""
    stderr_tail: str = ""
    timed_out: bool = False
    duration_s: float = 0.0

    def validate(self) -> None:
        check_schema_version(self.schema_version)
        require_nonnegative_int(self.step_index, field_name="step_index")
        require_nonempty_str(self.tool_name, field_name="tool_name")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise ContractError("exit_code must be an int")
        if not isinstance(self.stdout_tail, str):
            raise ContractError("stdout_tail must be a string")
        if not isinstance(self.stderr_tail, str):
            raise ContractError("stderr_tail must be a string")
        require_bool(self.timed_out, field_name="timed_out")
        require_nonnegative_finite_float(self.duration_s, field_name="duration_s")
        require_finite_float(float(self.exit_code), field_name="exit_code")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "step_index": self.step_index,
            "tool_name": self.tool_name,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "timed_out": self.timed_out,
            "duration_s": float(self.duration_s),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Observation:
        if not isinstance(data, Mapping):
            raise ContractError("Observation must be an object")
        known = frozenset(
            {
                "schema_version",
                "step_index",
                "tool_name",
                "exit_code",
                "stdout_tail",
                "stderr_tail",
                "timed_out",
                "duration_s",
            }
        )
        require_fields(
            data,
            frozenset({"schema_version", "step_index", "tool_name", "exit_code"}),
            what="Observation",
        )
        reject_unknown_fields(data, known, what="Observation")
        obs = cls(
            schema_version=check_schema_version(data["schema_version"]),
            step_index=require_nonnegative_int(data["step_index"], field_name="step_index"),
            tool_name=require_nonempty_str(data["tool_name"], field_name="tool_name"),
            exit_code=data["exit_code"],
            stdout_tail=data.get("stdout_tail", ""),
            stderr_tail=data.get("stderr_tail", ""),
            timed_out=require_bool(data.get("timed_out", False), field_name="timed_out"),
            duration_s=require_nonnegative_finite_float(
                data.get("duration_s", 0.0), field_name="duration_s"
            ),
        )
        if not isinstance(data["exit_code"], int) or isinstance(data["exit_code"], bool):
            raise ContractError("exit_code must be an int")
        obs.validate()
        return obs

    def to_json(self) -> str:
        return dumps_strict(self.to_dict())

    @classmethod
    def from_json(cls, text: str) -> Observation:
        return cls.from_dict(loads_dict_strict(text, what="Observation"))


__all__ = [
    "SUPPORTED_SCHEMA_VERSIONS",
    "TASK_SCHEMA_VERSION",
    "ACTION_SCHEMA_VERSION",
    "OBSERVATION_SCHEMA_VERSION",
    "KNOWN_ACTION_TYPES",
    "WRITE_ACTION_TYPES",
    "READ_ONLY_FORBIDDEN_ACTIONS",
    "TaskSpec",
    "Action",
    "Observation",
]
