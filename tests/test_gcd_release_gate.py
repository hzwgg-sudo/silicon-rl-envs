"""M1-10 GCD deterministic release-gate logic (default fast suite, no EDA).

Exercises the same gate logic as the opt-in real-tool E2E
(``tests/integration/test_gcd_e2e.py``) with a fake flow backend and a
fake pinned checkout, so the default suite stays free of EDA tools,
Docker, network, and API keys:

- three fresh scripted episodes with the same seed + actions agree on
  the semantic trace hash, metrics (baseline tolerances), and reward;
- stock vs one legal edit vs one invalid edit (invalid is rejected as
  ``invalid_submission`` with reward ``0.0`` and no workspace mutation);
- independent grading ignores forged agent-visible numbers (a forged
  file *inside* the submission is rejected; a forged report *outside*
  it cannot move the trusted grade);
- budget exhaustion terminates as ``TIMEOUT`` without a new tool call.

Tool-dependent acceptance (real pinned flow) is covered only by the
opt-in gate; this module proves the gate logic itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from silicon_env import trace as trace_mod
from silicon_env.environments.openroad import baseline as bl
from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad import evaluator as ev
from silicon_env.environments.openroad import flow as gcd_flow
from silicon_env.environments.openroad import metrics as gcd_metrics
from silicon_env.environments.openroad.environment import (
    GcdEnvironment,
    stock_candidate_text,
)
from silicon_env.environments.openroad.preflight import load_toolchain_lock
from silicon_env.runner import RunResult
from silicon_env.task import Action
from silicon_env.types import GradeStatus, StepStatus

BASE_AREA = 1000.0
CANDIDATE_AREA = 900.0
TOLS = dict(bl.DEFAULT_TOLERANCES)
GATE_SEED = 7
LEGAL_EDIT = {"PLACE_DENSITY": 0.5, "CORE_UTILIZATION": 60.0}


# --- fakes -----------------------------------------------------------------


def make_fake_checkout(tmp_path: Path) -> Path:
    lock = load_toolchain_lock()
    root = tmp_path / "orfs"
    for rel in lock["required_assets"]:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fake trusted asset\n", encoding="utf-8")
    flow_makefile = root / "flow" / "Makefile"
    flow_makefile.parent.mkdir(parents=True, exist_ok=True)
    if not flow_makefile.is_file():
        flow_makefile.write_text("# fake flow makefile\n", encoding="utf-8")
    return root


def _write_logs(log_dir: Path) -> tuple[Path, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    stdout_path.write_text("fake tool stdout\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    return stdout_path, stderr_path


class FakeRunner:
    """Stub behind the runner protocol (never touches tools)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, tool, args=(), *, cwd, log_dir, env=None, timeout_s=None):
        self.calls.append({"tool": tool, "args": list(args), "cwd": str(cwd)})
        log_path = Path(log_dir)
        stdout_path, stderr_path = _write_logs(log_path)
        return RunResult(
            tool_name=tool,
            argv=tuple(args),
            cwd=Path(cwd),
            log_dir=log_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            exit_code=0,
            status=StepStatus.SUCCESS,
            timed_out=False,
            launched=True,
            duration_s=0.01,
        )


