"""M2-02 Ibex baseline and correctness-gated scoring tests.

Lightweight by design: no EDA tools, Docker, network, or API keys.
All runs are fake :class:`GcdMetrics` (a design-agnostic final-stage
container despite the name) or a fake ``run_once`` callable; report
trees are synthetic fixtures under ``tmp_path`` carrying the real ORFS
``6_report.json`` key names. The only real files read are the packaged
Ibex task manifest/SDC (v0.2.0 pins) and the GCD task files for
separation checks. No ``tasks/ibex/baseline.json`` exists yet by
design: scoring stays fail-closed until the coordinator's three real
v0.2.0 reference runs land.

Tolerances (provisional, same values as the qualified GCD gate):
``area_rel=0.01``, ``wns_abs_ns=0.005``, ``tns_abs_ns=0.01``.
Justification: same pinned ORFS image+commit, same nangate45
platform, same full-precision ``6_report.json`` schema; the GCD
three-run gate (seeds 7/8/9) showed zero spread, so identical bounds
are the conservative starting point -- any drift fails closed, and the
coordinator confirms or tightens them from the measured Ibex spread
(never loosens to pass). The 5 ps WNS bound is far below the ~84 ps
headroom the margined 2.30 ns clock provides over the measured
2.2158 ns min period, so it cannot mask a real violation. The reward
formula is unchanged (0.5 + 0.5 * clamp, stock = 0.5).
"""

import importlib.util
import json
import time
from pathlib import Path

import pytest

from silicon_env.environments.openroad import baseline as bl
from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad import flow as flow_mod
from silicon_env.environments.openroad import grader, reports
from silicon_env.environments.openroad import ibex_config as ibex
from silicon_env.environments.openroad.metrics import GcdMetrics
from silicon_env.types import GradeStatus, StepStatus

GENERATOR_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "generate_openroad_baseline.py"
)

#: Ibex stock scale from the measured v0.1.0 probe (run 35054041797).
IBEX_AREA = 30029.0

#: v0.1.0 stock timing at 2.20 ns (violating; must never score).
V020_WNS = -0.0159
V020_TNS = -0.0315

#: Provisional Ibex tolerances (justified in the module docstring).
TOLS = {"area_rel": 0.01, "wns_abs_ns": 0.005, "tns_abs_ns": 0.01}


