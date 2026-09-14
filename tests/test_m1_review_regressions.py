"""Regressions from the M1 review, including real Git and Make (no EDA)."""
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from silicon_env.environments.openroad import baseline, config, flow, metrics
from silicon_env.environments.openroad.reports import discover_report_texts, parse_generated_reports
from silicon_env.environments.openroad.sources import verify_checkout
from silicon_env.runner import DEFAULT_ENV_ALLOWLIST, ToolRunner
from silicon_env.types import StepStatus


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "orfs"
    (root / "flow").mkdir(parents=True)
    # A real Make dependency graph: reusing WORK_HOME would incorrectly cache it.
    (root / "flow" / "Makefile").write_text('''
suffix = nangate45/gcd/default
json_metrics = {"finish__timing__setup__ws":0,"finish__timing__setup__tns":0,
json_metrics += "finish__design__instance__area__stdcell":$(CORE_UTILIZATION)}
.PHONY: final
final: $(WORK_HOME)/results/nangate45/gcd/default/6_final.gds
$(WORK_HOME)/results/nangate45/gcd/default/6_final.gds:
\tmkdir -p $(WORK_HOME)/results/nangate45/gcd/default
\tmkdir -p $(WORK_HOME)/reports/nangate45/gcd/default $(WORK_HOME)/logs/nangate45/gcd/default
\ttouch $@ $(WORK_HOME)/results/nangate45/gcd/default/6_final.def
\ttouch $(WORK_HOME)/results/nangate45/gcd/default/6_final.v
\tprintf 'wns 0.0 ns\\ntns 0.0 ns\\n' > $(WORK_HOME)/reports/nangate45/gcd/default/6_finish.rpt
\tprintf '%s' '$(json_metrics)' > $(WORK_HOME)/logs/$(suffix)/6_report.json
\tprintf 'Design area $(CORE_UTILIZATION) u^2\\n' > $(WORK_HOME)/logs/$(suffix)/6_report.log
''')
    git(root, "init", "-q")
    git(root, "add", "flow")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "fixture")
    commit = git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(config, "ORFS_COMMIT", commit)
    return root, commit


@pytest.mark.skipif(not shutil.which("make"), reason="requires GNU-compatible make")
def test_real_make_repeated_runs_are_isolated_and_parseable(checkout, tmp_path):
    root, commit = checkout
    runner = ToolRunner(tools={flow.FLOW_TOOL_NAME: ["make"]},
                        env_allowlist=[*DEFAULT_ENV_ALLOWLIST, *flow.FLOW_ENV_KEYS])
    outputs = []
    for index, utilization in enumerate((55, 55, 60)):
        result = flow.run_gcd_flow(
            {"CORE_UTILIZATION": utilization}, orfs_checkout=root,
            scratch_dir=tmp_path / f"run-{index}", runner=runner, seed=7, timeout_s=10,
        )
        assert result.ok, result.message
        parsed = parse_generated_reports(result)
        assert parsed.valid, parsed.reasons
        assert parsed.area_um2 == utilization
        assert "NUM_CORES=1" in result.provenance["command_argv"]
        outputs.append(result.provenance["output_dir"])
    assert len(set(outputs)) == 3
    assert verify_checkout(root, commit) == []
    assert not (root / "flow" / "results").exists()


def test_source_guard_rejects_wrong_revision_modified_and_ignored_inputs(checkout):
    root, commit = checkout
    assert verify_checkout(root, "0" * 40)
    assert not verify_checkout(root, commit)
    path = root / "flow" / "Makefile"
    original = path.read_text()
    path.write_text(original + "\n# tampered\n")
    assert verify_checkout(root, commit)
    path.write_text(original)
    (root / ".git" / "info" / "exclude").write_text("settings.mk\n")
    (root / "flow" / "settings.mk").write_text("SKIP_DETAILED_ROUTE=1\n")
    assert verify_checkout(root, commit)


def report_result(tmp_path):
    output = tmp_path / "output"
    suffix = Path("nangate45/gcd/default")
    timing = output / "reports" / suffix / "6_finish.rpt"
    area = output / "logs" / suffix / "6_report.log"
    for path, text in ((timing, "wns 0 ns\ntns 0 ns\n"),
                       (area, "Design area 100 u^2\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (area.parent / "6_report.json").write_text(json.dumps({
        "finish__timing__setup__ws": 0.0, "finish__timing__setup__tns": 0.0,
        "finish__design__instance__area__stdcell": 100.0,
    }))
    (area.parent / "5_2_route.json").write_text(
        '{"detailedroute__route__drc_errors": 0}')
    (timing.parent / "6_unconstrained.rpt").write_text("unconstrained_check_passed: 1\n")
    result = flow.FlowResult(status=StepStatus.SUCCESS, stage_reached="final",
                             provenance={"output_dir": str(output), "start_ns": 1,
                                         "variant": "default"})
    return result, timing, area


