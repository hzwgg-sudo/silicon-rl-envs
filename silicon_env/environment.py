"""Synchronous environment protocol (M0-06).

Small deterministic lifecycle shared by all M0 adapters:

- ``reset(task, seed)`` creates a fresh episode workspace + budget tracker
  and returns the initial :class:`Observation`.
- ``step(action)`` dispatches one validated action and returns a
  :class:`StepResult` (never raises for invalid *content* -- those become
  ``invalid_submission`` results that still consume one step).
- ``submit()`` grades the current workspace and terminates the episode.
- ``close()`` releases the episode workspace and is idempotent.

Misuse (step/submit before reset, after termination, or after close)
raises :class:`EnvironmentError`. Budget exhaustion never launches new
work: the over-budget step returns ``StepStatus.TIMEOUT`` with
``done=True`` and terminates the episode.

Stdlib-only, Python >= 3.10. No Gym, no async, no LLM, no EDA tools.
"""

from __future__ import annotations

import os
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Mapping

from silicon_env.budget import BudgetExhausted, BudgetTracker
from silicon_env.grader import GradeResult, StepResult
from silicon_env.runner import RunResult, ToolRunner
from silicon_env.task import Action, Observation, TaskSpec
from silicon_env.types import (
    ContractError,
    GradeStatus,
    Provenance,
    StepStatus,
)

ClockFn = Callable[[], float]

#: Cap applied to every observation tail for determinism + agent safety.
MAX_OBS_TAIL_CHARS = 4000


class EnvironmentError(ValueError):
    """Raised for environment misuse (not reset, terminated, or closed)."""


def sanitize_tail(text: Any, *, limit: int = MAX_OBS_TAIL_CHARS) -> str:
    """Coerce to ``str`` and keep at most the last ``limit`` chars."""
    if not isinstance(text, str):
        text = str(text)
    if limit <= 0:
        return ""
    if len(text) > limit:
        return text[-limit:]
    return text


def _require_seed_value(seed: Any) -> int:
    from silicon_env.types import require_seed

    return require_seed(seed)


