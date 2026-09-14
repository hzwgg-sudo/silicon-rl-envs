"""M1-03 fixed-endpoint GCD flow adapter tests.

Lightweight by design: no EDA tools, Docker, network, or API keys. The
runner is always a fake stub whose ``run()`` returns a canned
``RunResult`` (optionally materializing fresh artifacts to simulate the
tool). Only real file is the packaged ``toolchain.lock.json``.

The one real stock-GCD integration is opt-in and skipped by default
(``SILICON_RUN_OPENROAD_FLOW=1`` with ``ORFS_CHECKOUT`` set); it is
blocked on the Mac dev host, which is recorded -- never fabricated.
"""

import json
import os
from pathlib import Path

import pytest

from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad import flow as gcd_flow
from silicon_env.runner import DEFAULT_ENV_ALLOWLIST, RunResult, ToolRunner
from silicon_env.types import ContractError, StepStatus

RUN_REAL_FLOW = os.environ.get("SILICON_RUN_OPENROAD_FLOW", "") == "1"


# --- fakes -----------------------------------------------------------------


def make_fake_checkout(tmp_path: Path) -> Path:
    """Minimal pinned-checkout stand-in (flow/Makefile presence gate)."""
    root = tmp_path / "orfs"
    flow_dir = root / "flow"
    flow_dir.mkdir(parents=True)
    (flow_dir / "Makefile").write_text("# fake ORFS flow entry\n", encoding="utf-8")
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
        self.calls.append(
            {
                "tool": tool,
                "args": list(args),
                "cwd": str(cwd),
                "log_dir": str(log_dir),
                "env": dict(env or {}),
                "timeout_s": timeout_s,
            }
        )
        log_path = Path(log_dir)
        stdout_path, stderr_path = _write_logs(log_path)
        flow_dir = Path(cwd)
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


def run_stock(checkout: Path, scratch: Path, runner, **kwargs):
    return gcd_flow.run_gcd_flow(
        gcd.stock_candidate_config(),
        orfs_checkout=checkout,
        scratch_dir=scratch,
        runner=runner,
        seed=kwargs.pop("seed", 7),
        timeout_s=kwargs.pop("timeout_s", 60.0),
        **kwargs,
    )


def plan():
    return gcd_flow.resolve_flow_plan()


# --- contract ---------------------------------------------------------------


def test_endpoint_contract_matches_task_config():
    assert gcd_flow.FLOW_ENDPOINT == gcd.GCD_ENDPOINT == "final"
    assert gcd_flow.FLOW_TOOL_NAME == "openroad-flow"
    assert set(gcd_flow.STAGE_ORDER) == {
        "synth",
        "floorplan",
        "place",
        "cts",
        "route",
        "final",
    }
    resolved = plan()
    assert len(resolved["required_finals"]) == 3
    assert all(
        p.endswith(name)
        for p, name in zip(resolved["required_finals"], gcd_flow.REQUIRED_FINAL_BASENAMES)
    )
    assert set(resolved["stage_markers"]) == set(gcd_flow.STAGE_MARKER_BASENAMES)
    # Variant follows the pinned lockfile env (self-consistent by construction).
    assert resolved["variant"] == "default"


def test_pinned_env_is_single_threaded():
    resolved = plan()
    assert resolved["pinned_env"]["OMP_NUM_THREADS"] == "1"
    assert resolved["pinned_env"]["TZ"] == "UTC"
    assert "FLOW_VARIANT" in resolved["pinned_env"]
    assert resolved["make_argv"][0] == "make"
    assert any(a.startswith("DESIGN_CONFIG=") for a in resolved["make_argv"])


# --- success path ------------------------------------------------------------