def test_discovery_rejects_stale_reports_and_other_stages(tmp_path):
    result, timing, area = report_result(tmp_path)
    assert parse_generated_reports(result).valid
    result.provenance["start_ns"] = timing.stat().st_mtime_ns + 1
    # A newer placement report must not substitute for missing final timing.
    (timing.parent / "3_place_timing.rpt").write_text("wns 0\ntns 0\n")
    assert discover_report_texts(result)["timing_text"] is None
    assert not parse_generated_reports(result).valid
    timing.unlink()
    timing.symlink_to(area)
    assert not parse_generated_reports(result).valid


def test_failed_flow_with_complete_reports_cannot_score(tmp_path):
    result, _, _ = report_result(tmp_path)
    failed = replace(result, status=StepStatus.TOOL_FAILURE)
    parsed = metrics.parse_flow_result(failed, timing_text="wns 0\ntns 0\nrouted_completion: true",
                                       area_text="Design area 100")
    assert not parsed.valid
    assert "flow-failed" in parsed.reasons


@pytest.mark.parametrize("count", [2, -1, True, "0"])
def test_route_drc_cannot_be_ignored(tmp_path, count):
    result, _, area = report_result(tmp_path)
    (area.parent / "6_report.json").write_text(json.dumps({
        "finish__timing__setup__ws": 0.0, "finish__timing__setup__tns": 0.0,
        "finish__design__instance__area__stdcell": 100.0,
    }))
    (area.parent / "5_2_route.json").write_text(
        json.dumps({"detailedroute__route__drc_errors": count}))
    assert not parse_generated_reports(result).valid


@pytest.mark.parametrize("timing,area", [
    ("wns 1e308 s\ntns 0 ns", "Design area 100"),
    ("wns 0 ns\ntns -1e308 s", "Design area 100"),
    ("wns 0 ns\ntns 0 ns", "Design area 1e308 mm^2"),
])
def test_unit_conversion_overflow_is_invalid(timing, area):
    parsed = metrics.parse_final_reports(timing + "\nrouted_completion: true", area)
    assert not parsed.valid
    json.dumps(parsed.to_dict(), allow_nan=False)


def test_cli_uses_explicit_verified_baseline(tmp_path, monkeypatch):
    from silicon_env.cli import _load_gcd_baseline_record

    path = tmp_path / "baseline.json"
    path.write_text('{"source": "override"}')
    monkeypatch.setenv("SILICON_GCD_BASELINE", str(path))
    assert _load_gcd_baseline_record() == {"source": "override"}


def test_default_evaluator_reads_generated_reports(tmp_path, monkeypatch):
    from silicon_env.environments.openroad import evaluator

    result, _, _ = report_result(tmp_path)
    parsed = parse_generated_reports(result)
    record = baseline.build_baseline_record([parsed] * 3,
                                            run_ids=["a", "b", "c"], seeds=[0, 1, 2])
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "candidate.json").write_text("{}")
    root = tmp_path / "evals"
    root.mkdir()
    monkeypatch.setattr(evaluator, "_verify_trusted_sources",
                        lambda _: (tmp_path, {"ok": True}))
    grade = evaluator.evaluate_submission(
        submission, orfs_checkout=tmp_path, eval_root=root,
        runner=ToolRunner(tools={"unused": ["true"]}), baseline_record=record,
        seed=0, timeout_s=1, run_flow_fn=lambda *a, **kw: result,
    )
    assert grade.grade.valid, grade.reason_codes
    assert grade.grade.reward == 0.5


