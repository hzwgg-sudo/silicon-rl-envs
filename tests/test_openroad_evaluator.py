"""M1-07 independent clean-room GCD grading tests.

Lightweight by design: no EDA tools, Docker, network, or API keys. The
runner is always a fake stub, the flow is either the real trusted
adapter over a fake checkout or an injected fake, and baselines are
built from fake :class:`GcdMetrics` via
``baseline.build_baseline_record``.

The one real stock-GCD run is opt-in and skipped by default
(``SILICON_RUN_OPENROAD_EVAL=1`` with ``ORFS_CHECKOUT`` set); it is
blocked on the Mac dev host, which is recorded -- never fabricated.
"""

import json
import os
from pathlib import Path

import pytest

from silicon_env.environments.openroad import baseline as bl
from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad import evaluator as ev
from silicon_env.environments.openroad import flow as gcd_flow
from silicon_env.environments.openroad import grader as gcd_grader
from silicon_env.environments.openroad.metrics import GcdMetrics
from silicon_env.environments.openroad.preflight import load_toolchain_lock
from silicon_env.runner import RunResult
from silicon_env.types import GradeStatus, StepStatus

RUN_REAL_EVAL = os.environ.get("SILICON_RUN_OPENROAD_EVAL", "") == "1"
REAL_CHECKOUT = os.environ.get("ORFS_CHECKOUT", "").strip()

BASE_AREA = 1000.0
TOLS = {"area_rel": 0.01, "wns_abs_ns": 0.005, "tns_abs_ns": 0.01}


# --- fakes -----------------------------------------------------------------


def make_fake_checkout(tmp_path: Path) -> Path:
    """Pinned-checkout stand-in carrying every lockfile required asset."""
    lock = load_toolchain_lock()
    root = tmp_path / "orfs"
    for rel in lock["required_assets"]:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fake trusted asset\n", encoding="utf-8")
    return root


