"""M1-08 GCD interactive environment tests.

Lightweight by design: no EDA tools, Docker, network, or API keys. The
flow backend is always a fake stub (``run_flow_fn``), the submit-time
evaluator re-runs through injected fake ``parse_fn`` callables against
a fake pinned checkout, and baselines are built from fake
:class:`GcdMetrics`.

The one real scripted GCD episode is opt-in and skipped by default
(``SILICON_RUN_OPENROAD_ENV=1`` with ``ORFS_CHECKOUT`` set); it is
blocked on the Mac dev host, which is recorded -- never fabricated.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from silicon_env.environment import EnvironmentError
from silicon_env.environments.openroad import baseline as bl
from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad import flow as gcd_flow
from silicon_env.environments.openroad.environment import (
    GcdEnvironment,
    ensure_gcd_template,
    stock_candidate_text,
)
from silicon_env.environments.openroad.metrics import GcdMetrics
from silicon_env.environments.openroad.preflight import load_toolchain_lock
from silicon_env.runner import RunResult
from silicon_env.task import Action
from silicon_env.types import GradeStatus, StepStatus

RUN_REAL_ENV = os.environ.get("SILICON_RUN_OPENROAD_ENV", "") == "1"
REAL_CHECKOUT = os.environ.get("ORFS_CHECKOUT", "").strip()

BASE_AREA = 1000.0
CANDIDATE_AREA = 900.0
TOLS = {"area_rel": 0.01, "wns_abs_ns": 0.005, "tns_abs_ns": 0.01}

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = REPO_ROOT / "scripts" / "run_task.py"
GRADE_SCRIPT = REPO_ROOT / "scripts" / "grade_task.py"


# --- fakes -----------------------------------------------------------------


def make_fake_checkout(tmp_path: Path) -> Path:
    """Pinned-checkout stand-in carrying every lockfile required asset."""
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
    """Stub behind the runner protocol (flow backend / evaluator backend)."""

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
    """Build a GCD env with fake flow backend + fake checkout + baseline."""
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
        kwargs["parse_fn"] = lambda flow_result: make_metrics(area=CANDIDATE_AREA)
    return GcdEnvironment(**kwargs)


def act(action_type: str, params: dict | None = None) -> Action:
    return Action(schema_version=1, action_type=action_type, params=dict(params or {}))


def stock_text() -> str:
    return stock_candidate_text()


# --- template / reset --------------------------------------------------------


def test_template_holds_stock_candidate(tmp_path):
    template = ensure_gcd_template(tmp_path / "template")
    assert (template / gcd.CANDIDATE_RELPATH).read_text(encoding="utf-8") == stock_text()


def test_reset_seeds_stock_candidate_and_reports_it(tmp_path):
    env = make_env(tmp_path)
    try:
        obs = env.reset(gcd.make_gcd_task(seed=7))
        assert obs.step_index == 0
        assert gcd.CANDIDATE_RELPATH in obs.stdout_tail
        assert env.workspace is not None
        assert env.workspace.read_text(gcd.CANDIDATE_RELPATH) == stock_text()
    finally:
        env.close()


# --- scripted happy path -----------------------------------------------------


def test_scripted_reset_inspect_edit_run_submit(tmp_path):
    flow = FakeFlow(make_flow_result(StepStatus.SUCCESS, "final", "fake final"))
    env = make_env(tmp_path, run_flow_fn=flow)
    try:
        env.reset(gcd.make_gcd_task(seed=7))

        read = env.step(act("read_file", {"path": gcd.CANDIDATE_RELPATH}))
        assert read.status == StepStatus.SUCCESS
        assert not read.done
        assert read.observation.stdout_tail == stock_text()

        legal = json.dumps({"PLACE_DENSITY": 0.5, "CORE_UTILIZATION": 60.0})
        write = env.step(act("write_file", {"path": gcd.CANDIDATE_RELPATH, "content": legal}))
        assert write.status == StepStatus.SUCCESS
        assert not write.done

        run = env.step(act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert run.status == StepStatus.SUCCESS
        assert not run.done
        assert "final" in run.observation.stdout_tail
        assert "remaining" in run.observation.stdout_tail
        assert flow.calls and flow.calls[0]["candidate"]["PLACE_DENSITY"] == 0.5

        submitted = env.step(act("submit", {}))
        assert submitted.done
        assert submitted.status == StepStatus.SUCCESS
        assert submitted.reward == 1.0

        grade = env.last_submit_grade
        assert grade is not None
        assert grade.status == GradeStatus.PASS
        assert grade.passed
        assert grade.score == pytest.approx(0.5 + 0.5 * (BASE_AREA - CANDIDATE_AREA) / BASE_AREA)
        assert env.done
    finally:
        env.close()


def test_submit_call_grades_through_independent_evaluator(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(gcd.make_gcd_task(seed=3))
        grade = env.submit()
        assert grade.status == GradeStatus.PASS
        assert grade.passed
        assert grade.details["evaluator_id"] == "gcd-clean-evaluator"
        assert grade.provenance is not None and grade.provenance.seed == 3
        assert env.done
    finally:
        env.close()


def test_submit_grade_mapping_for_failing_candidate(tmp_path):
    env = make_env(
        tmp_path, parse_fn=lambda flow_result: make_metrics(area=CANDIDATE_AREA, wns=-0.5)
    )
    try:
        env.reset(gcd.make_gcd_task())
        grade = env.submit()
        assert grade.status == GradeStatus.FAIL
        assert not grade.passed
        assert grade.score == 0.0
    finally:
        env.close()


# --- invalid edits -----------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"PLACE_DENSITY": 999.0}),  # out of range
        json.dumps({"PLACE_DENSITY": "0.5; rm -rf /"}),  # injection string
        json.dumps({"CLOCK_PERIOD_NS": 0.5}),  # unknown key (immutable design fact)
        json.dumps({"PLACE_DENSITY": True}),  # bool is not a number
        "not json at all",  # malformed
        json.dumps([1, 2, 3]),  # not an object
    ],
)
def test_invalid_edit_rejected_without_mutation(tmp_path, content):
    env = make_env(tmp_path)
    try:
        env.reset(gcd.make_gcd_task())
        result = env.step(act("write_file", {"path": gcd.CANDIDATE_RELPATH, "content": content}))
        assert result.status == StepStatus.INVALID_SUBMISSION
        assert not result.done
        assert env.workspace is not None
        assert env.workspace.read_text(gcd.CANDIDATE_RELPATH) == stock_text()
        assert env.budget_tracker is not None
        assert env.budget_tracker.steps_used == 1
    finally:
        env.close()


def test_protected_write_and_unknown_tool_rejected(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(gcd.make_gcd_task())
        protected = env.step(
            act("write_file", {"path": "flow/designs/src/gcd/gcd.v", "content": "x"})
        )
        assert protected.status == StepStatus.INVALID_SUBMISSION
        unknown = env.step(act("run_tool", {"tool": "yosys"}))
        assert unknown.status == StepStatus.INVALID_SUBMISSION
        assert not unknown.done
    finally:
        env.close()


def test_missing_file_read_is_tool_failure(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(gcd.make_gcd_task())
        result = env.step(act("read_file", {"path": "does-not-exist.json"}))
        assert result.status == StepStatus.TOOL_FAILURE
        assert not result.done
    finally:
        env.close()


# --- tool outcomes ------------------------------------------------------------


def test_tool_failure_continues_episode(tmp_path):
    flow = FakeFlow(make_flow_result(StepStatus.TOOL_FAILURE, "place", "fake route fail"))
    env = make_env(tmp_path, run_flow_fn=flow)
    try:
        env.reset(gcd.make_gcd_task())
        run = env.step(act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert run.status == StepStatus.TOOL_FAILURE
        assert not run.done
        assert flow.calls
        grade = env.submit()
        assert grade.status == GradeStatus.PASS
    finally:
        env.close()


@pytest.mark.parametrize(
    ("flow_status", "step_status"),
    [
        (StepStatus.TIMEOUT, StepStatus.TIMEOUT),
        (StepStatus.INFRA_ERROR, StepStatus.INFRA_ERROR),
    ],
)
def test_tool_timeout_and_infra_terminate(tmp_path, flow_status, step_status):
    flow = FakeFlow(make_flow_result(flow_status, "place", f"fake {flow_status.value}"))
    env = make_env(tmp_path, run_flow_fn=flow)
    try:
        env.reset(gcd.make_gcd_task())
        run = env.step(act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert run.status == step_status
        assert run.done
        with pytest.raises(EnvironmentError, match="terminated"):
            env.step(act("read_file", {"path": gcd.CANDIDATE_RELPATH}))
    finally:
        env.close()


# --- reset isolation -----------------------------------------------------------


def test_repeated_reset_removes_prior_candidate_override(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(gcd.make_gcd_task(seed=3))
        legal = json.dumps({"PLACE_DENSITY": 0.5})
        write = env.step(
            act("write_file", {"path": gcd.CANDIDATE_RELPATH, "content": legal})
        )
        assert write.status == StepStatus.SUCCESS
        assert env.workspace is not None
        first_root = env.workspace.root
        assert first_root.is_dir()

        env.reset(gcd.make_gcd_task(seed=3))
        assert not first_root.exists()
        assert env.workspace is not None
        assert env.workspace.root != first_root
        assert env.workspace.read_text(gcd.CANDIDATE_RELPATH) == stock_text()
        assert env.budget_tracker is not None
        assert env.budget_tracker.steps_used == 0
    finally:
        env.close()


# --- budgets -------------------------------------------------------------------


def test_exhausted_step_budget_terminates_without_new_tool_call(tmp_path):
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


def test_exhausted_tool_budget_terminates_without_new_tool_call(tmp_path):
    flow = FakeFlow(make_flow_result(StepStatus.SUCCESS, "final", "fake final"))
    runner = FakeRunner()
    env = make_env(tmp_path, run_flow_fn=flow, runner=runner)
    try:
        env.reset(gcd.make_gcd_task(max_steps=10, max_tool_calls=1))
        first = env.step(act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert first.status == StepStatus.SUCCESS
        second = env.step(act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert second.status == StepStatus.TIMEOUT
        assert second.done
        assert len(flow.calls) == 1
        assert len(runner.calls) == 0  # fake flow never touches the backend runner
    finally:
        env.close()


def test_submit_past_step_budget_times_out(tmp_path):
    env = make_env(tmp_path)
    try:
        env.reset(gcd.make_gcd_task(max_steps=1, max_tool_calls=1))
        env.step(act("read_file", {"path": gcd.CANDIDATE_RELPATH}))
        grade = env.submit()
        assert grade.status == GradeStatus.TIMEOUT
        assert not grade.passed
    finally:
        env.close()


# --- CLI registration ------------------------------------------------------------


def _scrub_orfs_checkout() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ORFS_CHECKOUT", None)
    return env


def test_cli_run_task_registers_gcd_and_writes_outputs(tmp_path):
    task_path = tmp_path / "task.json"
    actions_path = tmp_path / "actions.json"
    task_path.write_text(gcd.make_gcd_task(seed=1).to_json() + "\n", encoding="utf-8")
    actions = [
        {"action_type": "read_file", "params": {"path": gcd.CANDIDATE_RELPATH}},
        {
            "action_type": "write_file",
            "params": {
                "path": gcd.CANDIDATE_RELPATH,
                "content": json.dumps({"PLACE_DENSITY": 0.5}),
            },
        },
    ]
    actions_path.write_text(json.dumps(actions) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, str(RUN_SCRIPT), "--task", str(task_path), "--actions",
         str(actions_path), "--output-dir", str(out)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
        env=_scrub_orfs_checkout(),
    )
    # No pinned checkout on this host: submit fails closed as infra (exit 3),
    # but the episode runs, persists outputs, and stays machine-readable.
    assert proc.returncode == 3, proc.stderr
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["task_id"] == gcd.GCD_TASK_ID
    assert summary["status"] == "infra_error"
    assert (out / "trace.jsonl").is_file()
    assert (out / "manifest.json").is_file()
    saved = json.loads((out / "submission" / "candidate.json").read_text(encoding="utf-8"))
    assert saved["PLACE_DENSITY"] == 0.5


def test_cli_grade_task_rejects_invalid_gcd_candidate(tmp_path):
    out = tmp_path / "out"
    (out / "submission").mkdir(parents=True)
    (out / "task.json").write_text(
        gcd.make_gcd_task(seed=1).to_json() + "\n", encoding="utf-8"
    )
    (out / "submission" / "candidate.json").write_text(
        json.dumps({"PLACE_DENSITY": 999.0}) + "\n", encoding="utf-8"
    )
    proc = subprocess.run(
        [sys.executable, str(GRADE_SCRIPT), "--submission-dir", str(out)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
        env=_scrub_orfs_checkout(),
    )
    assert proc.returncode == 2, proc.stderr
    grade = json.loads((out / "grade.json").read_text(encoding="utf-8"))
    assert grade["status"] == "invalid_submission"
    assert grade["passed"] is False


# --- real pinned episode (opt-in, blocked on Mac) ---------------------------------


@pytest.mark.skipif(
    not (RUN_REAL_ENV and REAL_CHECKOUT),
    reason=(
        "blocked on Mac dev host: needs SILICON_RUN_OPENROAD_ENV=1 plus "
        "ORFS_CHECKOUT at the pinned commit on the Linux route"
    ),
)
def test_real_pinned_gcd_episode_opt_in(tmp_path):
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

    from silicon_env.environments.openroad.environment import default_flow_runner

    baseline_path = gcd.TASK_DIR / "baseline.json"
    baseline_record = json.loads(baseline_path.read_text(encoding="utf-8"))
    env = GcdEnvironment(
        work_root=tmp_path / "work",
        runner=default_flow_runner(),
        orfs_checkout=REAL_CHECKOUT,
        baseline_record=baseline_record,
    )
    try:
        env.reset(gcd.make_gcd_task(seed=0))
        read = env.step(act("read_file", {"path": gcd.CANDIDATE_RELPATH}))
        assert read.status == StepStatus.SUCCESS
        legal = json.dumps({"PLACE_DENSITY": 0.5, "CORE_UTILIZATION": 55.0})
        write = env.step(
            act("write_file", {"path": gcd.CANDIDATE_RELPATH, "content": legal})
        )
        assert write.status == StepStatus.SUCCESS
        run = env.step(act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert run.status in (
            StepStatus.SUCCESS,
            StepStatus.TOOL_FAILURE,
            StepStatus.TIMEOUT,
            StepStatus.INFRA_ERROR,
        )
        if not env.done:
            grade = env.submit()
            grade.validate()
            assert grade.status in (
                GradeStatus.PASS,
                GradeStatus.FAIL,
                GradeStatus.INVALID_SUBMISSION,
                GradeStatus.TOOL_FAILURE,
                GradeStatus.TIMEOUT,
                GradeStatus.INFRA_ERROR,
            )
    finally:
        env.close()


# --- determinism: no clock-derived text in agent observations ------------------

CLOCK_TEXT_RE = (
    r"\b(wallclock_s|duration_s|elapsed_s|remaining_wallclock_s)="
)


def test_run_observation_carries_no_clock_derived_text(tmp_path):
    """Agent-visible tails must not embed clock floats.

    ``wallclock_s``/``duration_s`` are diagnostic-only (trace snapshots and
    flow provenance retain them). Embedding them in observation text would
    make otherwise-identical episodes hash differently, so the release
    gate compares raw semantic hashes.
    """
    import re

    env = make_env(tmp_path)
    try:
        env.reset(gcd.make_gcd_task(seed=0))
        run = env.step(act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert run.status == StepStatus.SUCCESS
        tails = (run.observation.stdout_tail or "") + "\n" + (
            run.observation.stderr_tail or ""
        )
        assert not re.search(CLOCK_TEXT_RE, tails), tails
    finally:
        env.close()


def test_identical_episodes_agree_on_raw_semantic_hash(tmp_path):
    """Two identical scripted episodes share one raw semantic trace hash."""
    from silicon_env import trace as trace_mod

    hashes = []
    for i in range(2):
        env = make_env(tmp_path / f"ep{i}")
        try:
            env.reset(gcd.make_gcd_task(seed=0))
            env.step(act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
            if not env.done:
                env.submit()
            run_dir = env.run_dir
            assert run_dir is not None
            events = trace_mod.read_events(Path(run_dir) / "trace.jsonl")
            trace_mod.verify_run(run_dir)
            hashes.append(trace_mod.semantic_hash_for(events))
        finally:
            env.close()
    assert hashes[0] == hashes[1]


def test_success_observation_excludes_variable_resource_logs(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    try:
        env.reset(gcd.make_gcd_task(seed=0))
        monkeypatch.setattr(env, "_runner_log_tails", lambda _: (
            "Elapsed time: 0:02.08[h:]min:sec. CPU time: user 1.94 sys 0.13\n"
            "6_report 2 183 N/A\nPeak memory: 187472KB."
        ))
        run = env.step(act("run_tool", {"tool": gcd_flow.FLOW_TOOL_NAME}))
        assert run.status == StepStatus.SUCCESS
        assert "Elapsed time" not in run.observation.stdout_tail
        assert "187472" not in run.observation.stdout_tail
        assert "stage=final" in run.observation.stdout_tail
    finally:
        env.close()
