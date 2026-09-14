"""Independent clean-room GCD grading workspace (M1-07).

Re-runs a GCD submission in a fresh evaluator scratch that the agent
never writes to. Agent-visible logs, reports, and metrics are untrusted
for final scoring: the evaluator imports *only* the allowed submitted
file (``candidate.json``), restores the trusted pinned sources and
constraints, re-runs the fixed flow through trusted code
(:mod:`flow`, :mod:`metrics`, :mod:`grader`), and recomputes the grade.
Evaluator provenance (run id, ORFS commit, image ref, seed, candidate
hash, tool versions) is retained separately from agent observations.

Submission policy (fail closed, before any flow starts):

- The submission directory must contain exactly one regular file,
  ``candidate.json``. Anything else -- protected RTL/SDC names
  (``gcd.v``, ``constraint.sdc``, ...), extra reports/metrics, or any
  other unauthorized file -- rejects the submission as invalid
  (reward ``0.0``).
- Symlinks are never followed: any symlink inside the submission
  directory (including a symlinked ``candidate.json``) rejects the
  submission.
- Oversized ``candidate.json`` files (over :data:`MAX_SUBMISSION_BYTES`)
  are rejected.
- Agent-supplied reports/metrics are never read: only ``candidate.json``
  bytes are copied into the fresh evaluator scratch. A forged report
  sitting next to the submission (or anywhere outside it) therefore
  cannot change the recomputed grade; a forged file *inside* the
  submission directory is an unauthorized file and rejects it -- either
  way the forged numbers never enter scoring.

Infrastructure outcomes (missing trusted sources, flow
``TIMEOUT``/``INFRA_ERROR``, runner crashes) carry no trainable reward:
they grade via :func:`grader.grade_infra_error` (``reward == 0.0``,
``excluded_from_aggregates=True``) with evaluator provenance retained.

Stdlib-only, Python >= 3.10. No EDA tools, Docker, network, or API keys
are touched here; execution goes through the caller-supplied runner.
Default unit tests inject fake ``run_flow_fn``/``parse_fn`` callables.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from silicon_env.environments.openroad import baseline as bl
from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad import flow as gcd_flow
from silicon_env.environments.openroad import grader as gcd_grader
from silicon_env.environments.openroad import metrics as gcd_metrics
from silicon_env.environments.openroad.preflight import load_toolchain_lock
from silicon_env.grader import GradeResult
from silicon_env.types import (
    ContractError,
    GradeStatus,
    Metric,
    Provenance,
    loads_dict_strict,
    require_seed,
)

#: Evaluator identity recorded in every provenance dict.
EVALUATOR_ID = "gcd-clean-evaluator"

#: Evaluator implementation version recorded in every provenance dict.
EVALUATOR_VERSION = "0.1.0"

#: The only file a submission directory may contain.
ALLOWED_SUBMISSION_FILES = (gcd.CANDIDATE_RELPATH,)

#: Explicit cap on ``candidate.json`` size (fail closed on anything larger).
MAX_SUBMISSION_BYTES = 65_536

#: Basenames that must never be submittable (protected RTL/SDC/config/lib
#: assets). Any submission entry matching one of these is rejected with a
#: ``protected-asset`` reason before any flow starts.
_PROTECTED_BASENAMES = frozenset(
    {Path(asset.rstrip("/")).name for asset in gcd.PROTECTED_ASSETS if asset.rstrip("/")}
) | frozenset(gcd.PROTECTED_ASSETS)


class SubmissionError(ValueError):
    """A submission failed validation (invalid candidate, not caller misuse)."""


class EvaluatorError(ValueError):
    """Evaluator misuse (bad roots, bad baseline type, bad wiring)."""


@dataclass(frozen=True)
class EvalResult:
    """Outcome of one independent evaluator re-run."""

    status: GradeStatus
    grade: gcd_grader.GcdGrade
    metrics: gcd_metrics.GcdMetrics | None
    candidate: dict[str, float] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()
    message: str = ""
    eval_scratch: str = ""

    @property
    def ok(self) -> bool:
        """True only when the recomputed grade is valid."""
        return self.grade.valid

    @property
    def reward(self) -> float:
        """Alias for the recomputed grade reward."""
        return self.grade.reward

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "grade": self.grade.to_dict(),
            "metrics": self.metrics.to_dict() if self.metrics is not None else None,
            "candidate": dict(self.candidate),
            "provenance": dict(self.provenance),
            "reason_codes": list(self.reason_codes),
            "message": self.message,
            "eval_scratch": self.eval_scratch,
        }


def _resolve_dir(value: Any, *, field_name: str) -> Path:
    raw = os.fspath(value) if isinstance(value, os.PathLike) else value
    if not isinstance(raw, str) or not raw.strip():
        raise EvaluatorError(f"{field_name} must be a non-empty path")
    return Path(raw)


def _is_nested_or_equal(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def validate_submission_dir(submission_dir: str | os.PathLike[str]) -> dict[str, float]:
    """Validate a submission directory; return the full trusted candidate.

    The directory must contain exactly one regular file,
    ``candidate.json``, holding a strict-JSON object that passes
    :func:`config.validate_candidate_config`. Missing candidates,
    unauthorized/extra files, protected-asset names, symlinks, oversized
    files, malformed JSON, and out-of-range values raise
    :class:`SubmissionError` (never a trainable score). Missing keys are
    filled with stock defaults so the evaluator always runs a complete
    candidate.

    Agent-supplied reports/metrics are never read here.
    """
    root = _resolve_dir(submission_dir, field_name="submission_dir").resolve()
    if root.is_symlink() or not root.is_dir():
        raise SubmissionError(f"submission_dir must be an existing directory: {root}")
    try:
        entries = sorted(os.listdir(root))
    except OSError as exc:
        raise SubmissionError(f"cannot list submission_dir {root}: {exc}") from exc

    if gcd.CANDIDATE_RELPATH not in entries:
        raise SubmissionError(
            "submission is missing the required file "
            f"{gcd.CANDIDATE_RELPATH!r} (got {entries!r}): invalid-submission"
        )
    for entry in entries:
        if entry != gcd.CANDIDATE_RELPATH:
            if entry in _PROTECTED_BASENAMES or Path(entry).name in _PROTECTED_BASENAMES:
                raise SubmissionError(
                    f"submission contains protected asset {entry!r}: "
                    "protected RTL/SDC/sources cannot enter the evaluator "
                    "(protected-asset)"
                )
            raise SubmissionError(
                f"submission contains unauthorized file {entry!r}: only "
                f"{list(ALLOWED_SUBMISSION_FILES)!r} may be submitted "
                "(unauthorized-file)"
            )
        target = root / entry
        if os.path.islink(target):
            raise SubmissionError(
                f"submission file {entry!r} is a symlink: symlinks are never "
                "followed (symlink-rejected)"
            )

    candidate_path = root / gcd.CANDIDATE_RELPATH
    try:
        size = candidate_path.stat().st_size
    except OSError as exc:
        raise SubmissionError(f"cannot stat {gcd.CANDIDATE_RELPATH!r}: {exc}") from exc
    if not candidate_path.is_file() or os.path.islink(candidate_path):
        raise SubmissionError(
            f"{gcd.CANDIDATE_RELPATH!r} must be a regular file "
            "(symlink-rejected)"
        )
    if size > MAX_SUBMISSION_BYTES:
        raise SubmissionError(
            f"{gcd.CANDIDATE_RELPATH!r} is {size} bytes, exceeding the "
            f"{MAX_SUBMISSION_BYTES}-byte submission cap (oversized-submission)"
        )
    try:
        text = candidate_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SubmissionError(
            f"cannot read {gcd.CANDIDATE_RELPATH!r} as UTF-8: {exc}"
        ) from exc
    try:
        payload = loads_dict_strict(text, what="candidate submission")
    except ContractError as exc:
        raise SubmissionError(f"candidate submission is not strict JSON: {exc}") from exc
    try:
        return gcd.candidate_with_defaults(payload)
    except ContractError as exc:
        raise SubmissionError(f"invalid candidate config: {exc}") from exc


def _check_eval_root(eval_root: Path, submission: Path) -> Path:
    try:
        eval_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EvaluatorError(f"cannot create eval_root {eval_root}: {exc}") from exc
    resolved = eval_root.resolve()
    if _is_nested_or_equal(resolved, submission):
        raise EvaluatorError(
            "eval_root must live outside the submission directory: the candidate "
            "and the evaluator must never share a writable directory"
        )
    return resolved


def _verify_trusted_sources(orfs_checkout: Any) -> tuple[Path, dict[str, Any]]:
    """Check the pinned checkout carries every lockfile required asset.

    Returns the resolved checkout plus the lock payload. Raises
    :class:`EvaluatorError` for a bad path argument and returns a
    human-readable failure string ("" when ok) for missing assets so the
    caller can classify it as infrastructure failure.
    """
    raw = os.fspath(orfs_checkout) if isinstance(orfs_checkout, os.PathLike) else orfs_checkout
    if not isinstance(raw, str) or not raw.strip():
        raise EvaluatorError("orfs_checkout must be a non-empty path")
    root = Path(raw).resolve()
    if not root.is_dir():
        raise EvaluatorError(f"orfs_checkout does not exist or is not a dir: {raw!r}")
    lock = load_toolchain_lock()
    missing = [
        rel
        for rel in lock.get("required_assets", [])
        if not (root / rel).is_file() or (root / rel).is_symlink()
    ]
    if missing:
        detail = (
            f"trusted pinned sources missing under {root}: {missing} "
            "(restore the pinned checkout before grading)"
        )
        return root, {"ok": False, "detail": detail, "lock": lock}
    return root, {"ok": True, "detail": "", "lock": lock}


def _evaluator_provenance(
    *,
    run_id: str,
    candidate: Mapping[str, float],
    seed: int,
    timeout_s: float,
    checkout: Path,
    eval_scratch: Path,
    flow_result: Any = None,
    tool_versions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "evaluator_id": EVALUATOR_ID,
        "evaluator_version": EVALUATOR_VERSION,
        "evaluator_run_id": run_id,
        "orfs_commit": gcd.ORFS_COMMIT,
        "image_pinned_ref": gcd.IMAGE_PINNED_REF,
        "seed": seed,
        "timeout_s": timeout_s,
        "candidate_hash": bl.compute_candidate_hash(dict(candidate)),
        "candidate": dict(candidate),
        "candidate_path": str(eval_scratch / gcd.CANDIDATE_RELPATH),
        "eval_scratch": str(eval_scratch),
        "orfs_checkout": str(checkout),
        "flow_tool": gcd_flow.FLOW_TOOL_NAME,
        "flow_endpoint": gcd_flow.FLOW_ENDPOINT,
        "metrics_schema": gcd_metrics.PINNED_SCHEMA,
        "grader_id": gcd.GCD_GRADER_ID,
        "grader_version": gcd.GCD_GRADER_VERSION,
        "trust_note": (
            "agent-visible logs/metrics/reports are untrusted and were never "
            "read; this grade was recomputed from trusted parser + grader "
            "outputs in a fresh evaluator scratch"
        ),
    }
    if tool_versions is not None:
        provenance["tool_versions"] = dict(tool_versions)
    if flow_result is not None:
        status = getattr(flow_result, "status", None)
        provenance["flow_status"] = getattr(status, "value", str(status))
        provenance["flow_stage_reached"] = str(getattr(flow_result, "stage_reached", ""))
        flow_prov = getattr(flow_result, "provenance", None)
        if isinstance(flow_prov, Mapping):
            for key in (
                "candidate_sha256",
                "variant",
                "duration_s",
                "runner_status",
                "runner_exit_code",
            ):
                if key in flow_prov:
                    provenance[f"flow_{key}"] = flow_prov[key]
    return provenance


def _invalid_result(
    *,
    candidate: Mapping[str, float] | None,
    reason_codes: tuple[str, ...],
    message: str,
    seed: int,
    provenance_extra: Mapping[str, Any] | None = None,
    eval_scratch: Path | None = None,
) -> EvalResult:
    codes = ("invalid-submission", *[c for c in reason_codes if c != "invalid-submission"])
    grade = gcd_grader.GcdGrade(
        valid=False,
        reward=0.0,
        feasibility=False,
        area_delta=0.0,
        reason_codes=codes,
        metrics=(Metric(name="reward", value=0.0, unit="score"),),
        status=GradeStatus.INVALID_SUBMISSION,
        infra_error=False,
        excluded_from_aggregates=False,
        message=message,
        baseline_area=0.0,
        candidate_area=0.0,
    )
    provenance = {
        "evaluator_id": EVALUATOR_ID,
        "evaluator_version": EVALUATOR_VERSION,
        "evaluator_run_id": f"eval_{uuid.uuid4().hex[:12]}",
        "orfs_commit": gcd.ORFS_COMMIT,
        "image_pinned_ref": gcd.IMAGE_PINNED_REF,
        "seed": seed,
        "candidate_hash": bl.compute_candidate_hash(dict(candidate)) if candidate else "",
        "eval_scratch": str(eval_scratch) if eval_scratch is not None else "",
        "trust_note": "submission rejected before any flow: no agent file entered scoring",
    }
    if provenance_extra:
        provenance.update(dict(provenance_extra))
    return EvalResult(
        status=GradeStatus.INVALID_SUBMISSION,
        grade=grade,
        metrics=None,
        candidate=dict(candidate) if candidate else {},
        provenance=provenance,
        reason_codes=codes,
        message=message,
        eval_scratch=str(eval_scratch) if eval_scratch is not None else "",
    )


def _infra_result(
    *,
    candidate: Mapping[str, float],
    seed: int,
    timeout_s: float,
    checkout: Path,
    eval_scratch: Path,
    flow_result: Any,
    reason: str,
    detail: str,
    tool_versions: Mapping[str, Any] | None = None,
) -> EvalResult:
    run_id = f"eval_{uuid.uuid4().hex[:12]}"
    provenance = _evaluator_provenance(
        run_id=run_id,
        candidate=candidate,
        seed=seed,
        timeout_s=timeout_s,
        checkout=checkout,
        eval_scratch=eval_scratch,
        flow_result=flow_result,
        tool_versions=tool_versions,
    )
    grade = gcd_grader.grade_infra_error(reason=reason, detail=detail)
    status = GradeStatus.INFRA_ERROR
    flow_status = getattr(getattr(flow_result, "status", None), "value", "")
    if flow_status == "timeout" or getattr(flow_result, "timed_out", False):
        status = GradeStatus.TIMEOUT
    codes = tuple(grade.reason_codes)
    return EvalResult(
        status=status,
        grade=grade,
        metrics=None,
        candidate=dict(candidate),
        provenance=provenance,
        reason_codes=codes,
        message=grade.message,
        eval_scratch=str(eval_scratch),
    )


def evaluate_submission(
    submission_dir: str | os.PathLike[str],
    *,
    orfs_checkout: str | os.PathLike[str],
    eval_root: str | os.PathLike[str],
    runner: Any,
    baseline_record: Mapping[str, Any],
    seed: int,
    timeout_s: float,
    env_overrides: Mapping[str, str] | None = None,
    tool_versions: Mapping[str, str] | None = None,
    run_flow_fn: Callable[..., Any] | None = None,
    parse_fn: Callable[[Any], gcd_metrics.GcdMetrics] | None = None,
    grade_fn: Callable[..., gcd_grader.GcdGrade] | None = None,
) -> EvalResult:
    """Re-run one submission in a fresh restricted evaluator workspace.

    :param submission_dir: agent-controlled directory holding exactly
        ``candidate.json``. Only those bytes are imported; nothing else
        in the directory is ever read.
    :param orfs_checkout: trusted pinned ORFS checkout (verified against
        the lockfile ``required_assets`` before any flow).
    :param eval_root: parent directory for the fresh evaluator scratch
        (must live outside ``submission_dir``; misuse raises
        :class:`EvaluatorError`).
    :param runner: object with a ``run(tool, args, *, cwd, log_dir, env,
        timeout_s)`` method used by the trusted flow adapter.
    :param baseline_record: approved baseline mapping for the trusted
        grader (validated inside the grader; rejected baselines fail
        closed with reward ``0.0``).
    :param seed: recorded in evaluator provenance (no ORFS seed
        passthrough exists; see :mod:`flow`).
    :param timeout_s: positive finite deadline forwarded to the flow.
    :param run_flow_fn: injectable flow callable for tests with signature
        ``(candidate, *, orfs_checkout, scratch_dir, runner, seed,
        timeout_s)``. Defaults to :func:`flow.run_gcd_flow`.
    :param parse_fn: injectable trusted-parser callable
        ``(flow_result) -> GcdMetrics``. Defaults to fail-closed
        :func:`metrics.parse_flow_result` with no report texts (a real
        pinned run must supply report extraction on top).
    :param grade_fn: injectable trusted-grader callable
        ``(metrics, *, baseline_record, evidence) -> GcdGrade``. Defaults
        to :func:`grader.grade_gcd_candidate`.
    :returns: an :class:`EvalResult` whose ``grade`` is always recomputed
        from trusted code -- never from agent-supplied numbers.
    """
    seed_value = require_seed(seed)
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not float(timeout_s) > 0
        or float(timeout_s) != float(timeout_s)
        or float(timeout_s) in (float("inf"), float("-inf"))
    ):
        raise EvaluatorError(f"timeout_s must be a positive finite number, got {timeout_s!r}")
    deadline = float(timeout_s)
    if not isinstance(baseline_record, Mapping):
        raise EvaluatorError("baseline_record must be a mapping")
    if runner is None or not callable(getattr(runner, "run", None)):
        raise EvaluatorError("runner must expose a callable run(tool, args, ...) method")

    submission = _resolve_dir(submission_dir, field_name="submission_dir").resolve()
    root = _check_eval_root(
        _resolve_dir(eval_root, field_name="eval_root"), submission
    )

    # --- validate the submission before touching any trusted source --------
    try:
        candidate = validate_submission_dir(submission)
    except (SubmissionError, ContractError) as exc:
        reason = str(exc)
        code = "invalid-candidate"
        for marker in (
            "protected-asset",
            "unauthorized-file",
            "symlink-rejected",
            "oversized-submission",
            "missing",
        ):
            if marker in reason:
                code = "missing-candidate" if marker == "missing" else marker
                break
        return _invalid_result(
            candidate=None,
            reason_codes=(code,),
            message=f"submission rejected: {exc}",
            seed=seed_value,
        )

    # --- fresh evaluator scratch: never the agent workspace -----------------
    run_id = f"eval_{uuid.uuid4().hex[:12]}"
    eval_scratch = (root / run_id).resolve()
    if _is_nested_or_equal(eval_scratch, submission):
        raise EvaluatorError("evaluator scratch collides with the submission directory")
    try:
        eval_scratch.mkdir(parents=False, exist_ok=False)
    except OSError as exc:
        raise EvaluatorError(f"cannot create evaluator scratch {eval_scratch}: {exc}") from exc
    try:
        candidate_bytes = (submission / gcd.CANDIDATE_RELPATH).read_bytes()
    except OSError as exc:
        return _invalid_result(
            candidate=candidate,
            reason_codes=("invalid-candidate",),
            message=f"submission rejected: cannot re-read candidate: {exc}",
            seed=seed_value,
            eval_scratch=eval_scratch,
        )
    (eval_scratch / gcd.CANDIDATE_RELPATH).write_bytes(candidate_bytes)

    # --- restore + verify trusted pinned sources ----------------------------
    checkout, trust = _verify_trusted_sources(orfs_checkout)
    if not trust["ok"]:
        provenance_extra = {
            "evaluator_run_id": run_id,
            "eval_scratch": str(eval_scratch),
            "orfs_checkout": str(checkout),
        }
        grade = gcd_grader.grade_infra_error(
            reason="trusted-sources-missing", detail=trust["detail"]
        )
        return EvalResult(
            status=GradeStatus.INFRA_ERROR,
            grade=grade,
            metrics=None,
            candidate=dict(candidate),
            provenance={
                **_evaluator_provenance(
                    run_id=run_id,
                    candidate=candidate,
                    seed=seed_value,
                    timeout_s=deadline,
                    checkout=checkout,
                    eval_scratch=eval_scratch,
                    tool_versions=tool_versions,
                ),
                **provenance_extra,
            },
            reason_codes=tuple(grade.reason_codes),
            message=grade.message,
            eval_scratch=str(eval_scratch),
        )

    flow_scratch = eval_scratch / "flow_scratch"
    try:
        flow_scratch.mkdir(parents=False, exist_ok=False)
    except OSError as exc:
        raise EvaluatorError(f"cannot create flow scratch {flow_scratch}: {exc}") from exc

    # --- trusted re-run ------------------------------------------------------
    flow_call = run_flow_fn or gcd_flow.run_gcd_flow
    try:
        if run_flow_fn is None:
            flow_result = gcd_flow.run_gcd_flow(
                dict(candidate),
                orfs_checkout=checkout,
                scratch_dir=flow_scratch,
                runner=runner,
                seed=seed_value,
                timeout_s=deadline,
                env_overrides=env_overrides,
                tool_versions=tool_versions,
            )
        else:
            flow_result = flow_call(
                dict(candidate),
                orfs_checkout=checkout,
                scratch_dir=flow_scratch,
                runner=runner,
                seed=seed_value,
                timeout_s=deadline,
            )
    except Exception as exc:
        return _infra_result(
            candidate=candidate,
            seed=seed_value,
            timeout_s=deadline,
            checkout=checkout,
            eval_scratch=eval_scratch,
            flow_result=None,
            reason="flow-runner-error",
            detail=f"{type(exc).__name__}: {exc}",
            tool_versions=tool_versions,
        )

    flow_status = getattr(getattr(flow_result, "status", None), "value", "")
    if flow_status == "timeout" or bool(getattr(flow_result, "timed_out", False)):
        return _infra_result(
            candidate=candidate,
            seed=seed_value,
            timeout_s=deadline,
            checkout=checkout,
            eval_scratch=eval_scratch,
            flow_result=flow_result,
            reason="flow-timeout",
            detail="flow exceeded its deadline",
            tool_versions=tool_versions,
        )
    if flow_status == "infra_error" or not bool(getattr(flow_result, "launched", True)):
        detail = str(getattr(flow_result, "error", "") or "flow infrastructure failure")
        return _infra_result(
            candidate=candidate,
            seed=seed_value,
            timeout_s=deadline,
            checkout=checkout,
            eval_scratch=eval_scratch,
            flow_result=flow_result,
            reason="flow-infra-error",
            detail=detail,
            tool_versions=tool_versions,
        )

    # --- trusted parse + grade (agent numbers are never consulted) -----------
    parse_call = parse_fn or (
        lambda flow_res: gcd_metrics.parse_flow_result(
            flow_res, timing_text="", area_text="", drc_text=None
        )
    )
    try:
        parsed = parse_call(flow_result)
    except gcd_metrics.MetricsError as exc:
        raise EvaluatorError(f"parse_fn misuse: {exc}") from exc
    if not isinstance(parsed, gcd_metrics.GcdMetrics):
        raise EvaluatorError("parse_fn must return a GcdMetrics")

    evidence: dict[str, Any] = {
        "protected_hash": bl.compute_protected_hash(),
        "orfs_commit": gcd.ORFS_COMMIT,
        "image_pinned_ref": gcd.IMAGE_PINNED_REF,
        "routed_ok": parsed.routed_ok,
        "drc_count": parsed.drc_count,
        "unconstrained_paths": parsed.unconstrained_paths,
        "wns_ns": parsed.wns_ns,
        "tns_ns": parsed.tns_ns,
    }
    grade_call = grade_fn or gcd_grader.grade_gcd_candidate
    try:
        grade = grade_call(parsed, baseline_record=baseline_record, evidence=evidence)
    except (bl.BaselineError, ContractError) as exc:
        raise EvaluatorError(f"grade_fn misuse: {exc}") from exc
    if not isinstance(grade, gcd_grader.GcdGrade):
        raise EvaluatorError("grade_fn must return a GcdGrade")

    provenance = _evaluator_provenance(
        run_id=run_id,
        candidate=candidate,
        seed=seed_value,
        timeout_s=deadline,
        checkout=checkout,
        eval_scratch=eval_scratch,
        flow_result=flow_result,
        tool_versions=tool_versions,
    )
    status = GradeStatus.PASS if grade.valid else GradeStatus.FAIL
    return EvalResult(
        status=status,
        grade=grade,
        metrics=parsed,
        candidate=dict(candidate),
        provenance=provenance,
        reason_codes=tuple(grade.reason_codes),
        message=grade.message,
        eval_scratch=str(eval_scratch),
    )


def to_grade_result(
    result: EvalResult,
    *,
    baseline_record: Mapping[str, Any] | None = None,
) -> Any:
    """Convert an :class:`EvalResult` to an M0 :class:`GradeResult`.

    Task identity comes from ``baseline_record`` when given (validated),
    else from the stock GCD task constants. Intended for environment
    integration in the follow-on ticket (#17);     scoring semantics stay
    owned by the trusted GCD grader wrapped here.
    """
    if not isinstance(result, EvalResult):
        raise EvaluatorError("result must be an EvalResult")
    task_id = gcd.GCD_TASK_ID
    task_version = gcd.GCD_TASK_VERSION
    seed = int(result.provenance.get("seed", 0))
    if baseline_record is not None:
        if not isinstance(baseline_record, Mapping):
            raise EvaluatorError("baseline_record must be a mapping")
        task_id = str(baseline_record.get("task_id", task_id))
        task_version = str(baseline_record.get("task_version", task_version))
    grade = result.grade
    return GradeResult(
        schema_version=1,
        task_id=task_id,
        task_version=task_version,
        status=result.status,
        score=float(grade.reward),
        passed=bool(grade.valid and result.status == GradeStatus.PASS),
        metrics=tuple(grade.metrics),
        provenance=Provenance(
            task_id=task_id,
            task_version=task_version,
            seed=require_seed(seed),
            toolchain_refs=dict(gcd.TOOLCHAIN_REFS),
            grader_id=gcd.GCD_GRADER_ID,
            grader_version=gcd.GCD_GRADER_VERSION,
        ),
        message=result.message,
        details={
            "evaluator_id": EVALUATOR_ID,
            "evaluator_version": EVALUATOR_VERSION,
            "evaluator_provenance": dict(result.provenance),
            "reason_codes": list(result.reason_codes),
            "eval_scratch": result.eval_scratch,
        },
    )


__all__ = [
    "ALLOWED_SUBMISSION_FILES",
    "EVALUATOR_ID",
    "EVALUATOR_VERSION",
    "MAX_SUBMISSION_BYTES",
    "EvalResult",
    "EvaluatorError",
    "SubmissionError",
    "evaluate_submission",
    "to_grade_result",
    "validate_submission_dir",
]