def make_metrics(
    area: float = BASE_AREA,
    wns: float = 0.0,
    tns: float = 0.0,
) -> gcd_metrics.GcdMetrics:
    return gcd_metrics.GcdMetrics(
        stage="final",
        schema="orfs-26Q2-final-v1",
        area_um2=area,
        wns_ns=wns,
        tns_ns=tns,
        routed_ok=True,
        drc_count=0,
        unconstrained_paths=0,
        valid=True,
        reasons=(),
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


def make_flow_result(status: StepStatus, stage: str, message: str) -> gcd_flow.FlowResult:
    return gcd_flow.FlowResult(
        status=status,
        stage_reached=stage,
        artifacts={},
        provenance={"endpoint": "final"},
        message=message,
    )


class FakeFlow:
    """Injectable ``run_flow_fn`` recording every invocation."""

    def __init__(self, result: gcd_flow.FlowResult) -> None:
        self._result = result
        self.calls: list[dict] = []

    def __call__(self, candidate, **kwargs):
        self.calls.append({"candidate": dict(candidate), **kwargs})
        return self._result


def make_env(tmp_path: Path, **overrides):
    checkout = overrides.pop("orfs_checkout", None)
    if checkout is None:
        checkout = make_fake_checkout(tmp_path)
    kwargs = {
        "work_root": tmp_path / "work",
        "orfs_checkout": checkout,
        "baseline_record": make_baseline(),
        "runner": FakeRunner(),
    }
    kwargs.update(overrides)
    if "run_flow_fn" not in kwargs:
        kwargs["run_flow_fn"] = FakeFlow(
            make_flow_result(StepStatus.SUCCESS, "final", "fake final")
        )
    if "parse_fn" not in kwargs:
        flow_obj = kwargs["run_flow_fn"]
        if isinstance(flow_obj, FakeFlow):
            kwargs["parse_fn"] = _candidate_area_parse(flow_obj)
        else:
            kwargs["parse_fn"] = lambda flow_result: make_metrics(area=CANDIDATE_AREA)
    return GcdEnvironment(**kwargs)


def _candidate_area_parse(flow_obj: FakeFlow):
    """Fake trusted parser: stock candidates meter at BASE_AREA, edits at CANDIDATE_AREA.

    The fake flow never measures anything, so the parser derives the
    metered area from the last recorded candidate instead. This keeps
    stock-vs-edit reward comparisons meaningful in the fake world.
    """

    def _parse(flow_result):
        candidate: dict = {}
        calls = getattr(flow_obj, "calls", [])
        if calls:
            candidate = dict(calls[-1].get("candidate", {}))
        try:
            full = gcd.candidate_with_defaults(candidate)
        except Exception:
            full = gcd.stock_candidate_config()
        area = BASE_AREA if full == gcd.stock_candidate_config() else CANDIDATE_AREA
        return make_metrics(area=area)

    return _parse


#: Clock-derived free-text keys, normalized belt-and-braces before hash
#: comparison. The trace semantic projection strips timing *keys* but
#: not clock values inside free text; ``GcdEnvironment`` therefore keeps
#: ``wallclock_s``/``duration_s`` out of agent-visible tails (timing is
#: diagnostic-only by design; see docs/artifacts.md), and the gate
#: additionally normalizes any such text a message may carry.
_CLOCK_TEXT_RE = re.compile(
    r"\b(wallclock_s|duration_s|elapsed_s)=(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
)


def normalize_gate_text(value):
    """Replace clock-derived floats in free text with a stable placeholder."""
    if isinstance(value, dict):
        return {key: normalize_gate_text(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_gate_text(item) for item in value]
    if isinstance(value, str):
        return _CLOCK_TEXT_RE.sub(r"\1=<clock>", value)
    return value


def gate_semantic_hash(events: list) -> str:
    """Semantic hash for gate comparison (clock-normalized, then stable-hashed)."""
    projected = trace_mod.semantic_projection_trace(events)
    return trace_mod.sha256_json(normalize_gate_text(projected))


def act(action_type: str, params: dict | None = None) -> Action:
    return Action(schema_version=1, action_type=action_type, params=dict(params or {}))


def semantic_hash_of(env: GcdEnvironment) -> str:
    run_dir = env.run_dir
    assert run_dir is not None and Path(run_dir).is_dir()
    events = trace_mod.read_events(Path(run_dir) / "trace.jsonl")
    assert len(events) >= 2  # reset + at least one step/submit
    trace_mod.verify_run(run_dir)
    return gate_semantic_hash(events)


def run_scripted_episode(tmp_path: Path, *, seed: int = GATE_SEED) -> dict:
    """Reset + inspect + legal edit + run + submit; return gate evidence."""
    flow = FakeFlow(make_flow_result(StepStatus.SUCCESS, "final", "fake final"))
    env = make_env(tmp_path, run_flow_fn=flow)
    try:
        env.reset(gcd.make_gcd_task(seed=seed))
        read = env.step(act("read_file", {"path": gcd.CANDIDATE_RELPATH}))
        assert read.status == StepStatus.SUCCESS
        write = env.step(
            act(
                "write_file",
                {
                    "path": gcd.CANDIDATE_RELPATH,
                    "content": json.dumps(LEGAL_EDIT),
                },
            )
        )
        assert write.status == StepStatus.SUCCESS
        run = env.step(act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert run.status == StepStatus.SUCCESS
        submitted = env.step(act("submit", {}))
        assert submitted.done and submitted.status == StepStatus.SUCCESS
        grade = env.last_submit_grade
        assert grade is not None
        return {
            "semantic_hash": semantic_hash_of(env),
            "reward": float(submitted.reward),
            "score": float(grade.score),
            "grade_status": grade.status,
            "metrics": {"area_um2": CANDIDATE_AREA, "wns_ns": 0.0, "tns_ns": 0.0},
        }
    finally:
        env.close()


# --- determinism across three fresh episodes ---------------------------------


def test_three_fresh_episodes_agree(tmp_path):
    episodes = [
        run_scripted_episode(tmp_path / f"ep{i}", seed=GATE_SEED) for i in range(3)
    ]
    hashes = {ep["semantic_hash"] for ep in episodes}
    assert len(hashes) == 1, f"semantic trace diverged: {hashes}"
    rewards = {ep["reward"] for ep in episodes}
    assert len(rewards) == 1
    scores = {ep["score"] for ep in episodes}
    assert len(scores) == 1
    for ep in episodes:
        assert ep["grade_status"] == GradeStatus.PASS
        assert bl.within_tolerances(ep["metrics"], episodes[0]["metrics"], TOLS)


def test_different_seed_or_edit_changes_trace(tmp_path):
    first = run_scripted_episode(tmp_path / "ep0", seed=GATE_SEED)
    second = run_scripted_episode(tmp_path / "ep1", seed=GATE_SEED + 1)
    assert first["semantic_hash"] != second["semantic_hash"]


# --- stock vs legal edit vs invalid edit -------------------------------------


def test_stock_scores_half_and_legal_edit_scores_above(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(gcd.make_gcd_task(seed=GATE_SEED))
        stock_grade = env.submit()
        assert stock_grade.status == GradeStatus.PASS
        assert stock_grade.score == pytest.approx(0.5)
    finally:
        env.close()

    flow = FakeFlow(make_flow_result(StepStatus.SUCCESS, "final", "fake final"))
    env = make_env(tmp_path / "legal", run_flow_fn=flow)
    try:
        env.reset(gcd.make_gcd_task(seed=GATE_SEED))
        write = env.step(
            act(
                "write_file",
                {"path": gcd.CANDIDATE_RELPATH, "content": json.dumps(LEGAL_EDIT)},
            )
        )
        assert write.status == StepStatus.SUCCESS
        grade = env.submit()
        assert grade.status == GradeStatus.PASS
        assert grade.score == pytest.approx(
            0.5 + 0.5 * (BASE_AREA - CANDIDATE_AREA) / BASE_AREA
        )
    finally:
        env.close()


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"PLACE_DENSITY": 999.0}),  # out of range
        json.dumps({"PLACE_DENSITY": "0.5; rm -rf /"}),  # injection string
        json.dumps({"CLOCK_PERIOD_NS": 0.5}),  # unknown key (immutable design fact)
        "not json at all",  # malformed
    ],
)
def test_invalid_edit_rejected_with_zero_reward(tmp_path, content):
    env = make_env(tmp_path)
    try:
        env.reset(gcd.make_gcd_task(seed=GATE_SEED))
        result = env.step(
            act("write_file", {"path": gcd.CANDIDATE_RELPATH, "content": content})
        )
        assert result.status == StepStatus.INVALID_SUBMISSION
        assert float(result.reward) == 0.0
        assert not result.done
        assert env.workspace is not None
        assert env.workspace.read_text(gcd.CANDIDATE_RELPATH) == stock_candidate_text()
    finally:
        env.close()