def make_metrics(
    area: float = IBEX_AREA,
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


def make_ibex_baseline(area: float = IBEX_AREA, **kwargs) -> dict:
    return bl.build_baseline_record(
        [make_metrics(area=area), make_metrics(area=area), make_metrics(area=area)],
        tolerances=dict(TOLS),
        run_ids=["stock-ibex-0", "stock-ibex-1", "stock-ibex-2"],
        seeds=[7, 8, 9],
        task=ibex,
        **kwargs,
    )


def make_evidence(record: dict, candidate: GcdMetrics, **overrides) -> dict:
    evidence = {
        "protected_hash": record["protected_hash"],
        "orfs_commit": record["orfs_commit"],
        "image_pinned_ref": record["image_pinned_ref"],
        "routed_ok": candidate.routed_ok,
        "drc_count": candidate.drc_count,
        "unconstrained_paths": candidate.unconstrained_paths,
        "wns_ns": candidate.wns_ns,
        "tns_ns": candidate.tns_ns,
    }
    evidence.update(overrides)
    return evidence


def load_generator():
    assert GENERATOR_SCRIPT.is_file()
    spec = importlib.util.spec_from_file_location(
        "generate_openroad_baseline_ibex", GENERATOR_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- task identity / fingerprint separation ----------------------------------


def test_ibex_record_carries_ibex_identity():
    record = make_ibex_baseline()
    assert record["task_id"] == "ibex-nangate45" == ibex.TASK_ID
    assert record["task_version"] == "0.2.0" == ibex.TASK_VERSION
    assert record["status"] == "verified"
    assert record["candidate"] == ibex.stock_candidate_config()
    assert record["provenance"]["seeds"] == [7, 8, 9]
    assert record["metrics"]["area_um2"] == pytest.approx(IBEX_AREA)


def test_fingerprints_separate_ibex_from_gcd():
    ibex_fp = bl.fingerprint_inputs(ibex.stock_candidate_config(), task=ibex)
    gcd_fp = bl.fingerprint_inputs(gcd.stock_candidate_config())
    # Shared pin: same ORFS commit and image.
    assert ibex_fp["orfs_commit"] == gcd_fp["orfs_commit"] == ibex.ORFS_COMMIT
    assert ibex_fp["image_pinned_ref"] == gcd_fp["image_pinned_ref"]
    # Separate identity: different stocks (50 vs 55) and different
    # protected facts (clock 2.30 vs 0.60 ns, disjoint RTL, own SDC).
    assert ibex_fp["candidate_hash"] != gcd_fp["candidate_hash"]
    assert ibex_fp["protected_hash"] != gcd_fp["protected_hash"]
    facts = bl.fixed_design_facts(task=ibex)
    assert facts["clock_period_ns"] == 2.30
    assert facts["design"] == ibex.FIXED_DESIGN == "ibex"
    assert set(facts["rtl"]).isdisjoint(set(gcd.FIXED_RTL))


def test_gcd_baseline_rejected_under_ibex_task_and_vice_versa():
    gcd_record = bl.build_baseline_record(
        [make_metrics(area=1000.0, wns=0.0)] * 3,
        tolerances=dict(TOLS),
        run_ids=["r0", "r1", "r2"],
        seeds=[7, 8, 9],
    )
    ibex_record = make_ibex_baseline()
    with pytest.raises(bl.BaselineError, match="task_id"):
        bl.validate_baseline(gcd_record, task=ibex)
    with pytest.raises(bl.BaselineError, match="task_id"):
        bl.validate_baseline(ibex_record, task=gcd)
    with pytest.raises(bl.BaselineError, match="task_id"):
        bl.validate_baseline(ibex_record)  # default task is GCD
    # Each record validates under its own task.
    bl.validate_baseline(gcd_record)
    bl.validate_baseline(ibex_record, task=ibex)


def test_gcd_evidence_hash_cannot_grade_ibex():
    ibex_record = make_ibex_baseline()
    candidate = make_metrics()
    evidence = make_evidence(ibex_record, candidate)
    evidence["protected_hash"] = bl.compute_protected_hash()  # GCD hash
    result = grader.grade_candidate(
        candidate, baseline_record=ibex_record, evidence=evidence, task=ibex
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert "hash-mismatch" in result.reason_codes


def test_cross_task_grading_fails_closed_baseline_invalid():
    ibex_record = make_ibex_baseline()
    candidate = make_metrics()
    evidence = make_evidence(ibex_record, candidate)
    # Ibex metrics graded as GCD: task identity rejects the record.
    result = grader.grade_gcd_candidate(
        candidate, baseline_record=ibex_record, evidence=evidence
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert result.reason_codes == ("baseline-invalid",)
    # GCD metrics graded as Ibex: same fail-closed outcome.
    gcd_record = bl.build_baseline_record(
        [make_metrics(area=1000.0)] * 3,
        tolerances=dict(TOLS),
        run_ids=["r0", "r1", "r2"],
        seeds=[7, 8, 9],
    )
    gcd_candidate = make_metrics(area=1000.0)
    result = grader.grade_candidate(
        gcd_candidate,
        baseline_record=gcd_record,
        evidence=make_evidence(gcd_record, gcd_candidate),
        task=ibex,
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert result.reason_codes == ("baseline-invalid",)


def test_tbd_placeholder_fails_closed_for_ibex():
    record = make_ibex_baseline()
    record["status"] = "TBD-unverified"
    with pytest.raises(bl.BaselineError, match="not usable for scoring"):
        bl.validate_baseline(record, task=ibex)
    candidate = make_metrics()
    result = grader.grade_candidate(
        candidate, baseline_record=record,
        evidence=make_evidence(record, candidate), task=ibex,
    )
    assert result.valid is False
    assert result.reason_codes == ("baseline-invalid",)


# --- acceptance: stock scores 0.5, shared formula ------------------------------


def test_stock_valid_ibex_baseline_scores_half():
    record = make_ibex_baseline()
    candidate = make_metrics()
    result = grader.grade_candidate(
        candidate, baseline_record=record,
        evidence=make_evidence(record, candidate), task=ibex,
    )
    assert result.valid is True
    assert result.ok is True
    assert result.reward == 0.5
    assert result.score == result.reward
    assert result.feasibility is True
    assert result.area_delta == pytest.approx(0.0)
    assert result.reason_codes == ("ok",)
    assert result.status == GradeStatus.PASS
    assert result.infra_error is False
    assert result.excluded_from_aggregates is False


@pytest.mark.parametrize(
    ("candidate_area", "expected"),
    [
        (IBEX_AREA, 0.5),
        (IBEX_AREA * 0.9, 0.55),
        (IBEX_AREA * 1.1, 0.45),
        (IBEX_AREA * 0.5, 0.75),
        (IBEX_AREA * 0.01, 0.995),  # 99% smaller -> near the 1.0 cap
        (IBEX_AREA * 2.5, 0.0),  # clamped: 150% larger
    ],
)
def test_shared_reward_formula_under_ibex_baseline(candidate_area, expected):
    record = make_ibex_baseline()
    candidate = make_metrics(area=candidate_area)
    result = grader.grade_candidate(
        candidate, baseline_record=record,
        evidence=make_evidence(record, candidate), task=ibex,
    )
    assert result.valid is True
    assert result.reward == pytest.approx(expected)
    assert result.reward == pytest.approx(
        grader.area_reward(record["metrics"]["area_um2"], candidate_area)
    )


# --- 2.20 ns-style slack rejected; baseline build rejects violations ----------


def test_v020_style_slack_scores_zero_timing_violation():
    record = make_ibex_baseline()
    candidate = make_metrics(area=IBEX_AREA, wns=V020_WNS, tns=V020_TNS)
    result = grader.grade_candidate(
        candidate, baseline_record=record,
        evidence=make_evidence(record, candidate), task=ibex,
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert result.feasibility is False
    assert "timing-violation" in result.reason_codes


def test_baseline_build_rejects_violating_runs():
    bad = make_metrics(area=IBEX_AREA, wns=V020_WNS, tns=V020_TNS)
    with pytest.raises(bl.BaselineError, match="timing|constraints|fails"):
        bl.build_baseline_record(
            [bad, bad, bad],
            tolerances=dict(TOLS),
            run_ids=["r0", "r1", "r2"],
            seeds=[7, 8, 9],
            task=ibex,
        )


# --- tolerance boundaries at Ibex scale ---------------------------------------


def test_area_boundary_at_ibex_scale():
    # Inclusive (<=) semantics at Ibex magnitude with exactly
    # representable arithmetic (30029 * 0.5 == 15014.5 exact).
    tols = {"area_rel": 0.5, "wns_abs_ns": 0.5, "tns_abs_ns": 0.5}
    base = {"area_um2": IBEX_AREA, "wns_ns": 0.0, "tns_ns": 0.0}
    inside = dict(base, area_um2=IBEX_AREA + 15014.5)  # exactly +50%
    outside = dict(base, area_um2=IBEX_AREA + 15014.5 + 1e-6)
    assert bl.within_tolerances(inside, base, tols)
    assert not bl.within_tolerances(outside, base, tols)
    # And under the provisional gate tolerances a clear 2% drift fails.
    assert not bl.within_tolerances(
        dict(base, area_um2=IBEX_AREA * 1.02), base, TOLS
    )


def test_wns_tns_boundaries():
    base = {"area_um2": IBEX_AREA, "wns_ns": 0.0, "tns_ns": 0.0}
    assert bl.within_tolerances(dict(base, wns_ns=0.005), base, TOLS)
    assert not bl.within_tolerances(dict(base, wns_ns=0.005 + 1e-9), base, TOLS)
    assert bl.within_tolerances(dict(base, tns_ns=-0.01), base, TOLS)
    assert not bl.within_tolerances(dict(base, tns_ns=-0.01 - 1e-9), base, TOLS)


def test_check_metrics_against_ibex_baseline():
    record = make_ibex_baseline()
    assert bl.check_metrics_against_baseline(record, make_metrics(), task=ibex) == []
    drifted = make_metrics(area=IBEX_AREA * 1.05)
    assert bl.check_metrics_against_baseline(record, drifted, task=ibex) == [
        "metric-drift"
    ]
    assert bl.check_metrics_against_baseline(
        record, make_metrics(valid=False), task=ibex
    ) == ["candidate-invalid"]
    with pytest.raises(bl.BaselineError):
        bl.check_metrics_against_baseline(record, make_metrics())  # default GCD task


# --- report fixtures: real ORFS 6_report.json keys in an Ibex tree -------------


def _write_ibex_tree(root: Path, *, ws: float, tns: float, area: float, drc: int) -> None:
    stem = root / "reports" / "nangate45" / "ibex" / "default"
    logs = root / "logs" / "nangate45" / "ibex" / "default"
    stem.mkdir(parents=True)
    logs.mkdir(parents=True)
    (stem / "6_finish.rpt").write_text("report_checks stub (rounded text)\n")
    (logs / "6_report.log").write_text("report log stub\n")
    (logs / "6_report.json").write_text(
        json.dumps(
            {
                "finish__timing__setup__ws": ws,
                "finish__timing__setup__tns": tns,
                "finish__design__instance__area__stdcell": area,
            }
        )
    )
    (logs / "5_2_route.json").write_text(
        json.dumps({"detailedroute__route__drc_errors": drc})
    )
    (stem / "6_unconstrained.rpt").write_text("unconstrained_check_passed: 1\n")


def _flow_result(output_dir: Path, start_ns: int) -> flow_mod.FlowResult:
    return flow_mod.FlowResult(
        status=StepStatus.SUCCESS,
        stage_reached="final",
        artifacts={},
        provenance={
            "output_dir": str(output_dir),
            "start_ns": start_ns,
            "variant": "default",
        },
    )


def test_ibex_report_keys_parse_from_ibex_tree(tmp_path):
    start_ns = time.time_ns()
    _write_ibex_tree(tmp_path, ws=0.012, tns=0.0, area=IBEX_AREA, drc=0)
    result = _flow_result(tmp_path, start_ns)
    texts = reports.discover_report_texts(
        result, design="ibex", platform="nangate45"
    )
    assert texts["timing_text"] is not None and "wns" in texts["timing_text"]
    assert f"{IBEX_AREA}" in (texts["area_text"] or "")
    assert texts["drc_text"] == "drc_violations: 0"
    parsed = reports.parse_generated_reports(
        result, design="ibex", platform="nangate45"
    )
    assert parsed.valid, parsed.reasons
    assert parsed.area_um2 == pytest.approx(IBEX_AREA)
    assert parsed.wns_ns == pytest.approx(0.0)
    assert parsed.tns_ns == pytest.approx(0.0)
    assert parsed.drc_count == 0
    assert parsed.unconstrained_paths == 0
    assert parsed.routed_ok is True


def test_ibex_tree_with_v020_slack_parses_but_never_scores(tmp_path):
    start_ns = time.time_ns()
    _write_ibex_tree(tmp_path, ws=V020_WNS, tns=V020_TNS, area=IBEX_AREA, drc=0)
    result = _flow_result(tmp_path, start_ns)
    parsed = reports.parse_generated_reports(
        result, design="ibex", platform="nangate45"
    )
    assert parsed.valid, parsed.reasons
    assert parsed.wns_ns == pytest.approx(V020_WNS)
    assert parsed.tns_ns == pytest.approx(V020_TNS)
    record = make_ibex_baseline()
    grade = grader.grade_candidate(
        parsed, baseline_record=record,
        evidence=make_evidence(record, parsed), task=ibex,
    )
    assert grade.valid is False
    assert grade.reward == 0.0
    assert "timing-violation" in grade.reason_codes


def test_ibex_drc_key_propagates_dirty_counts(tmp_path):
    start_ns = time.time_ns()
    _write_ibex_tree(tmp_path, ws=0.0, tns=0.0, area=IBEX_AREA, drc=3)
    result = _flow_result(tmp_path, start_ns)
    parsed = reports.parse_generated_reports(
        result, design="ibex", platform="nangate45"
    )
    assert parsed.valid is False
    assert parsed.drc_count == 3
    assert "drc-violations" in parsed.reasons


def test_design_paths_do_not_cross_read(tmp_path):
    # An Ibex lookup over a GCD-layout tree finds nothing (fail closed);
    # defaults still resolve the GCD tree (bit-identical GCD behavior).
    start_ns = time.time_ns()
    stem = tmp_path / "reports" / "nangate45" / "gcd" / "default"
    logs = tmp_path / "logs" / "nangate45" / "gcd" / "default"
    stem.mkdir(parents=True)
    logs.mkdir(parents=True)
    (stem / "6_finish.rpt").write_text("stub\n")
    (logs / "6_report.log").write_text("stub\n")
    (logs / "6_report.json").write_text(
        json.dumps(
            {
                "finish__timing__setup__ws": 0.0,
                "finish__timing__setup__tns": 0.0,
                "finish__design__instance__area__stdcell": 679.63,
            }
        )
    )
    (logs / "5_2_route.json").write_text(
        json.dumps({"detailedroute__route__drc_errors": 0})
    )
    (stem / "6_unconstrained.rpt").write_text("unconstrained_check_passed: 1\n")
    result = _flow_result(tmp_path, start_ns)
    crossed = reports.discover_report_texts(
        result, design="ibex", platform="nangate45"
    )
    assert crossed == {"timing_text": None, "area_text": None, "drc_text": None}
    assert reports.discover_report_texts(result)["timing_text"] is not None
    assert reports.parse_generated_reports(result).valid


def test_invalid_design_names_fail_closed(tmp_path):
    start_ns = time.time_ns()
    _write_ibex_tree(tmp_path, ws=0.0, tns=0.0, area=IBEX_AREA, drc=0)
    result = _flow_result(tmp_path, start_ns)
    for bad in ("", "../gcd", "ibex/../../gcd"):
        texts = reports.discover_report_texts(
            result, design=bad, platform="nangate45"
        )
        # Traversal/malformed names read nothing (fail closed, no raise).
        assert texts == {"timing_text": None, "area_text": None, "drc_text": None}
    # A GCD-default lookup over an Ibex-only tree likewise finds nothing.
    assert reports.discover_report_texts(result) == {
        "timing_text": None, "area_text": None, "drc_text": None}


# --- generator script: task-aware, fake-driven --------------------------------


def test_generator_builds_ibex_record_from_fakes(tmp_path):
    gen = load_generator()
    out = tmp_path / "baseline.json"
    record = gen.generate_baseline(
        output_path=out,
        run_once=lambda scratch, seed: make_metrics(),
        seeds=[7, 8, 9],
        task="ibex",
    )
    assert out.is_file()
    assert record["task_id"] == "ibex-nangate45"
    assert record["task_version"] == "0.2.0"
    assert record["provenance"]["run_ids"] == [
        "stock-ibex-0",
        "stock-ibex-1",
        "stock-ibex-2",
    ]
    assert record["provenance"]["seeds"] == [7, 8, 9]
    assert record["metrics"]["area_um2"] == pytest.approx(IBEX_AREA)
    stored = json.loads(out.read_text(encoding="utf-8"))
    bl.validate_baseline(stored, task=ibex)
    with pytest.raises(bl.BaselineError):
        bl.validate_baseline(stored)  # GCD default rejects it


def test_generator_ibex_fails_closed_on_drift_and_invalid(tmp_path):
    gen = load_generator()
    out = tmp_path / "baseline.json"

    def drifting(scratch: Path, seed: int) -> GcdMetrics:
        if seed == 9:
            return make_metrics(area=IBEX_AREA * 1.05)
        return make_metrics()

    with pytest.raises(bl.BaselineError, match="drift"):
        gen.generate_baseline(
            output_path=out, run_once=drifting, seeds=[7, 8, 9], task="ibex"
        )
    assert not out.exists()
    with pytest.raises(bl.BaselineError, match="constraints"):
        gen.generate_baseline(
            output_path=out,
            run_once=lambda scratch, seed: make_metrics(
                area=IBEX_AREA, wns=V020_WNS, tns=V020_TNS
            ),
            seeds=[7, 8, 9],
            task="ibex",
        )
    assert not out.exists()


def test_generator_task_resolution():
    gen = load_generator()
    assert gen.resolve_task(None) == ("gcd", gcd)
    assert gen.resolve_task("gcd") == ("gcd", gcd)
    assert gen.resolve_task("ibex") == ("ibex", ibex)
    assert gen.resolve_task(ibex) == ("ibex", ibex)
    with pytest.raises(bl.BaselineError, match="unknown task"):
        gen.resolve_task("mystery")
    with pytest.raises(bl.BaselineError, match="shared surface"):
        gen.resolve_task(object())


def test_ibex_run_once_rejects_bad_inputs_without_docker(tmp_path):
    gen = load_generator()
    # Checkout validation happens at builder time (no docker touched).
    with pytest.raises(bl.BaselineError, match="required assets|not a dir"):
        gen.make_ibex_run_once(
            orfs_checkout=str(tmp_path / "nope"), timeout_s=7200.0
        )
    with pytest.raises(bl.BaselineError, match="positive finite"):
        gen.make_ibex_run_once(orfs_checkout=str(tmp_path), timeout_s=0.0)


def test_check_ibex_checkout_requires_all_assets(tmp_path):
    gen = load_generator()
    with pytest.raises(bl.BaselineError, match="missing Ibex required assets"):
        gen.check_ibex_checkout(tmp_path)


def test_main_ibex_bogus_checkout_fails_without_writing(tmp_path, monkeypatch):
    gen = load_generator()
    monkeypatch.delenv("ORFS_CHECKOUT", raising=False)
    out = tmp_path / "baseline.json"
    assert (
        gen.main(
            ["--task", "ibex", "--orfs-checkout", str(tmp_path / "nope"),
             "--output", str(out), "--seed", "7"]
        )
        == 1
    )
    assert not out.exists()


def test_gcd_default_output_unchanged():
    gen = load_generator()
    parser = gen.build_parser()
    args = parser.parse_args([])
    assert args.task == "gcd"
    assert args.output is None  # resolved to the packaged GCD path in main
    assert gen.DEFAULT_OUTPUT.endswith("tasks/gcd/baseline.json")
    assert gen.DEFAULT_IBEX_OUTPUT.endswith("tasks/ibex/baseline.json")


# --- regression: GCD behavior bit-identical -----------------------------------


def test_gcd_grader_entry_still_pinned():
    record = bl.build_baseline_record(
        [make_metrics(area=1000.0)] * 3,
        tolerances=dict(TOLS),
        run_ids=["r0", "r1", "r2"],
        seeds=[0, 1, 2],
    )
    candidate = make_metrics(area=1000.0)
    via_old = grader.grade_gcd_candidate(
        candidate, baseline_record=record,
        evidence=make_evidence(record, candidate),
    )
    via_new = grader.grade_candidate(
        candidate, baseline_record=record,
        evidence=make_evidence(record, candidate),
    )
    assert via_old.to_dict() == via_new.to_dict()
    assert via_old.reward == 0.5


def test_task_config_missing_surface_rejected():
    with pytest.raises(bl.BaselineError, match="shared surface"):
        bl.validate_baseline(make_ibex_baseline(), task=object())
    with pytest.raises(bl.BaselineError, match="shared surface"):
        bl.fingerprint_inputs({}, task=object())
