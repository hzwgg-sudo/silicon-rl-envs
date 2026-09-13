"""Deterministic toy text-edit environment (M0-06).

A single-file edit task used to exercise the full reset/step/submit
lifecycle without EDA tools, network, or randomness:

- template file ``note.txt`` starts at :data:`TOY_INITIAL_TEXT`;
- the agent must rewrite it to :data:`TOY_TARGET_TEXT` via
  ``read_file`` / ``write_file`` actions, then call :meth:`submit`;
- the pure grader :func:`grade_candidate` compares the candidate bytes
  to the expected target (exact match passes).

The ``seed`` is recorded in provenance but drives no randomness, so the
same seed + action sequence always yields identical semantic
observations and grades.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from silicon_env.environment import BaseEnvironment
from silicon_env.grader import GradeResult
from silicon_env.runner import ToolRunner
from silicon_env.task import TaskSpec
from silicon_env.types import Budget, ContractError, GraderConfig, GradeStatus, Metric

TOY_TASK_ID = "toy-text-edit"
TOY_TASK_VERSION = "0.1.0"
TOY_FILE = "note.txt"
TOY_INITIAL_TEXT = "hello\n"
TOY_TARGET_TEXT = "hello world\n"
TOY_GRADER_ID = "toy-grader"
TOY_GRADER_VERSION = "0.1.0"

ClockFn = Callable[[], float]


def ensure_toy_template(template_dir: str | os.PathLike[str]) -> Path:
    """Create (or refresh) the toy template dir with the initial file."""
    template = Path(template_dir)
    template.mkdir(parents=True, exist_ok=True)
    (template / TOY_FILE).write_text(TOY_INITIAL_TEXT, encoding="utf-8")
    return template


def make_toy_task(
    *,
    seed: int = 0,
    max_steps: int = 10,
    max_wallclock_s: float = 60.0,
    target_text: str = TOY_TARGET_TEXT,
) -> TaskSpec:
    """Build the canonical toy :class:`TaskSpec`.

    ``target_text`` is informational only (the expected answer lives in
    the environment); it is recorded nowhere in the spec so grading
    cannot be bypassed by reading the task.
    """
    _ = target_text
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
        raise ContractError("max_steps must be a positive int")
    task = TaskSpec(
        schema_version=1,
        task_id=TOY_TASK_ID,
        task_version=TOY_TASK_VERSION,
        source_ref="toy@v0.1.0",
        toolchain_refs={},
        seed=seed,
        allowed_actions=("read_file", "write_file", "submit"),
        allowed_edit_paths=(TOY_FILE,),
        read_only=False,
        budgets=Budget(
            max_steps=max_steps,
            max_wallclock_s=float(max_wallclock_s),
            max_tool_calls=max_steps,
        ),
        grader=GraderConfig(
            grader_id=TOY_GRADER_ID,
            grader_version=TOY_GRADER_VERSION,
            timeout_s=min(5.0, float(max_wallclock_s)),
        ),
    )
    task.validate()
    return task


def grade_candidate(candidate: str, *, expected: str = TOY_TARGET_TEXT) -> tuple[bool, str]:
    """Pure grader: exact string match passes (no normalization)."""
    if not isinstance(candidate, str) or not isinstance(expected, str):
        raise ContractError("candidate and expected must be strings")
    if candidate == expected:
        return True, "candidate matches expected target"
    return False, (
        f"candidate ({len(candidate)} chars) does not match expected ({len(expected)} chars)"
    )


class ToyEnvironment(BaseEnvironment):
    """Toy text-edit adapter over :class:`BaseEnvironment`.

    :param work_root: parent dir for episode workspaces + logs.
    :param template_dir: optional pre-built template dir. When omitted, a
        ``_toy_template`` dir under ``work_root`` is (re)created with
        :data:`TOY_INITIAL_TEXT`.
    :param target_text: expected file content for a passing grade.
    :param initial_text: template content written when this env owns the
        template dir (ignored when ``template_dir`` is given).
    """

    def __init__(
        self,
        work_root: str | os.PathLike[str],
        *,
        template_dir: str | os.PathLike[str] | None = None,
        target_text: str = TOY_TARGET_TEXT,
        initial_text: str = TOY_INITIAL_TEXT,
        clock: ClockFn | None = None,
    ) -> None:
        if not isinstance(target_text, str):
            raise ContractError("target_text must be a string")
        if not isinstance(initial_text, str):
            raise ContractError("initial_text must be a string")
        root = Path(work_root)
        if template_dir is None:
            template = root / "_toy_template"
            template.mkdir(parents=True, exist_ok=True)
            (template / TOY_FILE).write_text(initial_text, encoding="utf-8")
        else:
            template = Path(template_dir)
        super().__init__(
            template_dir=template,
            work_root=root,
            runner=ToolRunner(tools={}),
            clock=clock,
        )
        self._target_text = target_text

    @property
    def target_text(self) -> str:
        return self._target_text

    def _initial_text(self) -> str:
        assert self.workspace is not None
        try:
            current = self.workspace.read_text(TOY_FILE)
        except Exception:
            current = ""
        return f"edit {TOY_FILE} to the target text; current content: {current!r}"

    def _grade(self) -> GradeResult:
        assert self.task is not None
        assert self.workspace is not None
        try:
            candidate = self.workspace.read_text(TOY_FILE)
        except Exception as exc:
            return GradeResult(
                schema_version=1,
                task_id=self.task.task_id,
                task_version=self.task.task_version,
                status=GradeStatus.TOOL_FAILURE,
                score=0.0,
                passed=False,
                metrics=(),
                provenance=self._provenance(),
                message=f"cannot read {TOY_FILE}: {exc}",
                details={"file": TOY_FILE},
            )
        passed, message = grade_candidate(candidate, expected=self._target_text)
        status = GradeStatus.PASS if passed else GradeStatus.FAIL
        metric = Metric(name="exact_match", value=1.0 if passed else 0.0, unit="bool")
        return GradeResult(
            schema_version=1,
            task_id=self.task.task_id,
            task_version=self.task.task_version,
            status=status,
            score=1.0 if passed else 0.0,
            passed=passed,
            metrics=(metric,),
            provenance=self._provenance(),
            message=message,
            details={"file": TOY_FILE, "candidate_chars": len(candidate)},
        )


# Alias for tests that expect a shorter name.
ToyEnv = ToyEnvironment

__all__ = [
    "TOY_FILE",
    "TOY_GRADER_ID",
    "TOY_GRADER_VERSION",
    "TOY_INITIAL_TEXT",
    "TOY_TARGET_TEXT",
    "TOY_TASK_ID",
    "TOY_TASK_VERSION",
    "ToyEnv",
    "ToyEnvironment",
    "ensure_toy_template",
    "grade_candidate",
    "make_toy_task",
]