def test_invalid_candidate_grades_invalid_with_zero_reward(tmp_path):
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / gcd.CANDIDATE_RELPATH).write_text(
        json.dumps({"PLACE_DENSITY": 999.0}), encoding="utf-8"
    )
    result = ev.evaluate_submission(
        str(submission),
        orfs_checkout=str(make_fake_checkout(tmp_path)),
        eval_root=str(tmp_path / "eval_root"),
        runner=FakeRunner(),
        baseline_record=make_baseline(),
        seed=GATE_SEED,
        timeout_s=60.0,
        run_flow_fn=FakeFlow(
            make_flow_result(StepStatus.SUCCESS, "final", "must not run")
        ),
        parse_fn=lambda flow_result: make_metrics(),
    )
    assert result.status == GradeStatus.INVALID_SUBMISSION
    assert result.reward == 0.0


# --- independent grading: tampering cannot improve the grade -----------------


def _evaluate_clean(submission: Path, checkout: Path, tmp_path: Path, **overrides):
    kwargs = {
        "orfs_checkout": str(checkout),
        "eval_root": str(tmp_path / "eval_root"),
        "runner": FakeRunner(),
        "baseline_record": make_baseline(),
        "seed": GATE_SEED,
        "timeout_s": 60.0,
        "run_flow_fn": FakeFlow(
            make_flow_result(StepStatus.SUCCESS, "final", "fake final")
        ),
        "parse_fn": lambda flow_result: make_metrics(area=CANDIDATE_AREA),
    }
    kwargs.update(overrides)
    return ev.evaluate_submission(str(submission), **kwargs)