class BaseEnvironment(ABC):
    """Synchronous episode lifecycle over workspaces + budgets + tools.

    :param template_dir: directory whose *contents* seed every episode.
    :param work_root: parent directory for per-episode workspace dirs and
        per-step tool logs (created if missing).
    :param runner: named-tool executor for ``run_tool`` actions. Defaults
        to an empty :class:`ToolRunner` (any ``run_tool`` then fails as a
        tool failure until tools are registered).
    :param clock: monotonic seconds source for budgets (tests inject fakes).
    :param obs_tail_limit: per-field cap for observation tails.
    :param enable_trace: when True (default), each episode records a
        versioned JSONL trace + manifest under ``work_root/runs/<run_id>``.
        Tracing never alters stepping semantics; I/O failures are ignored.
    """

    def __init__(
        self,
        *,
        template_dir: str | os.PathLike[str],
        work_root: str | os.PathLike[str],
        runner: ToolRunner | None = None,
        clock: ClockFn | None = None,
        obs_tail_limit: int = MAX_OBS_TAIL_CHARS,
        enable_trace: bool = True,
    ) -> None:
        template = Path(template_dir)
        if not template.is_dir():
            raise EnvironmentError(f"template_dir must be an existing directory: {template}")
        root = Path(work_root)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise EnvironmentError(f"cannot create work_root {root}: {exc}") from exc
        if not root.is_dir():
            raise EnvironmentError(f"work_root must be a directory: {root}")
        if runner is not None and not isinstance(runner, ToolRunner):
            raise EnvironmentError("runner must be a ToolRunner or None")
        if obs_tail_limit is not None and (
            isinstance(obs_tail_limit, bool)
            or not isinstance(obs_tail_limit, int)
            or obs_tail_limit <= 0
        ):
            raise EnvironmentError("obs_tail_limit must be a positive int")
        self._template_dir = template
        self._work_root = root
        self._runner = runner if runner is not None else ToolRunner(tools={})
        self._clock: ClockFn = clock if clock is not None else time.monotonic
        if not callable(self._clock):
            raise EnvironmentError("clock must be callable")
        self._obs_tail_limit = obs_tail_limit or MAX_OBS_TAIL_CHARS

        self._task: TaskSpec | None = None
        self._seed: int = 0
        self._workspace = None  # EpisodeWorkspace | None
        self._manager = None  # WorkspaceManager | None
        self._tracker: BudgetTracker | None = None
        self._step_count: int = 0
        self._done: bool = False
        self._closed: bool = False
        self._has_reset: bool = False
        self._enable_trace = bool(enable_trace)
        self._recorder = None  # TraceRecorder | None
        self._run_dir: Path | None = None
        self._trace_finalized: bool = True
        self._pending_submit_grade = None  # GradeResult | None
        self._last_submit_grade = None  # GradeResult | None (persists for recall)

    # -- state views ----------------------------------------------------

    @property
    def task(self) -> TaskSpec | None:
        return self._task

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def workspace(self):  # EpisodeWorkspace | None
        return self._workspace

    @property
    def budget_tracker(self) -> BudgetTracker | None:
        return self._tracker

    @property
    def done(self) -> bool:
        return self._done

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active(self) -> bool:
        return self._has_reset and not self._done and not self._closed

    @property
    def run_dir(self) -> Path | None:
        """Per-episode trace run directory (None when tracing is disabled)."""
        return self._run_dir

    @property
    def trace_recorder(self):  # TraceRecorder | None
        return self._recorder

    @property
    def last_submit_grade(self):  # GradeResult | None
        """Grade from the most recent submit (step action or submit call).

        Persists after termination so callers driving ``step`` with an
        explicit ``submit`` action can recall the exact grade without
        re-grading. ``None`` when no submit has happened this episode;
        cleared on ``reset``.
        """
        return self._last_submit_grade

    @property
    def trace_path(self) -> Path | None:
        if self._recorder is None:
            return None
        return self._recorder.trace_path

    @property
    def manifest_path(self) -> Path | None:
        if self._recorder is None:
            return None
        return self._recorder.manifest_path

    # -- lifecycle ------------------------------------------------------

    def reset(self, task: TaskSpec, seed: int | None = None) -> Observation:
        """Start a fresh episode, discarding any previous workspace."""
        prev_recorder = self._recorder
        prev_open = (
            self._enable_trace
            and prev_recorder is not None
            and not self._trace_finalized
            and self._has_reset
        )
        prev_snapshot = self._trace_snapshot() if prev_open else {}
        obs = self._reset_inner(task, seed)
        if prev_open:
            assert prev_recorder is not None
            try:
                prev_recorder.finalize_incomplete("superseded by reset", prev_snapshot)
            except Exception:
                pass
            self._trace_finalized = True
        self._trace_start()
        return obs

    def step(self, action: Action) -> StepResult:
        """Dispatch one action; invalid content becomes ``invalid_submission``."""
        result = self._step_inner(action)
        self._trace_after_step(action, result)
        pending, self._pending_submit_grade = self._pending_submit_grade, None
        if pending is not None:
            self._trace_after_submit(pending)
        return result

    def submit(self) -> GradeResult:
        """Grade the current workspace; terminates the episode."""
        result = self._submit_inner()
        self._trace_after_submit(result)
        return result

    def close(self) -> None:
        """Release the episode workspace. Idempotent."""
        self._finalize_trace_if_open(reason="closed without submit")
        if self._closed:
            return
        self._cleanup_workspace()
        self._closed = True
        self._done = True

    def _reset_inner(self, task: TaskSpec, seed: int | None = None) -> Observation:
        """Start a fresh episode, discarding any previous workspace."""
        if self._closed:
            raise EnvironmentError("environment is closed; cannot reset")
        if not isinstance(task, TaskSpec):
            raise ContractError("task must be a TaskSpec")
        task.validate()
        effective_seed = task.seed if seed is None else _require_seed_value(seed)

        # Discard the previous episode workspace before starting a new one.
        self._cleanup_workspace()

        from silicon_env.workspace import WorkspaceManager

        manager = WorkspaceManager(
            root_dir=self._work_root / "episodes",
            template_dir=self._template_dir,
            allowed_edit_paths=tuple(task.allowed_edit_paths),
        )
        workspace = manager.reset()
        tracker = BudgetTracker(task.budgets, clock=self._clock)

        self._task = task
        self._seed = effective_seed
        self._manager = manager
        self._workspace = workspace
        self._tracker = tracker
        self._step_count = 0
        self._done = False
        self._has_reset = True
        self._pending_submit_grade = None
        self._last_submit_grade = None

        text = self._initial_text()
        return Observation(
            schema_version=1,
            step_index=0,
            tool_name="reset",
            exit_code=0,
            stdout_tail=sanitize_tail(text, limit=self._obs_tail_limit),
            stderr_tail="",
            timed_out=False,
            duration_s=0.0,
        )

    def _step_inner(self, action: Action) -> StepResult:
        """Dispatch one action (traced by the :meth:`step` wrapper)."""
        self._require_active("step")
        assert self._task is not None and self._tracker is not None
        if not isinstance(action, Action):
            raise ContractError("action must be an Action")

        task = self._task
        tracker = self._tracker

        # Validate untrusted parameters only after checking the action quota.
        try:
            tracker.reserve_action()
        except BudgetExhausted as exc:
            return self._timeout_result(str(exc))
        try:
            task.validate_action(action)
            self._validate_params(action)
        except ContractError as exc:
            tracker.consume_action(valid=False)
            return self._invalid_result(action, str(exc))

        handler = {
            "read_file": self._do_read_file,
            "write_file": self._do_write_file,
            "run_tool": self._do_run_tool,
            "noop": self._do_noop,
            "submit": self._do_submit_action,
        }.get(action.action_type)
        if handler is None:  # pragma: no cover - validate_action guards this
            tracker.consume_action(valid=False)
            return self._invalid_result(action, f"unsupported action_type {action.action_type!r}")
        return handler(action)

    def _submit_inner(self) -> GradeResult:
        """Grade the current workspace (traced by the :meth:`submit` wrapper)."""
        self._require_active("submit")
        assert self._task is not None
        assert self._tracker is not None
        try:
            self._tracker.consume_action()
        except BudgetExhausted as exc:
            self._done = True
            return GradeResult(
                schema_version=1, task_id=self._task.task_id,
                task_version=self._task.task_version, status=GradeStatus.TIMEOUT,
                score=0.0, passed=False, provenance=self._provenance(), message=str(exc),
            )
        result = self._grade()
        result.validate()
        self._done = True
        self._last_submit_grade = result
        return result

    # -- trace hooks ----------------------------------------------------

    def _trace_start(self) -> None:
        """Create the per-episode recorder and log the reset event."""
        if not self._enable_trace:
            return
        try:
            from silicon_env.trace import TraceRecorder

            run_id = f"run_{uuid.uuid4().hex[:12]}"
            run_dir = self._work_root / "runs" / run_id
            backend = getattr(self._runner, "provenance", None)
            recorder = TraceRecorder(
                run_dir, task=self._task, seed=self._seed, run_id=run_id,
                runner_provenance=backend() if callable(backend) else {"runner": "local"},
            )
            snapshot = self._tracker.snapshot() if self._tracker is not None else {}
            immutable = ""
            if self._workspace is not None:
                try:
                    immutable = self._workspace.immutable_hash
                except Exception:
                    immutable = ""
            initial = self._initial_text()
            from silicon_env.task import Observation as _Obs

            obs = _Obs(
                schema_version=1,
                step_index=0,
                tool_name="reset",
                exit_code=0,
                stdout_tail=sanitize_tail(initial, limit=self._obs_tail_limit),
                stderr_tail="",
                timed_out=False,
                duration_s=0.0,
            )
            recorder.record_reset(obs, snapshot, immutable_hash=immutable)
            self._recorder = recorder
            self._run_dir = run_dir
            self._trace_finalized = False
            self._pending_submit_grade = None
        except Exception:
            self._recorder = None
            self._run_dir = None
            self._trace_finalized = True

    def _trace_snapshot(self) -> dict[str, Any]:
        try:
            if self._tracker is not None:
                return self._tracker.snapshot()
        except Exception:
            pass
        return {}

    def _trace_after_step(self, action: Action, result: StepResult) -> None:
        recorder = self._recorder
        if not self._enable_trace or recorder is None or self._trace_finalized:
            return
        try:
            log_dir = self._work_root / "logs" / self._workspace.root.name / (
                f"step_{result.step_index}"
            )
            artifact_files: list[Path] = []
            for name in ("stdout.log", "stderr.log"):
                candidate = log_dir / name
                try:
                    if candidate.is_file() and not candidate.is_symlink():
                        artifact_files.append(candidate)
                except OSError:
                    continue
            recorder.record_step(
                action,
                result,
                self._trace_snapshot(),
                artifact_files=tuple(artifact_files),
            )
        except Exception:
            pass

    def _trace_after_submit(self, grade: GradeResult) -> None:
        recorder = self._recorder
        if not self._enable_trace or recorder is None or self._trace_finalized:
            return
        try:
            recorder.record_submit(grade, self._trace_snapshot())
            recorder.finalize(
                status=grade.status.value,
                passed=bool(grade.passed),
                budget_snapshot=self._trace_snapshot(),
                message=grade.message,
            )
            self._trace_finalized = True
        except Exception:
            pass

    def _finalize_trace_if_open(self, *, reason: str) -> None:
        recorder = self._recorder
        if not self._enable_trace or recorder is None or self._trace_finalized:
            return
        if not self._has_reset:
            return
        try:
            recorder.finalize_incomplete(reason, self._trace_snapshot())
        except Exception:
            pass
        finally:
            self._trace_finalized = True

    # -- hooks ----------------------------------------------------------

    def _initial_text(self) -> str:
        """Human-readable reset summary shown in the initial observation."""
        assert self._workspace is not None
        try:
            entries = sorted(self._workspace.list_dir("."))
        except Exception:
            entries = []
        return "workspace ready: " + (", ".join(entries) if entries else "(empty)")

    @abstractmethod
    def _grade(self) -> GradeResult:
        """Pure grading of the current workspace (must not mutate budgets)."""

    # -- action handlers ------------------------------------------------

    def _do_read_file(self, action: Action) -> StepResult:
        assert self._tracker is not None and self._workspace is not None
        reason = self._tracker.consume_action(valid=True)
        rel = action.params["path"]
        try:
            content = self._workspace.read_text(rel)
        except Exception as exc:
            return self._failure_result(action, str(exc), exit_code=1, exhausted_reason=reason)
        return self._success_result(
            action,
            stdout=content,
            message=f"read {rel}",
            exhausted_reason=reason,
        )

    def _do_write_file(self, action: Action) -> StepResult:
        assert self._tracker is not None and self._workspace is not None
        reason = self._tracker.consume_action(valid=True)
        rel = action.params["path"]
        content = action.params["content"]
        try:
            self._workspace.write_text(rel, content)
        except Exception as exc:
            text = str(exc)
            if "protected" in text or "allowed_edit_paths" in text:
                return self._invalid_result(action, text, exhausted_reason=reason)
            return self._failure_result(action, text, exit_code=1, exhausted_reason=reason)
        return self._success_result(
            action, stdout=f"wrote {rel}", message=f"wrote {rel}", exhausted_reason=reason
        )

    def _do_noop(self, action: Action) -> StepResult:
        assert self._tracker is not None
        reason = self._tracker.consume_action(valid=True)
        return self._success_result(action, stdout="", message="noop", exhausted_reason=reason)

    def _do_run_tool(self, action: Action) -> StepResult:
        assert self._tracker is not None and self._workspace is not None
        params = action.params
        tool = params["tool"]
        args = tuple(params.get("args", ()))
        requested = self._requested_timeout(action)
        try:
            allowance_timeout = self._tracker.effective_timeout(requested)
        except BudgetExhausted as exc:
            return self._timeout_result(str(exc) or "budget exhausted")
        log_dir = self._work_root / "logs" / self._workspace.root.name / (
            f"step_{self._step_count + 1}"
        )
        # Charge before execution: elapsed wall time must not prevent charging a timeout.
        try:
            self._tracker.consume_tool_call()
        except BudgetExhausted as exc:
            return self._timeout_result(str(exc))
        try:
            run: RunResult = self._runner.run(
                tool,
                list(args),
                cwd=self._workspace.root,
                log_dir=log_dir,
                timeout_s=allowance_timeout,
            )
        except Exception as exc:
            # Runner misuse (unknown tool, bad args): charge the slot, report
            # as invalid so the agent learns the tool surface.
            return self._invalid_result(action, str(exc))
        reason = self._tracker.exhausted_reason()
        stdout = self._read_log_tail(run.stdout_path)
        stderr = self._read_log_tail(run.stderr_path)
        if run.status == StepStatus.SUCCESS:
            return self._success_result(
                action,
                stdout=stdout,
                stderr=stderr,
                tool_name=run.tool_name,
                duration_s=run.duration_s,
                message=f"ran {run.tool_name}",
                exhausted_reason=reason,
            )
        if run.status == StepStatus.TIMEOUT:
            obs = self._obs(
                action,
                tool_name=run.tool_name,
                exit_code=run.exit_code if run.exit_code is not None else 124,
                stdout=stdout,
                stderr=stderr or "timed out",
                timed_out=True,
                duration_s=run.duration_s,
            )
            return self._finish(action, obs, StepStatus.TIMEOUT, done=True)
        if run.status == StepStatus.INFRA_ERROR:
            obs = self._obs(
                action,
                tool_name=run.tool_name,
                exit_code=run.exit_code if run.exit_code is not None else 127,
                stdout=stdout,
                stderr=stderr or run.error,
                duration_s=run.duration_s,
            )
            return self._finish(action, obs, StepStatus.INFRA_ERROR, done=True)
        # TOOL_FAILURE: agent-visible failure, episode continues. A budget
        # that trips exactly on this charge surfaces on the *next*
        # reserve gate as TIMEOUT (see _success_result).
        obs = self._obs(
            action,
            tool_name=run.tool_name,
            exit_code=run.exit_code if run.exit_code is not None else 1,
            stdout=stdout,
            stderr=stderr,
            duration_s=run.duration_s,
        )
        return self._finish(action, obs, StepStatus.TOOL_FAILURE, done=False)

    def _do_submit_action(self, action: Action) -> StepResult:
        assert self._tracker is not None
        self._tracker.consume_action(valid=True)
        grade = self._grade()
        grade.validate()
        self._done = True
        self._pending_submit_grade = grade
        self._last_submit_grade = grade
        passed = grade.status == GradeStatus.PASS
        obs = self._obs(
            action,
            tool_name="submit",
            exit_code=0 if passed else 1,
            stdout=grade.message,
        )
        status = {
            GradeStatus.PASS: StepStatus.SUCCESS,
            GradeStatus.FAIL: StepStatus.INVALID_SUBMISSION,
            GradeStatus.INVALID_SUBMISSION: StepStatus.INVALID_SUBMISSION,
            GradeStatus.TOOL_FAILURE: StepStatus.TOOL_FAILURE,
            GradeStatus.TIMEOUT: StepStatus.TIMEOUT,
            GradeStatus.INFRA_ERROR: StepStatus.INFRA_ERROR,
        }[grade.status]
        reward = 1.0 if passed else 0.0
        self._step_count += 1
        return StepResult(
            schema_version=1,
            step_index=self._step_count,
            status=status,
            reward=reward,
            done=True,
            observation=Observation(
                schema_version=1,
                step_index=self._step_count,
                tool_name=obs.tool_name,
                exit_code=obs.exit_code,
                stdout_tail=obs.stdout_tail,
                stderr_tail=obs.stderr_tail,
                timed_out=status == StepStatus.TIMEOUT,
                duration_s=0.0,
            ),
            metrics=tuple(grade.metrics),
            provenance=self._provenance(),
            message=grade.message,
        )

    # -- result helpers -------------------------------------------------

    def _require_active(self, op: str) -> None:
        if self._closed:
            raise EnvironmentError(f"cannot {op}: environment is closed")
        if not self._has_reset or self._task is None or self._workspace is None:
            raise EnvironmentError(f"cannot {op}: call reset() first")
        if self._done:
            raise EnvironmentError(f"cannot {op}: episode has terminated; call reset()")

    def _cleanup_workspace(self) -> None:
        ws, mgr = self._workspace, self._manager
        self._workspace = None
        self._manager = None
        if ws is not None and mgr is not None:
            try:
                mgr.cleanup(ws)
            except Exception:
                pass

    def _provenance(self) -> Provenance:
        assert self._task is not None
        return Provenance(
            task_id=self._task.task_id,
            task_version=self._task.task_version,
            seed=self._seed,
            toolchain_refs=dict(self._task.toolchain_refs),
            grader_id=self._task.grader.grader_id,
            grader_version=self._task.grader.grader_version,
        )

    def _obs(
        self,
        action: Action,
        *,
        tool_name: str,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        duration_s: float = 0.0,
    ) -> Observation:
        # step_index filled in by _finish (next index).
        return Observation(
            schema_version=1,
            step_index=self._step_count + 1,
            tool_name=tool_name,
            exit_code=exit_code,
            stdout_tail=sanitize_tail(stdout, limit=self._obs_tail_limit),
            stderr_tail=sanitize_tail(stderr, limit=self._obs_tail_limit),
            timed_out=timed_out,
            duration_s=float(duration_s),
        )

    def _finish(
        self,
        action: Action,
        obs: Observation,
        status: StepStatus,
        *,
        reward: float | None = None,
        done: bool = False,
        message: str = "",
    ) -> StepResult:
        _ = action
        self._step_count += 1
        terminal = status in (StepStatus.TIMEOUT, StepStatus.INFRA_ERROR)
        done = bool(done or terminal)
        if done:
            self._done = True
        if reward is None:
            reward = 0.0
        fixed_obs = Observation(
            schema_version=1,
            step_index=self._step_count,
            tool_name=obs.tool_name,
            exit_code=obs.exit_code,
            stdout_tail=obs.stdout_tail,
            stderr_tail=obs.stderr_tail,
            timed_out=obs.timed_out,
            duration_s=obs.duration_s,
        )
        return StepResult(
            schema_version=1,
            step_index=self._step_count,
            status=status,
            reward=float(reward),
            done=done,
            observation=fixed_obs,
            metrics=(),
            provenance=self._provenance(),
            message=message,
        )

    def _success_result(
        self,
        action: Action,
        *,
        stdout: str = "",
        stderr: str = "",
        tool_name: str | None = None,
        duration_s: float = 0.0,
        message: str = "",
        exhausted_reason: str | None = None,
    ) -> StepResult:
        # Post-charge exhaustion does not rewrite this step: the allowance
        # was valid at dispatch, so the true outcome is reported with
        # done=False and the *next* dispatch hits the reserve gate and
        # returns TIMEOUT done=True (budget truncation).
        _ = exhausted_reason
        name = tool_name or action.action_type
        obs = self._obs(
            action,
            tool_name=name,
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration_s,
        )
        return self._finish(action, obs, StepStatus.SUCCESS, done=False, message=message)

    def _failure_result(
        self,
        action: Action,
        message: str,
        *,
        exit_code: int = 1,
        exhausted_reason: str | None = None,
    ) -> StepResult:
        _ = exhausted_reason
        obs = self._obs(action, tool_name=action.action_type, exit_code=exit_code, stderr=message)
        return self._finish(action, obs, StepStatus.TOOL_FAILURE, message=message)

    def _invalid_result(
        self, action: Action, message: str, *, exhausted_reason: str | None = None
    ) -> StepResult:
        _ = exhausted_reason
        obs = self._obs(action, tool_name=action.action_type, exit_code=2, stderr=message)
        return self._finish(action, obs, StepStatus.INVALID_SUBMISSION, message=message)

    def _timeout_result(self, message: str) -> StepResult:
        self._step_count += 1
        self._done = True
        assert self._task is not None
        obs = Observation(
            schema_version=1,
            step_index=self._step_count,
            tool_name="budget",
            exit_code=124,
            stdout_tail="",
            stderr_tail=sanitize_tail(message, limit=self._obs_tail_limit),
            timed_out=True,
            duration_s=0.0,
        )
        return StepResult(
            schema_version=1,
            step_index=self._step_count,
            status=StepStatus.TIMEOUT,
            reward=0.0,
            done=True,
            observation=obs,
            metrics=(),
            provenance=self._provenance(),
            message=message,
        )

    def _terminal_timeout_after(self, obs: Observation, action: Action, message: str) -> StepResult:
        _ = action
        return self._finish(
            obs=obs, action=action, status=StepStatus.TIMEOUT, done=True, message=message
        )

    # -- validation -----------------------------------------------------

    def _validate_params(self, action: Action) -> None:
        params = action.params
        if not isinstance(params, Mapping):
            raise ContractError("params must be an object")
        if action.action_type == "read_file":
            path = params.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ContractError("read_file requires params.path (non-empty string)")
            if set(params) - {"path"}:
                raise ContractError("read_file accepts only params.path")
        elif action.action_type == "write_file":
            path = params.get("path")
            content = params.get("content")
            if not isinstance(path, str) or not path.strip():
                raise ContractError("write_file requires params.path (non-empty string)")
            if not isinstance(content, str):
                raise ContractError("write_file requires params.content (string)")
            if set(params) - {"path", "content"}:
                raise ContractError("write_file accepts only params.path/content")
        elif action.action_type == "run_tool":
            tool = params.get("tool")
            if not isinstance(tool, str) or not tool.strip():
                raise ContractError("run_tool requires params.tool (non-empty string)")
            args = params.get("args", [])
            if not isinstance(args, (list, tuple)) or any(not isinstance(a, str) for a in args):
                raise ContractError("run_tool params.args must be a list of strings")
            if "timeout_s" in params:
                timeout = params["timeout_s"]
                if (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or not (float(timeout) > 0)
                    or float(timeout) != float(timeout)
                    or float(timeout) in (float("inf"), float("-inf"))
                ):
                    raise ContractError("run_tool params.timeout_s must be > 0")
            if set(params) - {"tool", "args", "timeout_s"}:
                raise ContractError("run_tool accepts only params.tool/args/timeout_s")
        elif action.action_type == "noop":
            if params:
                raise ContractError("noop accepts no params")
        elif action.action_type == "submit":
            if set(params) - set():
                raise ContractError("submit accepts no params")

    def _requested_timeout(self, action: Action) -> float | None:
        if action.action_type != "run_tool":
            return None
        value = action.params.get("timeout_s")
        return None if value is None else float(value)

    @staticmethod
    def _read_log_tail(path: Path, *, limit: int = MAX_OBS_TAIL_CHARS) -> str:
        try:
            data = Path(path).read_bytes()
        except OSError:
            return ""
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return ""
        return sanitize_tail(text, limit=limit)


# Backwards/forwards-compatible alias: the issue calls it
# "BaseEnvironment/Environment protocol".
Environment = BaseEnvironment

__all__ = [
    "MAX_OBS_TAIL_CHARS",
    "BaseEnvironment",
    "Environment",
    "EnvironmentError",
    "sanitize_tail",
]
