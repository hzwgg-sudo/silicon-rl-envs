"""GCD OpenROAD interactive environment (M1-08).

Exposes the pinned GCD physical-design task as the same synchronous
agent lifecycle as the toy task (:class:`ToyEnvironment`):

- ``reset(task, seed)`` creates a fresh episode workspace seeded with the
  stock ``candidate.json`` (any prior candidate override is discarded).
- ``read_file`` performs bounded reads through the workspace API.
- ``write_file`` to ``candidate.json`` validates the content as a
  candidate config *before* writing: malformed JSON, unknown keys,
  non-numeric values, out-of-range values, and Tcl/Make injection
  strings are rejected immediately as ``invalid_submission`` without
  mutating the workspace.
- ``run_tool`` with tool ``"openroad-flow"`` runs the current workspace
  candidate through the fixed flow adapter (:mod:`flow`) into a fresh
  scratch dir, exposing sanitized log tails, the stage reached, and the
  remaining budget. Unknown tools are rejected; flow ``TIMEOUT`` /
  ``INFRA_ERROR`` terminate the episode (as in the base class).
- ``submit()`` grades through the independent clean-room evaluator
  (:mod:`evaluator`) on a copies-only staging dir in a fresh eval root:
  agent-visible logs/metrics are never scored, only the trusted re-run.

Budgets are enforced by :class:`BaseEnvironment`: an exhausted budget
terminates with ``TIMEOUT`` without launching a new flow.

Stdlib-only, Python >= 3.10. Default unit tests inject fakes; the one
real pinned run is opt-in (see ``tests/test_openroad_environment.py``).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from silicon_env.environment import BaseEnvironment, sanitize_tail
from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad import evaluator as gcd_evaluator
from silicon_env.environments.openroad import flow as gcd_flow
from silicon_env.grader import GradeResult
from silicon_env.runner import DEFAULT_ENV_ALLOWLIST, ToolRunner
from silicon_env.types import ContractError, GradeStatus, Provenance

ClockFn = Callable[[], float]

#: Callable overriding the fixed flow adapter in tests. Signature:
#: ``(candidate, *, orfs_checkout, scratch_dir, runner, seed, timeout_s)``.
RunFlowFn = Callable[..., Any]

#: Callable fully replacing :func:`evaluator.evaluate_submission` in tests.
#: Signature: ``(submission_dir, *, orfs_checkout, eval_root, runner,
#: baseline_record, seed, timeout_s) -> EvalResult``.
EvaluateFn = Callable[..., Any]


def stock_candidate_text() -> str:
    """Return the stock ``candidate.json`` file content (with newline)."""
    return gcd.dumps_candidate_json(gcd.stock_candidate_config()) + "\n"


def ensure_gcd_template(template_dir: str | os.PathLike[str]) -> Path:
    """Create (or refresh) a template dir holding the stock candidate."""
    template = Path(template_dir)
    template.mkdir(parents=True, exist_ok=True)
    (template / gcd.CANDIDATE_RELPATH).write_text(stock_candidate_text(), encoding="utf-8")
    return template


def _normalize_rel(rel: Any) -> str:
    if isinstance(rel, os.PathLike):
        rel = os.fspath(rel)
    if not isinstance(rel, str):
        return ""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    return "/".join(parts)


def default_flow_runner() -> ToolRunner:
    """Build a real backend runner with the fixed flow tool registered."""
    return ToolRunner(
        tools={gcd_flow.FLOW_TOOL_NAME: ["make"]},
        env_allowlist=[*DEFAULT_ENV_ALLOWLIST, *gcd_flow.FLOW_ENV_KEYS],
    )


class GcdEnvironment(BaseEnvironment):
    """GCD adapter over :class:`BaseEnvironment`.

    :param work_root: parent dir for episode workspaces, flow scratches,
        submission staging dirs, eval roots, and logs.
    :param template_dir: optional pre-built template dir. When omitted, a
        ``_gcd_template`` dir under ``work_root`` is (re)created with the
        stock candidate.
    :param runner: backend runner used by the flow adapter (a
        :class:`ToolRunner` with ``openroad-flow`` registered, or a fake
        stub exposing ``run`` in tests). Defaults to
        :func:`default_flow_runner`.
    :param evaluator_runner: backend runner used by the submit-time
        evaluator re-run. Defaults to ``runner``.
    :param orfs_checkout: trusted pinned ORFS checkout (read by the flow
        and verified by the evaluator). May be empty in tests that inject
        ``run_flow_fn``/``evaluate_fn``; real runs fail closed as
        infrastructure errors instead of succeeding.
    :param baseline_record: approved baseline mapping for the trusted
        grader. TBD/unverified records fail closed at grade time (never
        at construction).
    :param run_flow_fn: injectable flow callable for tests (defaults to
        :func:`flow.run_gcd_flow`).
    :param parse_fn: injectable trusted-parser callable forwarded to the
        evaluator.
    :param grade_fn: injectable trusted-grader callable forwarded to the
        evaluator.
    :param evaluate_fn: injectable full-submission evaluator replacing
        :func:`evaluator.evaluate_submission` (grade mapping tests).
    """

    def __init__(
        self,
        work_root: str | os.PathLike[str],
        *,
        template_dir: str | os.PathLike[str] | None = None,
        runner: Any | None = None,
        evaluator_runner: Any | None = None,
        orfs_checkout: str | os.PathLike[str],
        baseline_record: Mapping[str, Any],
        clock: ClockFn | None = None,
        enable_trace: bool = True,
        run_flow_fn: RunFlowFn | None = None,
        parse_fn: Callable[[Any], Any] | None = None,
        grade_fn: Callable[..., Any] | None = None,
        evaluate_fn: EvaluateFn | None = None,
    ) -> None:
        if not isinstance(baseline_record, Mapping):
            raise ContractError("baseline_record must be a mapping")
        root = Path(work_root)
        if template_dir is None:
            template = ensure_gcd_template(root / "_gcd_template")
        else:
            template = Path(template_dir)
        flow_runner: Any = runner if runner is not None else default_flow_runner()
        if isinstance(flow_runner, ToolRunner):
            base_runner: ToolRunner = flow_runner
        else:
            if not callable(getattr(flow_runner, "run", None)):
                raise ContractError("runner must expose a callable run(tool, args, ...) method")
            base_runner = ToolRunner(tools={})
        super().__init__(
            template_dir=template,
            work_root=root,
            runner=base_runner,
            clock=clock,
            enable_trace=enable_trace,
        )
        self._flow_runner: Any = flow_runner
        self._evaluator_runner: Any = (
            evaluator_runner if evaluator_runner is not None else flow_runner
        )
        if self._evaluator_runner is None or not callable(
            getattr(self._evaluator_runner, "run", None)
        ):
            raise ContractError(
                "evaluator_runner must expose a callable run(tool, args, ...) method"
            )
        self._orfs_checkout = (
            os.fspath(orfs_checkout)
            if isinstance(orfs_checkout, os.PathLike)
            else orfs_checkout
        )
        self._baseline_record: dict[str, Any] = dict(baseline_record)
        self._run_flow_fn: RunFlowFn = run_flow_fn or gcd_flow.run_gcd_flow
        self._parse_fn = parse_fn
        self._grade_fn = grade_fn
        self._evaluate_fn = evaluate_fn
        if not callable(self._run_flow_fn):
            raise ContractError("run_flow_fn must be callable")

    # -- views ----------------------------------------------------------

    @property
    def orfs_checkout(self) -> Any:
        return self._orfs_checkout

    @property
    def baseline_record(self) -> dict[str, Any]:
        return dict(self._baseline_record)

    @property
    def flow_runner(self) -> Any:
        return self._flow_runner

    # -- lifecycle text -------------------------------------------------

    def _initial_text(self) -> str:
        assert self.workspace is not None
        try:
            current = self.workspace.read_text(gcd.CANDIDATE_RELPATH).strip()
        except Exception:
            current = ""
        return (
            f"edit {gcd.CANDIDATE_RELPATH} "
            f"(keys: {', '.join(gcd.ALLOWED_KEYS)}), then run tool "
            f"{gcd_flow.FLOW_TOOL_NAME!r} to endpoint {gcd.GCD_ENDPOINT!r} "
            f"and submit; current candidate: {current}"
        )

    # -- validated config writes ----------------------------------------

    def _do_write_file(self, action: Any) -> Any:
        assert self._tracker is not None
        rel = action.params.get("path")
        if _normalize_rel(rel) == gcd.CANDIDATE_RELPATH:
            content = action.params.get("content")
            try:
                from silicon_env.types import loads_dict_strict

                payload = loads_dict_strict(content, what="candidate config")
                gcd.validate_candidate_config(payload)
            except ContractError as exc:
                reason = self._tracker.consume_action(valid=True)
                return self._invalid_result(
                    action,
                    f"invalid candidate config for {gcd.CANDIDATE_RELPATH}: {exc}",
                    exhausted_reason=reason,
                )
        return super()._do_write_file(action)

    # -- fixed flow command ----------------------------------------------

    def _do_run_tool(self, action: Any) -> Any:
        assert self._tracker is not None
        assert self._workspace is not None
        tool = action.params.get("tool")
        requested = self._requested_timeout(action)
        try:
            allowance_timeout = self._tracker.effective_timeout(requested)
        except Exception as exc:
            from silicon_env.budget import BudgetExhausted

            if isinstance(exc, BudgetExhausted):
                return self._timeout_result(str(exc) or "budget exhausted")
            raise
        try:
            self._tracker.consume_tool_call()
        except Exception as exc:
            from silicon_env.budget import BudgetExhausted

            if isinstance(exc, BudgetExhausted):
                return self._timeout_result(str(exc))
            raise
        if tool != gcd_flow.FLOW_TOOL_NAME:
            return self._invalid_result(
                action,
                f"unknown tool {tool!r}; this environment only runs "
                f"{gcd_flow.FLOW_TOOL_NAME!r}",
            )
        try:
            text = self._workspace.read_text(gcd.CANDIDATE_RELPATH)
        except Exception as exc:
            return self._failure_result(
                action, f"cannot read {gcd.CANDIDATE_RELPATH}: {exc}"
            )
        try:
            from silicon_env.types import loads_dict_strict

            candidate = gcd.candidate_with_defaults(
                loads_dict_strict(text, what="candidate config")
            )
        except ContractError as exc:
            return self._invalid_result(
                action, f"workspace candidate is invalid (fix it first): {exc}"
            )
        try:
            scratch = self._fresh_scratch_dir()
        except OSError as exc:
            return self._infra_result(action, f"cannot create flow scratch: {exc}")
        try:
            flow_result = self._run_flow_fn(
                dict(candidate),
                orfs_checkout=self._orfs_checkout,
                scratch_dir=str(scratch),
                runner=self._flow_runner,
                seed=self._seed,
                timeout_s=allowance_timeout,
            )
        except Exception as exc:
            return self._infra_result(
                action, f"flow runner failed before completing: {exc}"
            )
        return self._map_flow_result(action, flow_result)

    def _fresh_scratch_dir(self) -> Path:
        assert self._workspace is not None
        parent = self._work_root / "flow_scratch"
        parent.mkdir(parents=True, exist_ok=True)
        scratch = parent / f"{self._workspace.root.name}_step_{self._step_count + 1}"
        scratch.mkdir(parents=False, exist_ok=False)
        return scratch

    def _map_flow_result(self, action: Any, flow_result: Any) -> Any:
        from silicon_env.types import StepStatus

        status = getattr(flow_result, "status", None)
        stage = str(getattr(flow_result, "stage_reached", "unknown"))
        message = str(getattr(flow_result, "message", "") or "")
        summary = self._flow_summary_text(flow_result)
        budget = self._budget_text()
        stdout = f"{summary}\n{budget}"
        if status == StepStatus.SUCCESS:
            return self._success_result(
                action,
                stdout=stdout,
                tool_name=gcd_flow.FLOW_TOOL_NAME,
                message=f"flow reached stage {stage!r}: {message}",
            )
        if status == StepStatus.TOOL_FAILURE:
            obs = self._obs(
                action,
                tool_name=gcd_flow.FLOW_TOOL_NAME,
                exit_code=1,
                stdout=stdout,
                stderr=message,
            )
            return self._finish(action, obs, StepStatus.TOOL_FAILURE, done=False)
        if status == StepStatus.TIMEOUT:
            obs = self._obs(
                action,
                tool_name=gcd_flow.FLOW_TOOL_NAME,
                exit_code=124,
                stdout=stdout,
                stderr=message or "flow timed out",
                timed_out=True,
            )
            return self._finish(action, obs, StepStatus.TIMEOUT, done=True)
        if status == StepStatus.INFRA_ERROR:
            return self._infra_result(action, message or "flow infrastructure failure")
        return self._infra_result(
            action, f"flow returned an unrecognized status {status!r}: {message}"
        )

    def _infra_result(self, action: Any, message: str) -> Any:
        from silicon_env.types import StepStatus

        obs = self._obs(
            action,
            tool_name=gcd_flow.FLOW_TOOL_NAME,
            exit_code=127,
            stderr=message,
        )
        return self._finish(action, obs, StepStatus.INFRA_ERROR, done=True)

    def _flow_summary_text(self, flow_result: Any) -> str:
        try:
            report = gcd_flow.summarize(flow_result)
        except Exception:
            return f"flow status={getattr(flow_result, 'status', '?')}"
        # NOTE: duration_s is deliberately excluded from the agent-visible
        # summary. It is clock-derived (diagnostic-only) and would make
        # otherwise-identical episodes hash differently; it remains
        # available in flow provenance and runner logs.
        lines = [
            f"flow status={report.get('status')} stage={report.get('stage_reached')} "
            f"ok={report.get('ok')} endpoint={report.get('endpoint')}",
            f"candidate={report.get('candidate_sha256')}",
        ]
        missing = report.get("missing_artifacts") or []
        stale = report.get("stale_artifacts") or []
        if missing:
            lines.append(f"missing={missing}")
        if stale:
            lines.append(f"stale={stale}")
        message = str(getattr(flow_result, "message", "") or "")
        if message:
            lines.append(sanitize_tail(message, limit=1000))
        tail = self._runner_log_tails(flow_result)
        if tail:
            lines.append(tail)
        return sanitize_tail("\n".join(lines), limit=self._obs_tail_limit)

    def _runner_log_tails(self, flow_result: Any) -> str:
        provenance = getattr(flow_result, "provenance", None)
        if not isinstance(provenance, Mapping):
            return ""
        parts: list[str] = []
        for key in ("runner_stdout_path", "runner_stderr_path"):
            raw = provenance.get(key)
            if not raw:
                continue
            tail = self._read_log_tail(Path(str(raw)), limit=1000)
            if tail:
                parts.append(f"{key}:\n{tail}")
        return "\n".join(parts)

    def _budget_text(self) -> str:
        # NOTE: remaining wallclock is deliberately excluded here. It is
        # clock-derived (diagnostic-only; still recorded in trace budget
        # snapshots) and would make otherwise-identical episodes hash
        # differently. Steps + tool calls are deterministic counters.
        assert self._tracker is not None
        try:
            snapshot = self._tracker.snapshot()
        except Exception:
            return "budget: unknown"
        return (
            f"remaining steps={snapshot.get('remaining_steps')} "
            f"tool_calls={snapshot.get('remaining_tool_calls')}"
        )

    # -- independent submit -----------------------------------------------

    def _grade(self) -> GradeResult:
        assert self.task is not None
        assert self.workspace is not None
        try:
            text = self.workspace.read_text(gcd.CANDIDATE_RELPATH)
        except Exception as exc:
            return self._grade_tool_failure(f"cannot read {gcd.CANDIDATE_RELPATH}: {exc}")
        staging: Path | None = None
        try:
            staging = self._stage_submission(text)
            eval_result = self._run_evaluator(staging)
        except GradeResultException as exc:
            return exc.grade
        except Exception as exc:
            return self._grade_infra_error(
                "evaluator-error", f"{type(exc).__name__}: {exc}"
            )
        finally:
            if staging is not None:
                self._cleanup_staging(staging)
        try:
            grade = gcd_evaluator.to_grade_result(
                eval_result, baseline_record=self._baseline_record
            )
        except Exception as exc:
            return self._grade_infra_error(
                "grade-convert-error", f"{type(exc).__name__}: {exc}"
            )
        if grade.task_id != self.task.task_id or (
            grade.task_version != self.task.task_version
        ):
            return self._grade_infra_error(
                "grade-identity-mismatch",
                f"evaluator grade identity {grade.task_id}@{grade.task_version} "
                f"!= task {self.task.task_id}@{self.task.task_version}",
            )
        return grade

    def _stage_submission(self, candidate_text: str) -> Path:
        parent = self._work_root / "submissions"
        parent.mkdir(parents=True, exist_ok=True)
        staging = parent / f"sub_{uuid.uuid4().hex[:12]}"
        staging.mkdir(parents=False, exist_ok=False)
        data = candidate_text.encode("utf-8")
        if len(data) > gcd_evaluator.MAX_SUBMISSION_BYTES:
            raise GradeResultException(
                self._grade_invalid(f"candidate exceeds submission cap: {len(data)} bytes")
            )
        (staging / gcd.CANDIDATE_RELPATH).write_bytes(data)
        return staging

    @staticmethod
    def _cleanup_staging(staging: Path) -> None:
        try:
            (staging / gcd.CANDIDATE_RELPATH).unlink(missing_ok=True)
            staging.rmdir()
        except OSError:
            pass

    def _run_evaluator(self, staging: Path) -> Any:
        assert self.task is not None
        eval_root = self._work_root / "eval_runs"
        timeout_s = float(self.task.grader.timeout_s)
        if self._evaluate_fn is not None:
            return self._evaluate_fn(
                str(staging),
                orfs_checkout=self._orfs_checkout,
                eval_root=str(eval_root),
                runner=self._evaluator_runner,
                baseline_record=dict(self._baseline_record),
                seed=self._seed,
                timeout_s=timeout_s,
            )
        kwargs: dict[str, Any] = {
            "orfs_checkout": self._orfs_checkout,
            "eval_root": str(eval_root),
            "runner": self._evaluator_runner,
            "baseline_record": dict(self._baseline_record),
            "seed": self._seed,
            "timeout_s": timeout_s,
            "run_flow_fn": self._run_flow_fn,
        }
        if self._parse_fn is not None:
            kwargs["parse_fn"] = self._parse_fn
        if self._grade_fn is not None:
            kwargs["grade_fn"] = self._grade_fn
        try:
            return gcd_evaluator.evaluate_submission(str(staging), **kwargs)
        except gcd_evaluator.EvaluatorError as exc:
            # Evaluator misuse (bad roots/checkout wiring): infrastructure.
            raise GradeResultException(
                self._grade_infra_error("evaluator-misuse", str(exc))
            ) from exc

    def _provenance_for_grade(self) -> Provenance:
        return self._provenance()

    def _grade_invalid(self, message: str) -> GradeResult:
        assert self.task is not None
        return GradeResult(
            schema_version=1,
            task_id=self.task.task_id,
            task_version=self.task.task_version,
            status=GradeStatus.INVALID_SUBMISSION,
            score=0.0,
            passed=False,
            metrics=(),
            provenance=self._provenance_for_grade(),
            message=message,
        )

    def _grade_tool_failure(self, message: str) -> GradeResult:
        assert self.task is not None
        return GradeResult(
            schema_version=1,
            task_id=self.task.task_id,
            task_version=self.task.task_version,
            status=GradeStatus.TOOL_FAILURE,
            score=0.0,
            passed=False,
            metrics=(),
            provenance=self._provenance_for_grade(),
            message=message,
        )

    def _grade_infra_error(self, reason: str, detail: str) -> GradeResult:
        assert self.task is not None
        message = f"infrastructure failure ({reason})"
        if detail:
            message += f": {detail}"
        return GradeResult(
            schema_version=1,
            task_id=self.task.task_id,
            task_version=self.task.task_version,
            status=GradeStatus.INFRA_ERROR,
            score=0.0,
            passed=False,
            metrics=(),
            provenance=self._provenance_for_grade(),
            message=message,
        )


class GradeResultException(Exception):
    """Internal control flow: carry a prebuilt :class:`GradeResult`."""

    def __init__(self, grade: GradeResult) -> None:
        super().__init__(grade.message)
        self.grade = grade


# Alias for tests that expect a shorter name.
GcdEnv = GcdEnvironment

__all__ = [
    "EvaluateFn",
    "GcdEnv",
    "GcdEnvironment",
    "GradeResultException",
    "RunFlowFn",
    "default_flow_runner",
    "ensure_gcd_template",
    "stock_candidate_text",
]