def test_forged_report_outside_submission_cannot_move_grade(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    submission = tmp_path / "clean-sub"
    submission.mkdir()
    (submission / gcd.CANDIDATE_RELPATH).write_text(
        gcd.dumps_candidate_json(gcd.stock_candidate_config()), encoding="utf-8"
    )
    forged_area = CANDIDATE_AREA * 0.01
    agent_dir = tmp_path / "agent-workspace"
    agent_dir.mkdir()
    (agent_dir / "report.json").write_text(
        json.dumps({"area_um2": forged_area, "wns_ns": 0.0}) + "\n", encoding="utf-8"
    )
    first = _evaluate_clean(submission, checkout, tmp_path)
    second = _evaluate_clean(submission, checkout, tmp_path)
    assert first.reward == second.reward == pytest.approx(
        0.5 + 0.5 * (BASE_AREA - CANDIDATE_AREA) / BASE_AREA
    )
    assert first.reason_codes == second.reason_codes == ("ok",)
    assert str(forged_area) not in json.dumps(first.to_dict())


def test_forged_file_inside_submission_is_rejected_not_scored(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    submission = tmp_path / "dirty-sub"
    submission.mkdir()
    (submission / gcd.CANDIDATE_RELPATH).write_text(
        gcd.dumps_candidate_json(gcd.stock_candidate_config()), encoding="utf-8"
    )
    forged_area = CANDIDATE_AREA * 0.01
    (submission / "report.json").write_text(
        json.dumps({"area_um2": forged_area}) + "\n", encoding="utf-8"
    )
    result = _evaluate_clean(submission, checkout, tmp_path)
    assert result.status == GradeStatus.INVALID_SUBMISSION
    assert result.reward == 0.0
    assert "unauthorized-file" in result.reason_codes
    assert str(forged_area) not in json.dumps(result.to_dict())


# --- budget exhaustion: no extra tool call -----------------------------------


def test_tiny_budget_times_out_without_extra_tool_call(tmp_path):
    flow = FakeFlow(make_flow_result(StepStatus.SUCCESS, "final", "must not run"))
    runner = FakeRunner()
    env = make_env(
        tmp_path,
        run_flow_fn=flow,
        runner=runner,
        orfs_checkout=tmp_path / "untouched",
    )
    try:
        env.reset(gcd.make_gcd_task(max_steps=1, max_tool_calls=1))
        first = env.step(act("read_file", {"path": gcd.CANDIDATE_RELPATH}))
        assert first.status == StepStatus.SUCCESS
        second = env.step(act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert second.status == StepStatus.TIMEOUT
        assert second.done
        assert flow.calls == []
        assert runner.calls == []
    finally:
        env.close()