def test_restricted_runtime_mounts_only_disposable_copies(tmp_path):
    from silicon_env.environments.openroad.runtime import GcdContainerRunner

    checkout = tmp_path / "trusted-source"
    checkout.mkdir()
    (checkout / "Makefile").write_text("# trusted\n")
    hook = tmp_path / "hook.tcl"
    hook.write_text("# trusted evidence\n")
    output = tmp_path / "outputs"
    output.mkdir()
    mounted = []

    class Backend:
        def run(self, tool, args, **kwargs):
            from silicon_env.runner import RunResult

            assert "cwd" not in kwargs
            assert kwargs["workdir"] == "/flow"
            assert "WORK_HOME=/outputs" in args
            assert "POST_FINAL_REPORT_TCL=/trusted/final_evidence.tcl" in args
            mounts = {m.container_path: m for m in kwargs["mounts"]}
            assert set(mounts) == {"/flow", "/outputs", "/trusted/final_evidence.tcl",
                                   "/trusted/constraint.sdc"}
            assert mounts["/trusted/constraint.sdc"].readonly
            assert "SDC_FILE=/trusted/constraint.sdc" in args
            assert mounts["/trusted/final_evidence.tcl"].readonly
            for mount in mounts.values():
                mounted.append(Path(mount.host_path))
                assert tmp_path not in Path(mount.host_path).parents
            source = Path(mounts["/flow"].host_path)
            assert (source / "Makefile").read_text() == "# trusted\n"
            (source / "Makefile").write_text("# tool wrote to disposable copy\n")
            (Path(mounts["/outputs"].host_path) / "partial.rpt").write_text("partial evidence")
            logs = Path(kwargs["log_dir"])
            logs.mkdir()
            return RunResult(tool_name=tool, argv=tuple(args), cwd=source, log_dir=logs,
                             stdout_path=logs / "stdout", stderr_path=logs / "stderr",
                             exit_code=1, status=StepStatus.TOOL_FAILURE,
                             timed_out=False, launched=True, duration_s=0.1)

    result = GcdContainerRunner(backend=Backend()).run(
        flow.FLOW_TOOL_NAME,
        [f"WORK_HOME={output}", f"POST_FINAL_REPORT_TCL={hook}", "final"],
        cwd=checkout, log_dir=tmp_path / "logs", timeout_s=1,
    )
    assert result.status == StepStatus.TOOL_FAILURE
    assert (output / "partial.rpt").read_text() == "partial evidence"
    assert (checkout / "Makefile").read_text() == "# trusted\n"
    assert all(not path.exists() for path in mounted)


def test_default_runtime_has_pinned_restricted_profile():
    from silicon_env.environments.openroad.environment import default_flow_runner

    backend = default_flow_runner().backend
    argv = backend.build_argv(flow.FLOW_TOOL_NAME, ["final"])
    for flag, value in (("--network", "none"), ("--pull", "never"),
                        ("--memory", "4g"), ("--cpus", "1")):
        assert argv[argv.index(flag) + 1] == value
    assert "--read-only" in argv
    assert argv[argv.index("--user") + 1] != "0"
    assert config.IMAGE_PINNED_REF in argv


def test_unconstrained_evidence_is_required_for_grade(tmp_path):
    result, timing, _ = report_result(tmp_path)
    evidence = timing.parent / "6_unconstrained.rpt"
    evidence.write_text("Warning: There are 2 unconstrained endpoints.\n"
                        "unconstrained_check_passed: 0\n")
    parsed = parse_generated_reports(result)
    assert parsed.unconstrained_paths == 2
    assert not parsed.valid
    evidence.unlink()
    parsed = parse_generated_reports(result)
    assert parsed.unconstrained_paths is None


def test_pinned_thread_and_variant_overrides_are_rejected(checkout, tmp_path):
    root, _ = checkout
    runner = ToolRunner(tools={flow.FLOW_TOOL_NAME: ["make"]})
    with pytest.raises(flow.FlowError, match="cannot override pinned"):
        flow.run_gcd_flow({}, orfs_checkout=root, scratch_dir=tmp_path / "scratch",
                          runner=runner, seed=0, timeout_s=1,
                          env_overrides={"OMP_NUM_THREADS": "8"})


def test_real_report_precision_and_stock_timing_failure(tmp_path):
    from silicon_env.environments.openroad.grader import grade_gcd_candidate

    result, timing, area = report_result(tmp_path)
    good = parse_generated_reports(result)
    record = baseline.build_baseline_record([good] * 3)
    fixture = Path(__file__).parent / "fixtures/openroad/real-26Q2"
    for name in ("6_finish.rpt", "6_unconstrained.rpt"):
        shutil.copyfile(fixture / name, timing.parent / name)
    for name in ("6_report.log", "6_report.json", "5_2_route.json"):
        shutil.copyfile(fixture / name, area.parent / name)
    measured = parse_generated_reports(result)
    assert measured.valid
    assert measured.wns_ns == -0.04544
    assert measured.tns_ns == -0.737691
    assert measured.area_um2 == 903.336
    with pytest.raises(baseline.BaselineError, match="fixed area/timing"):
        baseline.build_baseline_record([measured] * 3)
    grade = grade_gcd_candidate(measured, baseline_record=record,
                                evidence={"protected_hash": baseline.compute_protected_hash()})
    assert grade.reward == 0
    assert "timing-violation" in grade.reason_codes


def test_rounded_text_cannot_hide_small_negative_slack(tmp_path):
    result, _, area = report_result(tmp_path)
    payload = json.loads((area.parent / "6_report.json").read_text())
    payload["finish__timing__setup__ws"] = -0.00001
    payload["finish__timing__setup__tns"] = -0.00001
    (area.parent / "6_report.json").write_text(json.dumps(payload))
    measured = parse_generated_reports(result)
    assert measured.wns_ns < 0
    with pytest.raises(baseline.BaselineError, match="fixed area/timing"):
        baseline.build_baseline_record([measured] * 3)
