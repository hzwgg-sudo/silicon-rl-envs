"""Local task execution and grading commands (M0-08).

Toy-only CLI over :class:`ToyEnvironment`:

- ``run-task`` reads a task JSON file (:class:`TaskSpec` dict) plus an
  actions JSON file (list of ``{action_type, params}``), runs one
  :class:`ToyEnvironment` episode into ``--output-dir``, and writes
  ``summary.json`` + ``trace.jsonl`` + ``manifest.json`` plus a saved
  candidate file for later regrading.
- ``grade-task`` regrades a saved submission dir with the pure
  :func:`grade_candidate` grader (no environment, no tools).

Exit codes (documented in ``--help`` and the README):

- ``0``: episode/submission passed.
- ``2``: invalid submission or grading failure (bad task/actions
  content, episode failed, regrade mismatch/fail).
- ``3``: infrastructure/usage failure (missing files, output-dir
  collision, unsupported task id, I/O errors, unexpected exceptions).

Stdlib-only, Python >= 3.10. No network, no EDA tools, no interactivity.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

EXIT_PASS = 0
EXIT_FAIL = 2
EXIT_INFRA = 3

SUPPORTED_TASK_IDS = ("toy-text-edit",)

TASK_FILENAME = "task.json"
ACTIONS_FILENAME = "actions.json"
SUMMARY_FILENAME = "summary.json"
TRACE_FILENAME = "trace.jsonl"
MANIFEST_FILENAME = "manifest.json"
SUBMISSION_REL = "submission/note.txt"
GRADE_FILENAME = "grade.json"


class CliInvalidError(ValueError):
    """Task/actions content is invalid (exit 2)."""


class CliInfraError(ValueError):
    """CLI usage or environment failure (exit 3)."""


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def _read_text_file(path: Path, *, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliInfraError(f"{what} {path} cannot be read: {exc}") from exc


def _parse_json(text: str, *, what: str) -> Any:
    from silicon_env.types import ContractError, loads_strict

    try:
        return loads_strict(text)
    except ContractError as exc:
        raise CliInvalidError(f"{what} is not valid strict JSON: {exc}") from exc


def load_task_spec(task_path: Path):
    """Load and validate a TaskSpec dict; toy task ids only."""
    from silicon_env.task import TaskSpec
    from silicon_env.types import ContractError

    text = _read_text_file(task_path, what="task file")
    payload = _parse_json(text, what=f"task file {task_path}")
    if not isinstance(payload, dict):
        raise CliInvalidError(f"task file {task_path} must decode to an object")
    try:
        spec = TaskSpec.from_dict(payload)
    except ContractError as exc:
        raise CliInvalidError(f"task file {task_path} is invalid: {exc}") from exc
    if spec.task_id not in SUPPORTED_TASK_IDS:
        raise CliInfraError(
            f"unsupported task_id {spec.task_id!r}: this CLI is toy-only "
            f"(supported: {sorted(SUPPORTED_TASK_IDS)})"
        )
    return spec


def load_actions(actions_path: Path) -> list[dict[str, Any]]:
    """Load an actions list of {action_type, params} dicts (validated)."""
    from silicon_env.task import Action
    from silicon_env.types import ContractError

    text = _read_text_file(actions_path, what="actions file")
    payload = _parse_json(text, what=f"actions file {actions_path}")
    if not isinstance(payload, list):
        raise CliInvalidError(f"actions file {actions_path} must decode to a list")
    actions: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        where = f"actions file {actions_path} index {index}"
        if not isinstance(item, dict):
            raise CliInvalidError(f"{where} must be an object")
        unknown = sorted(set(item) - {"action_type", "params", "schema_version", "step_index"})
        if unknown:
            raise CliInvalidError(f"{where} has unknown fields: {unknown}")
        action_type = item.get("action_type")
        if not isinstance(action_type, str) or not action_type.strip():
            raise CliInvalidError(f"{where} needs a non-empty 'action_type' string")
        params = item.get("params", {})
        if not isinstance(params, dict):
            raise CliInvalidError(f"{where} 'params' must be an object")
        schema_version = item.get("schema_version", 1)
        step_index = item.get("step_index", index + 1)
        try:
            action = Action(
                schema_version=schema_version,
                action_type=action_type,
                params=dict(params),
                step_index=step_index,
            )
            action.validate()
        except (ContractError, TypeError, ValueError) as exc:
            raise CliInvalidError(f"{where} is invalid: {exc}") from exc
        actions.append(
            {
                "schema_version": action.schema_version,
                "action_type": action.action_type,
                "params": dict(action.params),
                "step_index": action.step_index,
            }
        )
    return actions


def prepare_output_dir(output_dir: Path) -> Path:
    """Create ``output_dir``; refuse to reuse a non-empty directory."""
    try:
        if output_dir.exists():
            if not output_dir.is_dir():
                raise CliInfraError(f"output dir {output_dir} exists and is not a directory")
            try:
                entries = list(output_dir.iterdir())
            except OSError as exc:
                raise CliInfraError(f"output dir {output_dir} cannot be listed: {exc}") from exc
            if entries:
                raise CliInfraError(
                    f"output dir {output_dir} already exists and is not empty "
                    f"({len(entries)} entries); use a fresh directory"
                )
        else:
            output_dir.mkdir(parents=True, exist_ok=False)
    except CliInfraError:
        raise
    except OSError as exc:
        raise CliInfraError(f"output dir {output_dir} cannot be created: {exc}") from exc
    return output_dir


def apply_seed_override(spec, seed: int | None):
    if seed is None:
        return spec
    from silicon_env.types import ContractError, require_seed

    try:
        clean = require_seed(seed)
    except ContractError as exc:
        raise CliInvalidError(f"invalid --seed {seed!r}: {exc}") from exc
    try:
        return replace(spec, seed=clean)
    except Exception as exc:  # pragma: no cover - frozen dataclass replace
        raise CliInfraError(f"cannot apply --seed: {exc}") from exc


def _grade_from_candidate(
    candidate: str | None,
    *,
    task,
    seed: int,
    read_error: str = "",
    fallback_status=None,
    fallback_message: str = "",
):
    """Build a GradeResult from saved candidate text (pure toy grader)."""
    from silicon_env.environments.toy import TOY_FILE, grade_candidate
    from silicon_env.grader import GradeResult
    from silicon_env.types import GradeStatus, Metric, Provenance

    provenance = Provenance(
        task_id=task.task_id,
        task_version=task.task_version,
        seed=seed,
        toolchain_refs=dict(task.toolchain_refs),
        grader_id=task.grader.grader_id,
        grader_version=task.grader.grader_version,
    )
    if candidate is None:
        status = fallback_status or GradeStatus.TOOL_FAILURE
        return GradeResult(
            schema_version=1,
            task_id=task.task_id,
            task_version=task.task_version,
            status=status,
            score=0.0,
            passed=False,
            metrics=(),
            provenance=provenance,
            message=fallback_message or f"cannot read {TOY_FILE}: {read_error}",
            details={"file": TOY_FILE},
        )
    passed, message = grade_candidate(candidate)
    status = GradeStatus.PASS if passed else GradeStatus.FAIL
    return GradeResult(
        schema_version=1,
        task_id=task.task_id,
        task_version=task.task_version,
        status=status,
        score=1.0 if passed else 0.0,
        passed=passed,
        metrics=(Metric(name="exact_match", value=1.0 if passed else 0.0, unit="bool"),),
        provenance=provenance,
        message=message,
        details={"file": TOY_FILE, "candidate_chars": len(candidate)},
    )


def run_episode(
    *,
    task,
    seed: int,
    action_dicts: Sequence[Mapping[str, Any]],
    output_dir: Path,
) -> dict[str, Any]:
    """Run one toy episode; write machine-readable outputs; return summary."""
    from silicon_env.environments.toy import TOY_FILE, ToyEnvironment
    from silicon_env.grader import GradeResult
    from silicon_env.task import Action
    from silicon_env.types import GradeStatus, StepStatus, dumps_strict

    work_root = output_dir / "_work"
    try:
        work_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CliInfraError(f"cannot create work dir {work_root}: {exc}") from exc

    env = ToyEnvironment(work_root=work_root, enable_trace=True)
    grade: GradeResult | None = None
    last_status: StepStatus | None = None
    last_message = ""
    steps = 0
    submitted_message = ""
    try:
        env.reset(task, seed)
        for raw in action_dicts:
            if env.done:
                break
            action = Action(
                schema_version=int(raw["schema_version"]),
                action_type=str(raw["action_type"]),
                params=dict(raw["params"]),
                step_index=int(raw["step_index"]),
            )
            result = env.step(action)
            steps = result.step_index
            last_status = result.status
            last_message = result.message
            if result.done and result.status in (StepStatus.TIMEOUT, StepStatus.INFRA_ERROR):
                from silicon_env.types import GradeStatus as _GS

                status = _GS.TIMEOUT if result.status == StepStatus.TIMEOUT else _GS.INFRA_ERROR
                grade = _grade_from_candidate(
                    None,
                    task=task,
                    seed=seed,
                    fallback_status=status,
                    fallback_message=result.message or result.status.value,
                )
                break
        if grade is None and not env.done:
            grade = env.submit()
            steps = env.budget_tracker.steps_used
            submitted_message = grade.message
        if grade is None:
            # Done via submit-step (or budget edge): re-grade the live
            # workspace with the pure grader so summary matches the trace.
            assert env.workspace is not None
            try:
                candidate = env.workspace.read_text(TOY_FILE)
                read_error = ""
            except Exception as exc:
                candidate = None  # type: ignore[assignment]
                read_error = str(exc)
            grade = _grade_from_candidate(candidate, task=task, seed=seed, read_error=read_error)
        assert grade is not None
        try:
            candidate_text: str | None = env.workspace.read_text(TOY_FILE)  # type: ignore[union-attr]
            candidate_error = ""
        except Exception as exc:
            candidate_text, candidate_error = None, str(exc)
    finally:
        try:
            env.close()
        except Exception:
            pass

    # -- persist outputs -------------------------------------------------
    try:
        run_dir = env.run_dir
        if run_dir is None or not Path(run_dir).is_dir():
            raise CliInfraError("episode produced no trace run dir")
        src_trace = Path(run_dir) / TRACE_FILENAME
        src_manifest = Path(run_dir) / MANIFEST_FILENAME
        if not src_trace.is_file() or not src_manifest.is_file():
            raise CliInfraError("episode trace files are missing")
        shutil.copyfile(src_trace, output_dir / TRACE_FILENAME)
        shutil.copyfile(src_manifest, output_dir / MANIFEST_FILENAME)
        if (Path(run_dir) / "artifacts").is_dir():
            shutil.copytree(Path(run_dir) / "artifacts", output_dir / "artifacts")
        (output_dir / TASK_FILENAME).write_text(
            dumps_strict(task.to_dict()) + "\n", encoding="utf-8"
        )
        (output_dir / ACTIONS_FILENAME).write_text(
            dumps_strict(list(action_dicts)) + "\n", encoding="utf-8"
        )
        submission_path = output_dir / SUBMISSION_REL
        submission_path.parent.mkdir(parents=True, exist_ok=True)
        if candidate_text is not None:
            submission_path.write_text(candidate_text, encoding="utf-8")
        budget_snapshot: dict[str, Any] = {}
        try:
            tracker = env.budget_tracker
            if tracker is not None:
                budget_snapshot = tracker.snapshot()
        except Exception:
            budget_snapshot = {}
        summary = {
            "schema_version": 1,
            "task_id": task.task_id,
            "task_version": task.task_version,
            "seed": seed,
            "passed": bool(grade.passed),
            "status": grade.status.value,
            "score": float(grade.score),
            "steps": int(steps),
            "message": submitted_message or grade.message or last_message,
            "last_step_status": last_status.value if last_status is not None else "",
            "candidate_error": candidate_error,
            "files": {
                "task": TASK_FILENAME,
                "actions": ACTIONS_FILENAME,
                "trace": TRACE_FILENAME,
                "manifest": MANIFEST_FILENAME,
                "submission": SUBMISSION_REL if candidate_text is not None else "",
            },
            "budget": budget_snapshot,
        }
        if grade.status == GradeStatus.PASS and not grade.passed:  # pragma: no cover
            raise CliInfraError("internal grading inconsistency")
        (output_dir / SUMMARY_FILENAME).write_text(dumps_strict(summary) + "\n", encoding="utf-8")
    except CliInfraError:
        raise
    except OSError as exc:
        raise CliInfraError(f"cannot write outputs into {output_dir}: {exc}") from exc
    return summary


def cmd_run_task(args: argparse.Namespace) -> int:
    task = load_task_spec(Path(args.task))
    task = apply_seed_override(task, args.seed)
    seed = task.seed
    action_dicts = load_actions(Path(args.actions))
    output_dir = prepare_output_dir(Path(args.output_dir))
    summary = run_episode(task=task, seed=seed, action_dicts=action_dicts, output_dir=output_dir)
    print(
        f"status={summary['status']} passed={str(summary['passed']).lower()} "
        f"score={summary['score']} steps={summary['steps']} output={output_dir}"
    )
    if summary["status"] == "infra_error":
        return EXIT_INFRA
    return EXIT_PASS if summary["passed"] else EXIT_FAIL


def cmd_grade_task(args: argparse.Namespace) -> int:
    from silicon_env.types import dumps_strict

    submission_dir = Path(args.submission_dir)
    if not submission_dir.is_dir():
        raise CliInfraError(f"submission dir {submission_dir} does not exist")
    task_path = submission_dir / TASK_FILENAME
    if not task_path.is_file():
        raise CliInfraError(
            f"submission dir {submission_dir} has no {TASK_FILENAME}; "
            "grade the output dir produced by run-task"
        )
    task = load_task_spec(task_path)
    candidate_path = submission_dir / SUBMISSION_REL
    if not candidate_path.is_file():
        raise CliInfraError(
            f"submission candidate {candidate_path} is missing; expected the run-task output dir"
        )
    try:
        candidate = candidate_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliInfraError(f"cannot read candidate {candidate_path}: {exc}") from exc
    grade = _grade_from_candidate(candidate, task=task, seed=task.seed)
    output_path = Path(args.output) if args.output else (submission_dir / GRADE_FILENAME)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(dumps_strict(grade.to_dict()) + "\n", encoding="utf-8")
    except OSError as exc:
        raise CliInfraError(f"cannot write grade file {output_path}: {exc}") from exc
    print(
        f"status={grade.status.value} passed={str(bool(grade.passed)).lower()} "
        f"score={float(grade.score)} submission={submission_dir}"
    )
    return EXIT_PASS if grade.passed else EXIT_FAIL


EXIT_HELP = (
    "exit codes: 0 pass, 2 invalid submission or grading failure, 3 infrastructure or usage failure"
)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(EXIT_INFRA, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="silicon-run-task", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser(
        "run-task",
        help="run a toy task with an explicit actions file",
        description=f"Run one toy episode into --output-dir. {EXIT_HELP}.",
    )
    run_p.add_argument("--task", required=True, help="path to task JSON file (TaskSpec dict)")
    run_p.add_argument(
        "--actions", required=True, help="path to actions JSON file (list of {action_type, params})"
    )
    run_p.add_argument("--output-dir", required=True, help="fresh (nonexistent or empty) dir")
    run_p.add_argument("--seed", type=int, default=None, help="override the task seed")
    run_p.set_defaults(func=cmd_run_task)
    grade_p = sub.add_parser(
        "grade-task",
        help="regrade a saved run-task submission dir",
        description=f"Regrade a run-task output dir with the pure toy grader. {EXIT_HELP}.",
    )
    grade_p.add_argument("--submission-dir", required=True, help="run-task output dir")
    grade_p.add_argument(
        "--output", default=None, help="grade JSON path (default: <submission-dir>/grade.json)"
    )
    grade_p.set_defaults(func=cmd_grade_task)
    return parser


def build_run_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="silicon-run-task",
        description=f"Run one toy episode into --output-dir. {EXIT_HELP}.",
    )
    parser.add_argument("--task", required=True, help="path to task JSON file (TaskSpec dict)")
    parser.add_argument(
        "--actions", required=True, help="path to actions JSON file (list of {action_type, params})"
    )
    parser.add_argument("--output-dir", required=True, help="fresh (nonexistent or empty) dir")
    parser.add_argument("--seed", type=int, default=None, help="override the task seed")
    return parser


def build_grade_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="silicon-grade-task",
        description=f"Regrade a run-task output dir with the pure toy grader. {EXIT_HELP}.",
    )
    parser.add_argument("--submission-dir", required=True, help="run-task output dir")
    parser.add_argument(
        "--output", default=None, help="grade JSON path (default: <submission-dir>/grade.json)"
    )
    return parser


def main_run(argv: Sequence[str] | None = None) -> int:
    args = build_run_parser().parse_args(argv)
    try:
        return cmd_run_task(args)
    except CliInvalidError as exc:
        eprint(f"error: invalid submission: {exc}")
        return EXIT_FAIL
    except CliInfraError as exc:
        eprint(f"error: infrastructure failure: {exc}")
        return EXIT_INFRA
    except Exception as exc:  # never leak a traceback as exit 0
        eprint(f"error: infrastructure failure: unexpected {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)
        return EXIT_INFRA


def main_grade(argv: Sequence[str] | None = None) -> int:
    args = build_grade_parser().parse_args(argv)
    try:
        return cmd_grade_task(args)
    except CliInvalidError as exc:
        eprint(f"error: invalid submission: {exc}")
        return EXIT_FAIL
    except CliInfraError as exc:
        eprint(f"error: infrastructure failure: {exc}")
        return EXIT_INFRA
    except Exception as exc:  # never leak a traceback as exit 0
        eprint(f"error: infrastructure failure: unexpected {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)
        return EXIT_INFRA


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    if not argv and len(sys.argv) == 1:
        parser.print_help()
        return EXIT_INFRA
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return EXIT_INFRA
    try:
        return args.func(args)
    except CliInvalidError as exc:
        eprint(f"error: invalid submission: {exc}")
        return EXIT_FAIL
    except CliInfraError as exc:
        eprint(f"error: infrastructure failure: {exc}")
        return EXIT_INFRA
    except Exception as exc:  # never leak a traceback as exit 0
        eprint(f"error: infrastructure failure: unexpected {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)
        return EXIT_INFRA


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "EXIT_FAIL",
    "EXIT_HELP",
    "EXIT_INFRA",
    "EXIT_PASS",
    "GRADE_FILENAME",
    "SUBMISSION_REL",
    "SUPPORTED_TASK_IDS",
    "CliInfraError",
    "CliInvalidError",
    "apply_seed_override",
    "build_grade_parser",
    "build_parser",
    "build_run_parser",
    "cmd_grade_task",
    "cmd_run_task",
    "eprint",
    "load_actions",
    "load_task_spec",
    "main",
    "main_grade",
    "main_run",
    "prepare_output_dir",
    "run_episode",
]
