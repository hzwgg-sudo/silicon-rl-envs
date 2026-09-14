"""M1-09 deterministic GCD baseline agents tests.

Lightweight by design: no EDA tools, Docker, network, or API keys. The
flow backend is always a fake stub whose observation messages embed
scripted ``area: <N>`` numbers (the real flow summary carries no area,
so the search falls back to its stable density/utilization/index
tie-breaker there). The submit-time evaluator re-runs through injected
fake ``parse_fn`` callables against a fake pinned checkout.

The real pinned GCD episode for both agents is opt-in and skipped by
default (``SILICON_RUN_OPENROAD_ENV=1`` with ``ORFS_CHECKOUT`` set);
it is blocked on the Mac dev host, which is recorded -- never
fabricated.
"""

import os
from pathlib import Path

import pytest

from silicon_env.agents.baseline import NOOP_MAX_ACTIONS, run_noop_agent
from silicon_env.agents.openroad_search import (
    SEARCH_MAX_CANDIDATES,
    SEARCH_MAX_TOOL_CALLS,
    parse_observed_metrics,
    run_search_agent,
    search_candidates,
)
from silicon_env.environments.openroad import baseline as bl
from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad import flow as gcd_flow
from silicon_env.environments.openroad.environment import (
    GcdEnvironment,
    stock_candidate_text,
)
from silicon_env.environments.openroad.metrics import GcdMetrics
from silicon_env.environments.openroad.preflight import load_toolchain_lock
from silicon_env.runner import RunResult
from silicon_env.types import GradeStatus, StepStatus

RUN_REAL_ENV = os.environ.get("SILICON_RUN_OPENROAD_ENV", "") == "1"
REAL_CHECKOUT = os.environ.get("ORFS_CHECKOUT", "").strip()

BASE_AREA = 1000.0
TOLS = {"area_rel": 0.01, "wns_abs_ns": 0.005, "tns_abs_ns": 0.01}


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