def test_success_stock_candidate_completes_final(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    resolved = plan()
    emit = (*resolved["required_finals"], resolved["optional_final_log"])
    runner = FakeRunner(emit=emit)
    scratch = tmp_path / "scratch"
    result = run_stock(checkout, scratch, runner)

    assert result.status == StepStatus.SUCCESS
    assert result.stage_reached == "final"
    assert result.ok
    for relpath in resolved["required_finals"]:
        assert relpath in result.artifacts
    assert result.provenance["missing_artifacts"] == []
    assert result.provenance["stale_artifacts"] == []

    call = runner.calls[0]
    assert call["tool"] == "openroad-flow"
    assert call["cwd"] == str(checkout / "flow")
    assert call["timeout_s"] == 60.0
    assert "DESIGN_CONFIG=./designs/nangate45/gcd/config.mk" in call["args"]
    assert "PLACE_DENSITY=0.3" in call["args"]
    assert "CORE_UTILIZATION=55.0" in call["args"]
    assert "-j1" in call["args"]
    assert call["env"]["OMP_NUM_THREADS"] == "1"
    assert call["env"]["TZ"] == "UTC"

    prov = result.provenance
    assert prov["orfs_commit"] == gcd.ORFS_COMMIT
    assert prov["image_pinned_ref"] == gcd.IMAGE_PINNED_REF
    assert prov["seed_requested"] == 7
    assert prov["seed_passthrough_supported"] is False
    assert prov["seed_passthrough_note"]
    assert prov["unsupported_nondeterminism_controls"]
    assert prov["candidate"] == gcd.stock_candidate_config()
    assert len(prov["candidate_sha256"]) == 64
    assert prov["duration_s"] >= 0.0

    # Scratch materialization: candidate + overrides records exist ...
    candidate_text = (scratch / "candidate.json").read_text(encoding="utf-8")
    assert json.loads(candidate_text) == gcd.stock_candidate_config()
    overrides = (scratch / "gcd_overrides.mk").read_text(encoding="utf-8")
    assert "PLACE_DENSITY" in overrides and "CORE_UTILIZATION" in overrides
    # ... while the pinned checkout carries no candidate files.
    assert not (checkout / "candidate.json").exists()
    assert not (checkout / "flow" / "candidate.json").exists()
    assert not (checkout / "flow" / "gcd_overrides.mk").exists()


def test_partial_candidate_fills_stock_defaults(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    resolved = plan()
    runner = FakeRunner(emit=tuple(resolved["required_finals"]))
    result = gcd_flow.run_gcd_flow(
        {"PLACE_DENSITY": 0.5},
        orfs_checkout=checkout,
        scratch_dir=tmp_path / "scratch",
        runner=runner,
        seed=0,
        timeout_s=10.0,
    )
    assert result.ok
    assert "PLACE_DENSITY=0.5" in runner.calls[0]["args"]
    assert "CORE_UTILIZATION=55.0" in runner.calls[0]["args"]
    assert result.provenance["candidate"] == {
        "PLACE_DENSITY": 0.5,
        "CORE_UTILIZATION": 55.0,
    }


def test_env_overrides_recorded(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    resolved = plan()
    runner = FakeRunner(emit=tuple(resolved["required_finals"]))
    result = gcd_flow.run_gcd_flow(
        {},
        orfs_checkout=checkout,
        scratch_dir=tmp_path / "scratch",
        runner=runner,
        seed=1,
        timeout_s=10.0,
        env_overrides={"FLOW_NOTE": "probe"},
    )
    assert result.ok
    assert runner.calls[0]["env"]["FLOW_NOTE"] == "probe"
    assert result.provenance["env"]["FLOW_NOTE"] == "probe"


def test_summarize_compact_sample(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    resolved = plan()
    runner = FakeRunner(emit=tuple(resolved["required_finals"]))
    result = run_stock(checkout, tmp_path / "scratch", runner)
    sample = gcd_flow.summarize(result)
    assert sample["endpoint"] == "final"
    assert sample["status"] == "success"
    assert sample["stage_reached"] == "final"
    assert sample["ok"] is True
    assert sample["artifact_count"] == len(result.artifacts)
    assert sample["orfs_commit"] == gcd.ORFS_COMMIT
    assert sample["candidate_sha256"] == result.provenance["candidate_sha256"]


# --- failure paths: never success --------------------------------------------


def test_stage_failure_reports_progress_without_success(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    resolved = plan()
    runner = FakeRunner(
        status=StepStatus.TOOL_FAILURE,
        exit_code=2,
        emit=(resolved["stage_markers"]["place"],),
    )
    result = run_stock(checkout, tmp_path / "scratch", runner)
    assert result.status == StepStatus.TOOL_FAILURE
    assert result.stage_reached == "place"
    assert not result.ok


def test_failed_run_without_markers_stays_not_started(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    runner = FakeRunner(status=StepStatus.TOOL_FAILURE, exit_code=1)
    result = run_stock(checkout, tmp_path / "scratch", runner)
    assert result.status == StepStatus.TOOL_FAILURE
    assert result.stage_reached == "not-started"
    assert not result.ok


def test_timeout_maps_to_timeout(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    runner = FakeRunner(status=StepStatus.TIMEOUT, exit_code=124, timed_out=True)
    result = run_stock(checkout, tmp_path / "scratch", runner)
    assert result.status == StepStatus.TIMEOUT
    assert not result.ok


def test_infra_error_maps_to_infra(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    runner = FakeRunner(status=StepStatus.INFRA_ERROR, exit_code=None, launched=False)
    result = run_stock(checkout, tmp_path / "scratch", runner)
    assert result.status == StepStatus.INFRA_ERROR
    assert not result.ok


def test_runner_exception_maps_to_infra(tmp_path):
    checkout = make_fake_checkout(tmp_path)

    class ExplodingRunner:
        def run(self, *args, **kwargs):
            raise RuntimeError("boom")

    result = run_stock(checkout, tmp_path / "scratch", ExplodingRunner())
    assert result.status == StepStatus.INFRA_ERROR
    assert not result.ok


def test_zero_exit_without_finals_is_not_success(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    runner = FakeRunner()  # exit 0 but emits nothing
    result = run_stock(checkout, tmp_path / "scratch", runner)
    assert result.status == StepStatus.TOOL_FAILURE
    assert not result.ok
    assert result.stage_reached != "final"
    assert set(result.provenance["missing_artifacts"]) == set(plan()["required_finals"])


def test_stale_finals_are_rejected(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    resolved = plan()
    for relpath in resolved["required_finals"]:
        target = checkout / "flow" / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old output\n", encoding="utf-8")
        os.utime(target, (1_000_000_000, 1_000_000_000))  # 2001: definitely stale
    runner = FakeRunner()  # exit 0, emits nothing fresh
    result = run_stock(checkout, tmp_path / "scratch", runner)
    assert result.status == StepStatus.TOOL_FAILURE
    assert not result.ok
    assert set(result.provenance["stale_artifacts"]) == set(resolved["required_finals"])


# --- misuse: raise, never return ----------------------------------------------


def test_invalid_candidate_rejected(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    runner = FakeRunner()
    with pytest.raises(ContractError):
        gcd_flow.run_gcd_flow(
            {"PLACE_DENSITY": 0.5, "CLOCK_PERIOD": 1.0},
            orfs_checkout=checkout,
            scratch_dir=tmp_path / "scratch",
            runner=runner,
            seed=0,
            timeout_s=10.0,
        )
    with pytest.raises(ContractError):
        gcd_flow.run_gcd_flow(
            {"PLACE_DENSITY": 99.0},
            orfs_checkout=checkout,
            scratch_dir=tmp_path / "scratch2",
            runner=runner,
            seed=0,
            timeout_s=10.0,
        )
    assert runner.calls == []


def test_scratch_inside_checkout_rejected(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    with pytest.raises(gcd_flow.FlowError):
        run_stock(checkout, checkout / "flow" / "scratch", FakeRunner())


def test_nonempty_scratch_rejected(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "leftover.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(gcd_flow.FlowError):
        run_stock(checkout, scratch, FakeRunner())


def test_missing_checkout_rejected(tmp_path):
    with pytest.raises(gcd_flow.FlowError):
        run_stock(tmp_path / "nope", tmp_path / "scratch", FakeRunner())


def test_checkout_without_flow_makefile_rejected(tmp_path):
    checkout = tmp_path / "orfs"
    checkout.mkdir()  # no flow/Makefile
    with pytest.raises(gcd_flow.FlowError):
        run_stock(checkout, tmp_path / "scratch", FakeRunner())


def test_bad_seed_timeout_runner_rejected(tmp_path):
    checkout = make_fake_checkout(tmp_path)
    runner = FakeRunner()
    with pytest.raises(ContractError):
        run_stock(checkout, tmp_path / "s1", runner, seed=-1)
    with pytest.raises(gcd_flow.FlowError):
        run_stock(checkout, tmp_path / "s2", runner, timeout_s=0)
    with pytest.raises(gcd_flow.FlowError):
        gcd_flow.run_gcd_flow(
            {},
            orfs_checkout=checkout,
            scratch_dir=tmp_path / "s3",
            runner=None,
            seed=0,
            timeout_s=1.0,
        )
    assert runner.calls == []


# --- wrapper script (static assertions only; never executed) ------------------


def test_run_flow_script_is_present_and_pinned():
    script = Path(gcd_flow.__file__).with_name("scripts") / "run_flow.sh"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "check_openroad.py" in text  # preflight gate
    assert "DESIGN_CONFIG=./designs/nangate45/gcd/config.mk" in text
    assert "-j1" in text
    assert "OMP_NUM_THREADS=1" in text
    assert "seed" in text.lower()  # record-only seed note
    assert os.access(script, os.X_OK)


# --- real stock run (opt-in; blocked on Mac) ----------------------------------


@pytest.mark.skipif(
    not RUN_REAL_FLOW,
    reason="real stock GCD run needs Linux x86_64 + pinned image + ORFS "
    "checkout (blocked on Mac; opt-in via SILICON_RUN_OPENROAD_FLOW=1)",
)
def test_real_stock_gcd_flow(tmp_path):
    """Opt-in only: stock GCD through the fixed endpoint on the Linux route.

    Requires ``ORFS_CHECKOUT`` (pinned commit) and a host that can run the
    pinned flow. Blocked on the Mac dev laptop: do not fabricate results.
    """
    checkout_raw = os.environ.get("ORFS_CHECKOUT", "").strip()
    assert checkout_raw, "SILICON_RUN_OPENROAD_FLOW=1 needs ORFS_CHECKOUT set"
    runner = ToolRunner(
        tools={gcd_flow.FLOW_TOOL_NAME: ["make"]},
        env_allowlist=[*DEFAULT_ENV_ALLOWLIST, *gcd_flow.FLOW_ENV_KEYS],
    )
    timeout = float(os.environ.get("SILICON_FLOW_TIMEOUT_S", "7200"))
    result = gcd_flow.run_gcd_flow(
        gcd.stock_candidate_config(),
        orfs_checkout=Path(checkout_raw),
        scratch_dir=tmp_path / "scratch",
        runner=runner,
        seed=0,
        timeout_s=timeout,
    )
    assert result.ok, result.message
    assert result.stage_reached == "final"
    sample = gcd_flow.summarize(result)
    assert sample["missing_artifacts"] == []
    assert sample["stale_artifacts"] == []