def _write_logs(log_dir: Path) -> tuple[Path, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    stdout_path.write_text("fake tool stdout\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    return stdout_path, stderr_path


class FakeRunner:
    """Stub behind the runner protocol; optionally emits fresh artifacts."""

    def __init__(
        self,
        *,
        status: StepStatus = StepStatus.SUCCESS,
        exit_code: int | None = 0,
        timed_out: bool = False,
        launched: bool = True,
        emit: tuple[str, ...] = (),
        error: str = "",
    ) -> None:
        self._status = status
        self._exit_code = exit_code
        self._timed_out = timed_out
        self._launched = launched
        self._emit = emit
        self._error = error
        self.calls: list[dict] = []

    def run(self, tool, args=(), *, cwd, log_dir, env=None, timeout_s=None):
        self.calls.append({"tool": tool, "args": list(args), "cwd": str(cwd)})
        log_path = Path(log_dir)
        stdout_path, stderr_path = _write_logs(log_path)
        flow_dir = Path(next(a.split("=", 1)[1] for a in args if a.startswith("WORK_HOME=")))
        for relpath in self._emit:
            target = flow_dir / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fake artifact\n", encoding="utf-8")
        return RunResult(
            tool_name=tool,
            argv=tuple(args),
            cwd=flow_dir,
            log_dir=log_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            exit_code=self._exit_code,
            status=self._status,
            timed_out=self._timed_out,
            launched=self._launched,
            duration_s=0.01,
            error=self._error,
        )


def make_metrics(
    area: float = BASE_AREA,
    wns: float = 0.0,
    tns: float = 0.0,
    *,
    valid: bool = True,
    routed_ok: bool = True,
    drc_count: int | None = 0,
    unconstrained: int | None = 0,
) -> GcdMetrics:
    return GcdMetrics(
        stage="final",
        schema="orfs-26Q2-final-v1",
        area_um2=area,
        wns_ns=wns,
        tns_ns=tns,
        routed_ok=routed_ok,
        drc_count=drc_count,
        unconstrained_paths=unconstrained,
        valid=valid,
        reasons=() if valid else ("synthetic-invalid",),
        notes=(),
        raw_refs={"timing": "<fake>", "area": "<fake>"},
    )


def make_baseline(area: float = BASE_AREA) -> dict:
    return bl.build_baseline_record(
        [make_metrics(area=area), make_metrics(area=area), make_metrics(area=area)],
        tolerances=dict(TOLS),
        run_ids=["run-0", "run-1", "run-2"],
        seeds=[0, 1, 2],
    )


def make_submission(tmp_path: Path, candidate: dict, *, name: str = "submission") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "candidate.json").write_text(json.dumps(candidate) + "\n", encoding="utf-8")
    return root


def fake_flow_success(candidate, **kwargs):
    _ = (candidate, kwargs)
    return gcd_flow.FlowResult(
        status=StepStatus.SUCCESS,
        stage_reached="final",
        artifacts={},
        provenance={"endpoint": "final"},
        message="fake final",
    )


def evaluate_clean(submission: Path, checkout: Path, tmp_path: Path, **overrides):
    kwargs = {
        "orfs_checkout": checkout,
        "eval_root": tmp_path / "eval_root",
        "runner": FakeRunner(),
        "baseline_record": make_baseline(),
        "seed": 7,
        "timeout_s": 60.0,
        "run_flow_fn": fake_flow_success,
        "parse_fn": lambda flow_result: make_metrics(),
    }
    kwargs.update(overrides)
    return ev.evaluate_submission(submission, **kwargs)


# --- submission validation --------------------------------------------------


def test_clean_submission_validates_with_stock_defaults(tmp_path):
    submission = make_submission(tmp_path, {})
    assert ev.validate_submission_dir(submission) == gcd.stock_candidate_config()


def test_missing_candidate_rejected_before_flow(tmp_path):
    submission = tmp_path / "empty"
    submission.mkdir()
    calls: list = []

    def _must_not_run(candidate, **kwargs):
        calls.append((candidate, kwargs))
        raise AssertionError("flow must not run for an invalid submission")

    result = evaluate_clean(
        submission,
        make_fake_checkout(tmp_path),
        tmp_path,
        run_flow_fn=_must_not_run,
    )
    assert calls == []
    assert result.status == GradeStatus.INVALID_SUBMISSION
    assert result.reward == 0.0
    assert result.metrics is None
    assert "missing-candidate" in result.reason_codes


@pytest.mark.parametrize("protected", ["gcd.v", "constraint.sdc", "config.mk"])
def test_protected_rtl_sdc_rejected_before_flow(tmp_path, protected):
    submission = make_submission(tmp_path, gcd.stock_candidate_config())
    (submission / protected).write_text("// tampered rtl\n", encoding="utf-8")
    calls: list = []

    def _must_not_run(candidate, **kwargs):
        calls.append((candidate, kwargs))
        raise AssertionError("flow must not run for a protected-asset submission")

    result = evaluate_clean(
        submission,
        make_fake_checkout(tmp_path),
        tmp_path,
        run_flow_fn=_must_not_run,
    )
    assert calls == []
    assert result.status == GradeStatus.INVALID_SUBMISSION
    assert result.reward == 0.0
    assert "protected-asset" in result.reason_codes


def test_extra_report_file_rejected_and_never_scored(tmp_path):
    submission = make_submission(tmp_path, gcd.stock_candidate_config())
    forged_area = BASE_AREA * 0.01
    (submission / "report.json").write_text(
        json.dumps({"area_um2": forged_area, "claim": "tiny"}) + "\n", encoding="utf-8"
    )
    result = evaluate_clean(submission, make_fake_checkout(tmp_path), tmp_path)
    assert result.status == GradeStatus.INVALID_SUBMISSION
    assert result.reward == 0.0
    assert result.metrics is None
    assert "unauthorized-file" in result.reason_codes
    # The forged number never enters scoring outputs.
    assert forged_area != 0.0
    assert str(forged_area) not in json.dumps(result.to_dict())


def test_symlink_submission_rejected(tmp_path):
    submission = make_submission(tmp_path, gcd.stock_candidate_config())
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (submission / "evil.txt").symlink_to(outside)
    with pytest.raises(ev.SubmissionError, match="symlink|unauthorized"):
        ev.validate_submission_dir(submission)
    result = evaluate_clean(submission, make_fake_checkout(tmp_path), tmp_path)
    assert result.status == GradeStatus.INVALID_SUBMISSION
    assert result.reward == 0.0


def test_symlinked_candidate_rejected(tmp_path):
    submission = tmp_path / "link-sub"
    submission.mkdir()
    outside = tmp_path / "real-candidate.json"
    outside.write_text(json.dumps(gcd.stock_candidate_config()), encoding="utf-8")
    (submission / "candidate.json").symlink_to(outside)
    with pytest.raises(ev.SubmissionError, match="[Ss]ymlink"):
        ev.validate_submission_dir(submission)


def test_oversized_candidate_rejected(tmp_path):
    submission = tmp_path / "big-sub"
    submission.mkdir()
    (submission / "candidate.json").write_text("x" * (ev.MAX_SUBMISSION_BYTES + 1))
    with pytest.raises(ev.SubmissionError, match="[Oo]versiz|cap"):
        ev.validate_submission_dir(submission)


def test_invalid_candidate_values_rejected_before_flow(tmp_path):
    submission = make_submission(tmp_path, {"PLACE_DENSITY": 999.0})
    calls: list = []

    def _must_not_run(candidate, **kwargs):
        calls.append((candidate, kwargs))
        raise AssertionError("flow must not run for an invalid candidate")

    result = evaluate_clean(
        submission,
        make_fake_checkout(tmp_path),
        tmp_path,
        run_flow_fn=_must_not_run,
    )
    assert calls == []
    assert result.status == GradeStatus.INVALID_SUBMISSION
    assert result.reward == 0.0


def test_mismatched_fingerprint_cannot_score(tmp_path):
    submission = make_submission(tmp_path, gcd.stock_candidate_config())
    record = make_baseline()
    record = {**record, "protected_hash": "0" * 64}  # stale vs trusted pins
    result = evaluate_clean(
        submission,
        make_fake_checkout(tmp_path),
        tmp_path,
        baseline_record=record,
    )
    assert result.grade.valid is False
    assert result.reward == 0.0
    assert "baseline-invalid" in result.grade.reason_codes


# --- forged reports cannot move the trusted grade ----------------------------


def test_forged_report_outside_submission_is_ignored(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    candidate = gcd.stock_candidate_config()
    clean = make_submission(tmp_path, candidate, name="clean-sub")
    forged_area = BASE_AREA * 0.01
    agent_dir = tmp_path / "agent-workspace"
    agent_dir.mkdir()
    (agent_dir / "candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    (agent_dir / "report.json").write_text(
        json.dumps({"area_um2": forged_area, "wns_ns": 0.0}) + "\n", encoding="utf-8"
    )
    # The evaluator only imports clean/candidate.json; the sibling forged
    # report is never read, so both grades must match exactly.
    first = evaluate_clean(clean, checkout, tmp_path)
    second = evaluate_clean(clean, checkout, tmp_path)
    assert first.reward == second.reward == 0.5
    assert first.reason_codes == second.reason_codes == ("ok",)
    assert str(forged_area) not in json.dumps(first.to_dict())


# --- workspace isolation -----------------------------------------------------


def test_evaluator_scratch_is_fresh_and_isolated(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    submission = make_submission(tmp_path, gcd.stock_candidate_config())
    (submission / "agent-notes.txt").unlink(missing_ok=True)
    eval_root = tmp_path / "eval_root"
    result = evaluate_clean(submission, checkout, tmp_path, eval_root=eval_root)
    assert result.ok is True
    scratch = Path(result.eval_scratch)
    assert scratch.resolve() != submission.resolve()
    assert scratch.resolve() not in submission.resolve().parents
    assert submission.resolve() not in scratch.resolve().parents
    assert scratch.parent.resolve() == eval_root.resolve()
    # Only the allowed file was imported; nothing else was copied.
    assert (scratch / "candidate.json").read_text(encoding="utf-8") == (
        submission / "candidate.json"
    ).read_text(encoding="utf-8")
    assert "agent-notes.txt" not in {p.name for p in scratch.iterdir()}
    # The agent workspace gained no evaluator outputs.
    assert sorted(p.name for p in submission.iterdir()) == ["candidate.json"]


def test_eval_root_inside_submission_is_misuse(tmp_path):
    submission = make_submission(tmp_path, gcd.stock_candidate_config())
    with pytest.raises(ev.EvaluatorError, match="[Ss]hare|outside"):
        evaluate_clean(
            submission,
            make_fake_checkout(tmp_path),
            tmp_path,
            eval_root=submission / "eval",
        )
    with pytest.raises(ev.EvaluatorError, match="[Ss]hare|outside"):
        evaluate_clean(
            submission,
            make_fake_checkout(tmp_path),
            tmp_path,
            eval_root=submission,
        )


# --- infra failures carry no trainable reward --------------------------------


@pytest.mark.parametrize("kind", ["infra_error", "timeout"])
def test_infra_failure_excluded_with_provenance(tmp_path, kind):
    if kind == "infra_error":
        flow_result = gcd_flow.FlowResult(
            status=StepStatus.INFRA_ERROR,
            stage_reached="not-started",
            artifacts={},
            provenance={"endpoint": "final"},
            message="fake infra",
        )
    else:
        flow_result = gcd_flow.FlowResult(
            status=StepStatus.TIMEOUT,
            stage_reached="place",
            artifacts={},
            provenance={"endpoint": "final"},
            message="fake timeout",
        )
    submission = make_submission(tmp_path, gcd.stock_candidate_config())
    result = evaluate_clean(
        submission,
        make_fake_checkout(tmp_path),
        tmp_path,
        run_flow_fn=lambda candidate, **kwargs: flow_result,
    )
    assert result.grade.infra_error is True
    assert result.grade.excluded_from_aggregates is True
    assert result.reward == 0.0
    assert result.status in (GradeStatus.INFRA_ERROR, GradeStatus.TIMEOUT)
    assert "infra-error-excluded" in result.reason_codes
    # Provenance is retained for diagnosis.
    assert result.provenance["orfs_commit"] == gcd.ORFS_COMMIT
    assert result.provenance["evaluator_run_id"].startswith("eval_")
    assert result.eval_scratch


def test_missing_trusted_sources_is_infra_not_reward(tmp_path):
    checkout = tmp_path / "thin-orfs"
    (checkout / "flow").mkdir(parents=True)
    (checkout / "flow" / "Makefile").write_text("# no required assets\n", encoding="utf-8")
    submission = make_submission(tmp_path, gcd.stock_candidate_config())
    result = evaluate_clean(submission, checkout, tmp_path)
    assert result.grade.infra_error is True
    assert result.grade.excluded_from_aggregates is True
    assert result.reward == 0.0
    assert "trusted-sources-missing" in result.grade.reason_codes


# --- stock end-to-end through the trusted adapter ----------------------------


def test_stock_submission_end_to_end_trusted_grade_half(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    plan = gcd_flow.resolve_flow_plan()
    emit = tuple([*plan["required_finals"], *plan["stage_markers"].values()])
    runner = FakeRunner(emit=emit)
    submission = make_submission(tmp_path, gcd.stock_candidate_config())
    record = make_baseline()
    result = ev.evaluate_submission(
        submission,
        orfs_checkout=checkout,
        eval_root=tmp_path / "eval_root",
        runner=runner,
        baseline_record=record,
        seed=7,
        timeout_s=60.0,
        # Trusted parser injection: the fake flow emits layout markers, not
        # real STA/area reports, so tests supply the measured metrics here.
        # The grade itself still runs through the real trusted grader.
        parse_fn=lambda flow_result: make_metrics(),
    )
    assert runner.calls and runner.calls[0]["tool"] == gcd_flow.FLOW_TOOL_NAME
    assert result.ok is True
    assert result.reward == 0.5
    assert result.reason_codes == ("ok",)
    # Trusted grade matches a direct trusted-grader call on the same inputs.
    direct = gcd_grader.grade_gcd_candidate(
        make_metrics(),
        baseline_record=record,
        evidence={
            "protected_hash": record["protected_hash"],
            "orfs_commit": record["orfs_commit"],
            "image_pinned_ref": record["image_pinned_ref"],
            "routed_ok": True,
            "drc_count": 0,
            "unconstrained_paths": 0,
            "wns_ns": 0.0,
            "tns_ns": 0.0,
        },
    )
    assert result.grade.to_dict() == direct.to_dict()


def test_to_grade_result_round_trip(tmp_path):
    from silicon_env.grader import GradeResult

    checkout = make_fake_checkout(tmp_path)
    submission = make_submission(tmp_path, gcd.stock_candidate_config())
    record = make_baseline()
    result = evaluate_clean(submission, checkout, tmp_path, baseline_record=record)
    graded = ev.to_grade_result(result, baseline_record=record)
    assert isinstance(graded, GradeResult)
    graded.validate()
    assert graded.score == 0.5
    assert graded.passed is True
    assert graded.status == GradeStatus.PASS
    assert graded.details["evaluator_provenance"]["evaluator_run_id"].startswith("eval_")
    assert GradeResult.from_json(graded.to_json()).to_dict() == graded.to_dict()


@pytest.mark.skipif(
    not (RUN_REAL_EVAL and REAL_CHECKOUT),
    reason=(
        "blocked on Mac dev host: needs SILICON_RUN_OPENROAD_EVAL=1 plus "
        "ORFS_CHECKOUT at the pinned commit on the Linux route"
    ),
)
def test_real_pinned_end_to_end_opt_in():
    from silicon_env.environments.openroad.preflight import run_preflight

    lock = load_toolchain_lock()
    report = run_preflight(
        lock,
        probe_tool=lambda name: (True, "opt-in"),
        orfs_checkout=REAL_CHECKOUT,
        machine="x86_64",
        system="linux",
        mem_gb=16.0,
        cpu_count=8,
    )
    assert report.ok, report.message()


@pytest.fixture(autouse=True)
def synthetic_checkout_verification(monkeypatch):
    monkeypatch.setattr("silicon_env.environments.openroad.flow.verify_checkout", lambda *a: [])
    monkeypatch.setattr("silicon_env.environments.openroad.sources.verify_checkout", lambda *a: [])