def make_metrics(area: float = BASE_AREA) -> GcdMetrics:
    return GcdMetrics(
        stage="final",
        schema="orfs-26Q2-final-v1",
        area_um2=area,
        wns_ns=0.0,
        tns_ns=0.0,
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


class ScriptedFlow:
    """Fake ``run_flow_fn``: per-call areas embedded in the message.

    ``areas[i]`` is the observed area for the i-th flow call (extra
    calls reuse the last entry); ``None`` means a tool failure at the
    place stage (infeasible candidate).
    """

    def __init__(self, areas: list[float | None]) -> None:
        assert areas, "script at least one flow outcome"
        self._areas = list(areas)
        self.calls: list[dict] = []

    def __call__(self, candidate, **kwargs):
        index = len(self.calls)
        self.calls.append({"candidate": dict(candidate), **kwargs})
        area = self._areas[min(index, len(self._areas) - 1)]
        if area is None:
            return gcd_flow.FlowResult(
                status=StepStatus.TOOL_FAILURE,
                stage_reached="place",
                artifacts={},
                provenance={"endpoint": "final"},
                message="fake route fail",
            )
        return gcd_flow.FlowResult(
            status=StepStatus.SUCCESS,
            stage_reached="final",
            artifacts={},
            provenance={"endpoint": "final"},
            message=f"fake final area: {area} um^2 routed_completion: true",
        )


def make_env(tmp_path: Path, flow: ScriptedFlow, **overrides):
    kwargs = {
        "work_root": tmp_path / "work",
        "orfs_checkout": make_fake_checkout(tmp_path),
        "baseline_record": make_baseline(),
        "runner": FakeRunner(),
        "run_flow_fn": flow,
        "parse_fn": lambda flow_result: make_metrics(area=BASE_AREA),
    }
    kwargs.update(overrides)
    return GcdEnvironment(**kwargs)


def probe_calls(flow: ScriptedFlow) -> list[dict]:
    """Agent-driven flow probes, excluding the trusted evaluator re-run.

    ``submit()`` re-runs the winner through the same ``run_flow_fn`` in
    a fresh scratch under ``eval_runs/``; that re-run is trusted grading
    work (it consumes no agent step/tool budget), not an agent probe.
    """
    return [c for c in flow.calls if "eval_runs" not in str(c.get("scratch_dir", ""))]


# --- grid / parser unit tests --------------------------------------------------


def test_search_grid_has_four_legal_pairs():
    grid = search_candidates()
    assert len(grid) == SEARCH_MAX_CANDIDATES
    assert len(grid) <= 4
    for entry in grid:
        gcd.validate_candidate_config(entry)
    assert grid[0] == gcd.stock_candidate_config()


def test_parse_observed_metrics_area_ordering():
    assert parse_observed_metrics("area: 900 um^2")["area_um2"] == 900.0
    assert parse_observed_metrics("cell_area: 850")["area_um2"] == 850.0
    assert parse_observed_metrics("no numbers here")["area_um2"] is None
    assert parse_observed_metrics("area: nan")["area_um2"] is None


# --- no-op baseline ------------------------------------------------------------


def test_noop_submits_stock_with_trusted_baseline_score(tmp_path):
    flow = ScriptedFlow([BASE_AREA])
    env = make_env(tmp_path, flow)
    try:
        result = run_noop_agent(env, gcd.make_gcd_task(seed=7), 7)
        assert len(result.actions) <= NOOP_MAX_ACTIONS
        assert result.tool_calls_used == 0
        assert probe_calls(flow) == []
        assert result.candidate_text == stock_candidate_text()
        assert result.grade is not None
        assert result.grade.status == GradeStatus.PASS
        assert result.grade.score == pytest.approx(0.5)
        assert env.last_submit_grade is not None
        assert env.last_submit_grade.score == pytest.approx(0.5)
    finally:
        env.close()


def test_noop_trace_recorded(tmp_path):
    flow = ScriptedFlow([BASE_AREA])
    env = make_env(tmp_path, flow)
    try:
        result = run_noop_agent(env, gcd.make_gcd_task(seed=7), 7)
        assert result.grade is not None
        assert env.trace_path is not None
        assert Path(env.trace_path).is_file()
    finally:
        env.close()


def test_noop_determinism_same_seed_same_trace(tmp_path):
    def _once(path: Path):
        flow = ScriptedFlow([BASE_AREA])
        env = make_env(path, flow)
        try:
            return run_noop_agent(env, gcd.make_gcd_task(seed=11), 11)
        finally:
            env.close()

    first = _once(tmp_path / "a")
    second = _once(tmp_path / "b")
    assert first.actions == second.actions
    assert first.candidate_text == second.candidate_text
    assert first.score == pytest.approx(second.score)


# --- bounded search ------------------------------------------------------------


def test_search_selects_smallest_observed_area(tmp_path):
    flow = ScriptedFlow([1000.0, 900.0, 950.0, 975.0])
    env = make_env(tmp_path, flow)
    try:
        result = run_search_agent(env, gcd.make_gcd_task(seed=7), 7)
        assert result.best_index == 1
        assert result.submitted_candidate["PLACE_DENSITY"] == pytest.approx(0.20)
        assert len(probe_calls(flow)) <= SEARCH_MAX_TOOL_CALLS
        assert result.grade is not None
        assert result.grade.validate() is None
    finally:
        env.close()


def test_search_tie_break_prefers_lower_density_then_utilization(tmp_path):
    grid = (
        {"PLACE_DENSITY": 0.30, "CORE_UTILIZATION": 55.0},
        {"PLACE_DENSITY": 0.50, "CORE_UTILIZATION": 40.0},
        {"PLACE_DENSITY": 0.30, "CORE_UTILIZATION": 40.0},
        {"PLACE_DENSITY": 0.50, "CORE_UTILIZATION": 60.0},
    )
    flow = ScriptedFlow([900.0, 850.0, 850.0, 850.0])
    env = make_env(tmp_path, flow)
    try:
        result = run_search_agent(env, gcd.make_gcd_task(seed=7), 7, candidates=grid)
        # Indices 1-3 tie on area: lowest density (0.30, index 2) wins over 0.50.
        assert result.best_index == 2
        assert result.submitted_candidate["CORE_UTILIZATION"] == pytest.approx(40.0)
    finally:
        env.close()


def test_search_tie_break_earliest_index_wins_full_tie(tmp_path):
    grid = (
        {"PLACE_DENSITY": 0.40, "CORE_UTILIZATION": 50.0},
        {"PLACE_DENSITY": 0.40, "CORE_UTILIZATION": 50.0},
        {"PLACE_DENSITY": 0.60, "CORE_UTILIZATION": 60.0},
        {"PLACE_DENSITY": 0.60, "CORE_UTILIZATION": 70.0},
    )
    flow = ScriptedFlow([900.0, 900.0, 950.0, 950.0])
    env = make_env(tmp_path, flow)
    try:
        result = run_search_agent(env, gcd.make_gcd_task(seed=7), 7, candidates=grid)
        assert result.best_index == 0
    finally:
        env.close()


def test_search_determinism_same_seed_same_selection(tmp_path):
    def _once(path: Path):
        flow = ScriptedFlow([1000.0, 900.0, 950.0, 975.0])
        env = make_env(path, flow)
        try:
            return run_search_agent(env, gcd.make_gcd_task(seed=7), 7)
        finally:
            env.close()

    first = _once(tmp_path / "a")
    second = _once(tmp_path / "b")
    assert first.actions == second.actions
    assert first.best_index == second.best_index
    assert first.submitted_candidate == second.submitted_candidate


def test_search_all_invalid_submits_stock_honestly(tmp_path):
    flow = ScriptedFlow([None, None, None, None])
    env = make_env(tmp_path, flow)
    try:
        result = run_search_agent(env, gcd.make_gcd_task(seed=7), 7)
        assert all(not rec.feasible for rec in result.records)
        assert result.best_index == 0
        assert result.submitted_candidate == gcd.stock_candidate_config()
        assert result.improved is False
        assert result.grade is not None
        # Honest report: the trusted grade stands as-is, no improvement claimed.
        assert result.score == pytest.approx(result.grade.score)
    finally:
        env.close()


def test_search_budget_exhaustion_stops_within_caps(tmp_path):
    flow = ScriptedFlow([1000.0, 900.0, 950.0, 975.0])
    env = make_env(tmp_path, flow)
    try:
        result = run_search_agent(
            env, gcd.make_gcd_task(seed=7, max_steps=5, max_tool_calls=5), 7
        )
        assert len(probe_calls(flow)) <= SEARCH_MAX_TOOL_CALLS
        assert len(probe_calls(flow)) <= 1
        assert result.budget_limited is True
        assert result.grade is not None
        assert env.budget_tracker is not None
        assert env.budget_tracker.steps_used <= 5
    finally:
        env.close()


def test_search_tiny_budget_submits_without_probing(tmp_path):
    flow = ScriptedFlow([1000.0, 900.0, 950.0, 975.0])
    env = make_env(tmp_path, flow)
    try:
        result = run_search_agent(
            env, gcd.make_gcd_task(seed=7, max_steps=3, max_tool_calls=3), 7
        )
        assert probe_calls(flow) == []
        assert result.records == ()
        assert result.submitted_candidate == gcd.stock_candidate_config()
        assert result.grade is not None
    finally:
        env.close()


def test_search_respects_tool_call_cap(tmp_path):
    flow = ScriptedFlow([1000.0, 900.0, 950.0, 975.0])
    env = make_env(tmp_path, flow)
    try:
        result = run_search_agent(
            env, gcd.make_gcd_task(seed=7, max_steps=10, max_tool_calls=10), 7
        )
        assert len(probe_calls(flow)) == SEARCH_MAX_TOOL_CALLS
        assert env.budget_tracker is not None
        assert env.budget_tracker.tool_calls_used <= SEARCH_MAX_TOOL_CALLS
        assert env.budget_tracker.steps_used <= 10
        _ = result
    finally:
        env.close()


def test_search_rejects_oversized_grid(tmp_path):
    flow = ScriptedFlow([1000.0])
    env = make_env(tmp_path, flow)
    try:
        with pytest.raises(ValueError, match="cap"):
            run_search_agent(
                env,
                gcd.make_gcd_task(seed=7),
                7,
                candidates=(
                    {"PLACE_DENSITY": 0.3, "CORE_UTILIZATION": 55.0},
                    {"PLACE_DENSITY": 0.4, "CORE_UTILIZATION": 55.0},
                    {"PLACE_DENSITY": 0.5, "CORE_UTILIZATION": 55.0},
                    {"PLACE_DENSITY": 0.6, "CORE_UTILIZATION": 55.0},
                    {"PLACE_DENSITY": 0.7, "CORE_UTILIZATION": 55.0},
                ),
            )
    finally:
        env.close()


# --- real pinned profile (opt-in, blocked on Mac) --------------------------------


@pytest.mark.skipif(
    not (RUN_REAL_ENV and REAL_CHECKOUT),
    reason=(
        "blocked on Mac dev host: needs SILICON_RUN_OPENROAD_ENV=1 plus "
        "ORFS_CHECKOUT at the pinned commit on the Linux route"
    ),
)
def test_real_pinned_baselines_opt_in(tmp_path):
    import json as _json

    baseline_path = gcd.TASK_DIR / "baseline.json"
    baseline_record = _json.loads(baseline_path.read_text(encoding="utf-8"))
    from silicon_env.environments.openroad.environment import default_flow_runner

    noop_env = GcdEnvironment(
        work_root=tmp_path / "noop",
        runner=default_flow_runner(),
        orfs_checkout=REAL_CHECKOUT,
        baseline_record=baseline_record,
    )
    try:
        noop_result = run_noop_agent(noop_env, gcd.make_gcd_task(seed=0), 0)
        assert noop_result.grade is not None
        noop_result.grade.validate()
    finally:
        noop_env.close()

    search_env = GcdEnvironment(
        work_root=tmp_path / "search",
        runner=default_flow_runner(),
        orfs_checkout=REAL_CHECKOUT,
        baseline_record=baseline_record,
    )
    try:
        search_result = run_search_agent(search_env, gcd.make_gcd_task(seed=0), 0)
        assert search_result.grade is not None
        search_result.grade.validate()
        # Improvement is recorded, not required: the pinned run may show
        # no gain over stock on this host/profile.
        _ = (noop_result.score, search_result.score, search_result.improved)
    finally:
        search_env.close()
