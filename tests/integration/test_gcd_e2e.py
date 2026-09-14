"""M1-10 GCD deterministic release gate on the real pinned toolchain (opt-in).

Everything here is skipped by default. Enable on a provisioned Linux
route only::

    export ORFS_CHECKOUT=/path/to/OpenROAD-flow-scripts  # pinned commit
    export SILICON_RUN_GCD_E2E=1
    pytest tests/integration/test_gcd_e2e.py -v

Optional: ``SILICON_GCD_BASELINE=/path/to/baseline.json`` points at a
verified record produced by
``scripts/generate_openroad_baseline.py`` (the workflow generates one
before this gate). Without it the packaged ``baseline.json`` is used;
when that record is still ``TBD-unverified``, grade-dependent
assertions fail closed and the suite reports the blocker instead of
claiming success.

When enabled, the gate runs three fresh :class:`GcdEnvironment`
episodes with the same seed + the same scripted actions
(reset/inspect/legal-edit/run/submit) on the pinned profile and
checks:

1. identical semantic trace hashes (see ``gate_semantic_hash`` in
   ``tests/test_gcd_release_gate.py``: the environment keeps
   clock-derived text out of agent-visible tails, and the gate
   additionally normalizes any such text defensively before hashing);
2. parsed final-stage metrics pairwise within the declared baseline
   tolerances;
3. equal trusted rewards;
4. stock vs one legal edit vs one invalid edit (invalid is rejected as
   ``invalid_submission`` with reward ``0.0`` and no mutation);
5. independent grading: a forged agent-visible report cannot move the
   trusted grade, and a forged file inside the submission is rejected;
6. budget exhaustion terminates as ``TIMEOUT`` without launching a new
   flow.

Real pinned-tool runs are blocked on the Mac dev host (arm64, no
EDA/Linux/Docker daemon); the default fast suite covers the same
gate logic with fakes in ``tests/test_gcd_release_gate.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import replace
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
    default_flow_runner,
)
from silicon_env.task import Action
from silicon_env.types import GradeStatus, StepStatus

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_HELPERS_PATH = REPO_ROOT / "tests" / "test_gcd_release_gate.py"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_openroad_baseline.py"

RUN_GATE = os.environ.get("SILICON_RUN_GCD_E2E", "") == "1"
CHECKOUT = os.environ.get("ORFS_CHECKOUT", "").strip()
BASELINE_OVERRIDE = os.environ.get("SILICON_GCD_BASELINE", "").strip()

GATE_SEED = int(os.environ.get("GATE_SEED", "7"))
LEGAL_EDIT = {"PLACE_DENSITY": 0.5, "CORE_UTILIZATION": 55.0}

# Real-flow budgets: the pinned GCD flow runs for tens of minutes, far
# beyond the default task budgets. These stay explicit and bounded; the
# workflow caps the whole job with timeout-minutes on top.
REAL_WALLCLOCK_S = 14400.0
REAL_GRADER_TIMEOUT_S = float(os.environ.get("GATE_TIMEOUT_S", "7200"))

NEEDS_GATE = pytest.mark.skipif(
    not RUN_GATE,
    reason=(
        "opt-in release gate: set SILICON_RUN_GCD_E2E=1 plus ORFS_CHECKOUT "
        "at the pinned commit on the Linux route (blocked on Mac dev host)"
    ),
)


def _load_gate_helpers():
    """Reuse the clock-normalized gate hash from the default fast suite."""
    spec = importlib.util.spec_from_file_location(
        "gcd_release_gate_helpers", GATE_HELPERS_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_generator():
    """Reuse best-effort final-report discovery from the baseline generator."""
    spec = importlib.util.spec_from_file_location(
        "generate_openroad_baseline", GENERATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _require_checkout() -> Path:
    if not RUN_GATE:
        pytest.skip(
            "opt-in release gate: set SILICON_RUN_GCD_E2E=1 plus ORFS_CHECKOUT "
            "at the pinned commit on the Linux route"
        )
    if not CHECKOUT:
        pytest.fail("enabled GCD gate requires ORFS_CHECKOUT")
    root = Path(CHECKOUT)
    if not root.is_dir():
        pytest.fail(f"ORFS_CHECKOUT is not a directory: {root}")
    if not (root / "flow" / "Makefile").is_file():
        pytest.fail(
            f"ORFS_CHECKOUT {root} has no flow/Makefile; pass the pinned "
            f"ORFS checkout ({gcd.ORFS_COMMIT[:12]}...)"
        )
    return root


def _load_baseline_record() -> dict:
    if BASELINE_OVERRIDE:
        return bl.load_baseline(BASELINE_OVERRIDE)
    return bl.load_baseline(gcd.TASK_DIR / "baseline.json")


def _real_task(seed: int, **overrides):
    """Task with generous but bounded real-flow budgets."""
    task = gcd.make_gcd_task(
        seed=seed,
        max_steps=overrides.get("max_steps", 10),
        max_wallclock_s=overrides.get("max_wallclock_s", REAL_WALLCLOCK_S),
        max_tool_calls=overrides.get("max_tool_calls", 10),
    )
    timeout_s = overrides.get("grader_timeout_s", REAL_GRADER_TIMEOUT_S)
    task = replace(task, grader=replace(task.grader, timeout_s=timeout_s))
    task.validate()
    return task


def _discovery_parse_fn():
    """Use the same trusted report parser as the production evaluator."""
    from silicon_env.environments.openroad.reports import parse_generated_reports

    return parse_generated_reports


def _make_real_env(work_root: Path, checkout: Path, baseline_record: dict, **overrides):
    kwargs = {
        "work_root": work_root,
        "runner": default_flow_runner(),
        "orfs_checkout": checkout,
        "baseline_record": dict(baseline_record),
        "parse_fn": _discovery_parse_fn(),
    }
    kwargs.update(overrides)
    return GcdEnvironment(**kwargs)


def _act(action_type: str, params: dict | None = None) -> Action:
    return Action(schema_version=1, action_type=action_type, params=dict(params or {}))


def _run_scripted_episode(
    parent: Path, name: str, checkout: Path, baseline_record: dict, *, seed: int = GATE_SEED
) -> dict:
    """One fresh reset/inspect/legal-edit/run/submit episode on real tools."""
    helpers = _load_gate_helpers()
    captured: list = []
    real_run_flow = gcd_flow.run_gcd_flow

    def _recording_run_flow(candidate, **kwargs):
        result = real_run_flow(candidate, **kwargs)
        captured.append(result)
        return result

    parse_fn = _discovery_parse_fn()
    env = _make_real_env(
        parent / name,
        checkout,
        baseline_record,
        run_flow_fn=_recording_run_flow,
        parse_fn=parse_fn,
    )
    try:
        env.reset(_real_task(seed))
        read = env.step(_act("read_file", {"path": gcd.CANDIDATE_RELPATH}))
        assert read.status == StepStatus.SUCCESS, f"inspect failed: {read.message}"
        write = env.step(
            _act(
                "write_file",
                {"path": gcd.CANDIDATE_RELPATH, "content": json.dumps(LEGAL_EDIT)},
            )
        )
        assert write.status == StepStatus.SUCCESS, f"legal edit failed: {write.message}"
        run = env.step(_act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert run.status in (
            StepStatus.SUCCESS,
            StepStatus.TOOL_FAILURE,
            StepStatus.TIMEOUT,
            StepStatus.INFRA_ERROR,
        ), f"unexpected flow status: {run.status}"
        grade = None
        if not env.done:
            grade = env.submit()
        else:
            grade = env.last_submit_grade
        assert grade is not None, "episode ended without a grade"
        run_dir = env.run_dir
        assert run_dir is not None and Path(run_dir).is_dir()
        events = trace_mod.read_events(Path(run_dir) / "trace.jsonl")
        trace_mod.verify_run(run_dir)
        parsed: list[gcd_metrics.GcdMetrics] = []
        for flow_result in captured:
            try:
                parsed.append(parse_fn(flow_result))
            except Exception:
                pass
        return {
            "semantic_hash": helpers.gate_semantic_hash(events),
            "grade": grade,
            "reward": float(grade.score),
            "parsed": parsed,
            "run_dir": str(run_dir),
        }
    finally:
        env.close()


# --- three-episode determinism -------------------------------------------------


@NEEDS_GATE
def test_three_episodes_agree_on_trace_metrics_and_reward(tmp_path):
    checkout = _require_checkout()
    baseline_record = _load_baseline_record()
    try:
        bl.validate_baseline(baseline_record)
        baseline_verified = True
    except bl.BaselineError as exc:
        baseline_verified = False
        baseline_note = str(exc)
    episodes = [
        _run_scripted_episode(tmp_path, f"ep{i}", checkout, baseline_record)
        for i in range(3)
    ]
    hashes = {ep["semantic_hash"] for ep in episodes}
    assert len(hashes) == 1, f"semantic trace diverged across runs: {hashes}"
    rewards = {ep["reward"] for ep in episodes}
    assert len(rewards) == 1, f"reward diverged across runs: {rewards}"
    for index, ep in enumerate(episodes):
        assert ep["grade"].passed, ep["grade"].message
        assert ep["parsed"], (
            f"run {index} produced no parseable final metrics; "
            "report discovery found nothing usable (fail closed)"
        )
        for metric in ep["parsed"]:
            assert metric.valid, (
                f"run {index} metrics invalid: {metric.reasons}; "
                "a real pinned run must yield valid final-stage metrics"
            )
    reference = {
        "area_um2": episodes[0]["parsed"][-1].area_um2,
        "wns_ns": episodes[0]["parsed"][-1].wns_ns,
        "tns_ns": episodes[0]["parsed"][-1].tns_ns,
    }
    tols = (
        dict(baseline_record["tolerances"])
        if baseline_verified
        else dict(bl.DEFAULT_TOLERANCES)
    )
    for index in (1, 2):
        candidate = {
            "area_um2": episodes[index]["parsed"][-1].area_um2,
            "wns_ns": episodes[index]["parsed"][-1].wns_ns,
            "tns_ns": episodes[index]["parsed"][-1].tns_ns,
        }
        assert bl.within_tolerances(candidate, reference, tols), (
            f"run {index} drifts outside tolerances vs run 0: "
            f"{candidate!r} vs {reference!r}"
        )
    if not baseline_verified:
        pytest.fail(
            "trace/metrics/reward agree across runs, but the packaged "
            f"baseline is not verified ({baseline_note}); generate a "
            "verified record for scoring use"
        )


# --- stock vs legal edit vs invalid edit ---------------------------------------


@NEEDS_GATE
def test_stock_legal_and_invalid_edits(tmp_path):
    checkout = _require_checkout()
    baseline_record = _load_baseline_record()

    env = _make_real_env(tmp_path / "stock", checkout, baseline_record)
    try:
        env.reset(_real_task(GATE_SEED))
        grade = env.submit()
        grade.validate()
        assert grade.passed, grade.message
        stock_reward = float(grade.score)
    finally:
        env.close()

    legal = _run_scripted_episode(tmp_path, "legal", checkout, baseline_record)
    assert legal["grade"] is not None

    env = _make_real_env(tmp_path / "invalid", checkout, baseline_record)
    try:
        env.reset(_real_task(GATE_SEED))
        bad = env.step(
            _act(
                "write_file",
                {
                    "path": gcd.CANDIDATE_RELPATH,
                    "content": json.dumps({"PLACE_DENSITY": 999.0}),
                },
            )
        )
        assert bad.status == StepStatus.INVALID_SUBMISSION
        assert float(bad.reward) == 0.0
        assert not bad.done
        assert env.workspace is not None
        current = env.workspace.read_text(gcd.CANDIDATE_RELPATH)
        assert json.loads(current) == gcd.stock_candidate_config(), (
            "invalid edit must not mutate the workspace"
        )
    finally:
        env.close()

    assert stock_reward > 0.0
    assert float(legal["reward"]) >= 0.0


# --- independent grading: tampering cannot improve the grade -------------------


@NEEDS_GATE
def test_forged_reports_cannot_move_trusted_grade(tmp_path):
    checkout = _require_checkout()
    baseline_record = _load_baseline_record()

    def _evaluate(submission: Path):
        return ev.evaluate_submission(
            str(submission),
            orfs_checkout=str(checkout),
            eval_root=str(tmp_path / "eval_root"),
            runner=default_flow_runner(),
            baseline_record=dict(baseline_record),
            seed=GATE_SEED,
            timeout_s=REAL_GRADER_TIMEOUT_S,
            parse_fn=_discovery_parse_fn(),
        )

    clean = tmp_path / "clean-sub"
    clean.mkdir()
    (clean / gcd.CANDIDATE_RELPATH).write_text(
        gcd.dumps_candidate_json(gcd.stock_candidate_config()), encoding="utf-8"
    )
    agent_dir = tmp_path / "agent-workspace"
    agent_dir.mkdir()
    forged_area = 1.0
    (agent_dir / "report.json").write_text(
        json.dumps({"area_um2": forged_area}) + "\n", encoding="utf-8"
    )
    first = _evaluate(clean)
    second = _evaluate(clean)
    assert first.reward == second.reward
    assert first.reason_codes == second.reason_codes
    assert str(forged_area) not in json.dumps(first.to_dict())

    dirty = tmp_path / "dirty-sub"
    dirty.mkdir()
    (dirty / gcd.CANDIDATE_RELPATH).write_text(
        gcd.dumps_candidate_json(gcd.stock_candidate_config()), encoding="utf-8"
    )
    (dirty / "report.json").write_text(
        json.dumps({"area_um2": forged_area}) + "\n", encoding="utf-8"
    )
    rejected = _evaluate(dirty)
    assert rejected.status == GradeStatus.INVALID_SUBMISSION
    assert rejected.reward == 0.0
    assert rejected.reward <= first.reward


# --- budget exhaustion: no extra tool call -------------------------------------


@NEEDS_GATE
def test_tiny_budget_times_out_without_new_tool_call(tmp_path):
    checkout = _require_checkout()
    baseline_record = _load_baseline_record()
    calls: list = []
    real_run_flow = gcd_flow.run_gcd_flow

    def _counting_run_flow(candidate, **kwargs):
        calls.append(dict(candidate))
        return real_run_flow(candidate, **kwargs)

    env = _make_real_env(
        tmp_path / "work",
        checkout,
        baseline_record,
        run_flow_fn=_counting_run_flow,
    )
    try:
        env.reset(_real_task(GATE_SEED, max_steps=1, max_tool_calls=1))
        first = env.step(_act("read_file", {"path": gcd.CANDIDATE_RELPATH}))
        assert first.status == StepStatus.SUCCESS
        second = env.step(_act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert second.status == StepStatus.TIMEOUT
        assert second.done
        assert calls == []
    finally:
        env.close()


@NEEDS_GATE
def test_real_noop_and_bounded_search_agents(tmp_path):
    from silicon_env.agents.baseline import run_noop_agent
    from silicon_env.agents.openroad_search import run_search_agent

    checkout = _require_checkout()
    baseline_record = _load_baseline_record()
    outcomes = {}
    for name, agent in (("noop", run_noop_agent), ("search", run_search_agent)):
        env = _make_real_env(tmp_path / name, checkout, baseline_record)
        try:
            result = agent(env, _real_task(GATE_SEED), GATE_SEED)
            assert result.grade is not None
            assert result.grade.passed, result.grade.message
            outcomes[name] = {"score": result.score, "actions": list(result.actions)}
            if name == "search":
                outcomes[name]["improved"] = result.improved
                outcomes[name]["candidate"] = result.submitted_candidate
            else:
                assert result.tool_calls_used == 0
                assert result.score == pytest.approx(0.5, abs=0.005)
        finally:
            env.close()
    (tmp_path / "agent-results.json").write_text(json.dumps(outcomes, indent=2) + "\n")
