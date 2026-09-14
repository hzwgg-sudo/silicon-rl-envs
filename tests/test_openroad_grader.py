"""M1-06 correctness-gated GCD scoring tests.

Lightweight by design: no EDA tools, Docker, network, or API keys.
Baselines are built from fake :class:`GcdMetrics` via
``baseline.build_baseline_record``; the only real file read is the
packaged ``TBD-unverified`` placeholder (asserted to fail scoring).
"""

import copy
import json
import math
from pathlib import Path

import pytest

from silicon_env.environments.openroad import baseline as bl
from silicon_env.environments.openroad import grader as gcd_grader
from silicon_env.environments.openroad.metrics import GcdMetrics
from silicon_env.types import GradeStatus

TASK_BASELINE = Path(bl.__file__).with_name("tasks") / "gcd" / "baseline.json"

BASE_AREA = 1000.0
TOLS = {"area_rel": 0.01, "wns_abs_ns": 0.005, "tns_abs_ns": 0.01}


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


def grade(area: float = BASE_AREA, **kwargs) -> gcd_grader.GcdGrade:
    record = make_baseline()
    candidate = make_metrics(area=area, **kwargs)
    return gcd_grader.grade_gcd_candidate(
        candidate, baseline_record=record, evidence=make_evidence(record, candidate)
    )


# --- acceptance: stock scores 0.5, timing-violating smaller scores 0 -----------


def test_stock_valid_baseline_scores_half():
    result = grade()
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


def test_timing_violating_smaller_design_scores_zero():
    result = grade(area=BASE_AREA * 0.5, wns=-0.05)
    assert result.valid is False
    assert result.reward == 0.0
    assert result.score == 0.0
    assert result.feasibility is False
    assert "timing-violation" in result.reason_codes


# --- explicit bounded formula: better / worse / clamp --------------------------


@pytest.mark.parametrize(
    ("candidate_area", "expected"),
    [
        (BASE_AREA, 0.5),  # stock
        (BASE_AREA * 0.9, 0.55),  # 10% smaller -> above 0.5
        (BASE_AREA * 1.1, 0.45),  # 10% larger -> below 0.5
        (BASE_AREA * 0.5, 0.75),  # 50% smaller
        (BASE_AREA * 1.5, 0.25),  # 50% larger
    ],
)
def test_bounded_formula_table(candidate_area, expected):
    result = grade(area=candidate_area)
    assert result.valid is True
    assert result.reward == pytest.approx(expected)
    assert result.score == result.reward
    assert result.area_delta == pytest.approx(BASE_AREA - candidate_area)


def test_huge_regression_clamps_to_zero():
    result = grade(area=BASE_AREA * 3.0)  # relative improvement -2.0 -> clamp -1
    assert result.valid is True
    assert result.reward == 0.0
    assert result.area_delta == pytest.approx(-2.0 * BASE_AREA)


def test_huge_improvement_approaches_one():
    result = grade(area=1.0)  # relative improvement 0.999 -> ~0.9995
    assert result.valid is True
    assert result.reward == pytest.approx(0.5 + 0.5 * (BASE_AREA - 1.0) / BASE_AREA)
    assert result.reward < 1.0


def test_clamp_boundaries():
    assert gcd_grader.clamp_rel_improvement(1.5) == 1.0
    assert gcd_grader.clamp_rel_improvement(-1.5) == -1.0
    assert gcd_grader.clamp_rel_improvement(1.0) == 1.0
    assert gcd_grader.clamp_rel_improvement(-1.0) == -1.0
    assert gcd_grader.clamp_rel_improvement(0.25) == 0.25
    with pytest.raises(Exception, match="finite"):
        gcd_grader.clamp_rel_improvement(float("nan"))


def test_area_reward_helper_matches_formula():
    assert gcd_grader.area_reward(1000.0, 1000.0) == 0.5
    assert gcd_grader.area_reward(1000.0, 900.0) == pytest.approx(0.55)
    assert gcd_grader.area_reward(1000.0, 3000.0) == 0.0  # clamped


# --- constraint failures --------------------------------------------------------


def test_tns_violation_scores_zero():
    result = grade(tns=-0.1)
    assert result.valid is False
    assert result.reward == 0.0
    assert "timing-violation" in result.reason_codes


def test_zero_slack_passes_timing():
    result = grade(wns=0.0, tns=0.0)
    assert result.valid is True
    assert result.reward == 0.5


def test_drc_dirty_scores_zero():
    result = grade(drc_count=7)
    assert result.valid is False
    assert result.reward == 0.0
    assert "drc-dirty" in result.reason_codes
    assert result.feasibility is False


def test_drc_unknown_fails_closed():
    result = grade(drc_count=None)
    assert result.valid is False
    assert result.reward == 0.0
    assert "missing-evidence" in result.reason_codes


def test_unconstrained_paths_scores_zero():
    result = grade(unconstrained=3)
    assert result.valid is False
    assert result.reward == 0.0
    assert "unconstrained-paths" in result.reason_codes


def test_unconstrained_unknown_fails_closed():
    result = grade(unconstrained=None)
    assert result.valid is False
    assert result.reward == 0.0
    assert "missing-evidence" in result.reason_codes


def test_route_incomplete_scores_zero():
    candidate = make_metrics(valid=True, routed_ok=False)
    record = make_baseline()
    result = gcd_grader.grade_gcd_candidate(
        candidate, baseline_record=record, evidence=make_evidence(record, candidate)
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert "route-incomplete" in result.reason_codes


def test_invalid_metrics_flag_scores_zero():
    result = grade(valid=False)
    assert result.valid is False
    assert result.reward == 0.0
    assert "invalid-metrics" in result.reason_codes


# --- hash tampering / evidence ----------------------------------------------------


def test_hash_tampering_scores_zero_but_stays_feasible():
    record = make_baseline()
    candidate = make_metrics()
    evidence = make_evidence(record, candidate, protected_hash="0" * 64)
    result = gcd_grader.grade_gcd_candidate(
        candidate, baseline_record=record, evidence=evidence
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert "hash-mismatch" in result.reason_codes
    assert result.feasibility is True  # design feasible, trust failed


def test_toolchain_ref_tampering_scores_zero():
    record = make_baseline()
    candidate = make_metrics()
    evidence = make_evidence(record, candidate, orfs_commit="0" * 40)
    result = gcd_grader.grade_gcd_candidate(
        candidate, baseline_record=record, evidence=evidence
    )
    assert result.valid is False
    assert "hash-mismatch" in result.reason_codes


@pytest.mark.parametrize("evidence", [None, {}, {"orfs_commit": "x"}])
def test_missing_evidence_fails_closed(evidence):
    record = make_baseline()
    candidate = make_metrics()
    result = gcd_grader.grade_gcd_candidate(
        candidate, baseline_record=record, evidence=evidence
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert "missing-evidence" in result.reason_codes


def test_evidence_disagreement_fails_closed():
    record = make_baseline()
    candidate = make_metrics(wns=0.0)
    evidence = make_evidence(record, candidate, wns_ns=-0.5)  # disagrees
    result = gcd_grader.grade_gcd_candidate(
        candidate, baseline_record=record, evidence=evidence
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert "evidence-mismatch" in result.reason_codes


def test_non_metrics_candidate_fails_closed():
    record = make_baseline()
    candidate = make_metrics()
    result = gcd_grader.grade_gcd_candidate(
        "not-metrics", baseline_record=record,
        evidence=make_evidence(record, candidate),
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert "invalid-metrics" in result.reason_codes


# --- baseline trust: zero / TBD / tampered ---------------------------------------


def test_tbd_placeholder_baseline_fails_scoring_use():
    payload = bl.load_baseline(TASK_BASELINE)
    assert payload["status"] == "TBD-unverified"
    candidate = make_metrics()
    evidence = {"protected_hash": "TBD-unverified"}
    result = gcd_grader.grade_gcd_candidate(
        candidate, baseline_record=payload, evidence=evidence
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert result.reason_codes == ("baseline-invalid",)


def test_zero_baseline_area_fails_closed():
    record = make_baseline()
    tampered = copy.deepcopy(record)
    tampered["metrics"] = dict(tampered["metrics"], area_um2=0.0)
    candidate = make_metrics()
    result = gcd_grader.grade_gcd_candidate(
        candidate, baseline_record=tampered,
        evidence=make_evidence(record, candidate),
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert result.reason_codes == ("baseline-invalid",)


def test_tampered_baseline_protected_hash_fails_closed():
    record = make_baseline()
    tampered = copy.deepcopy(record)
    tampered["protected_hash"] = "f" * 64
    candidate = make_metrics()
    result = gcd_grader.grade_gcd_candidate(
        candidate, baseline_record=tampered,
        evidence=make_evidence(tampered, candidate),
    )
    assert result.valid is False
    assert result.reward == 0.0
    assert result.reason_codes == ("baseline-invalid",)


# --- non-finite metrics fail closed -------------------------------------------------


@pytest.mark.parametrize("area", [float("nan"), float("inf"), 0.0, -5.0, None])
def test_bad_candidate_area_fails_closed(area):
    result = grade(area=area)
    assert result.valid is False
    assert result.reward == 0.0
    assert "nonfinite-metric" in result.reason_codes


@pytest.mark.parametrize("slack", [float("nan"), float("inf"), None])
def test_nonfinite_slack_fails_closed(slack):
    result = grade(wns=slack)
    assert result.valid is False
    assert result.reward == 0.0
    assert "nonfinite-metric" in result.reason_codes
    result = grade(tns=slack)
    assert result.valid is False
    assert "nonfinite-metric" in result.reason_codes


# --- infrastructure errors carry no trainable reward ---------------------------------


def test_infra_error_excluded_from_aggregates():
    result = gcd_grader.grade_infra_error("runner-timeout")
    assert result.valid is False
    assert result.ok is False
    assert result.reward == 0.0
    assert result.score == 0.0
    assert result.feasibility is False
    assert result.infra_error is True
    assert result.excluded_from_aggregates is True
    assert result.status == GradeStatus.INFRA_ERROR
    assert "infra-error-excluded" in result.reason_codes
    assert "runner-timeout" in result.reason_codes


def test_infra_error_default_reason():
    result = gcd_grader.grade_infra_error()
    assert result.reason_codes == ("infra-error-excluded",)
    assert result.excluded_from_aggregates is True


# --- typed metrics / serialization --------------------------------------------------


def test_metrics_are_typed_with_units():
    result = grade(area=BASE_AREA * 0.9)
    by_name = {m.name: m for m in result.metrics}
    assert by_name["cell_area"].unit == "um^2"
    assert by_name["wns"].unit == "ns"
    assert by_name["reward"].value == pytest.approx(result.reward)
    assert by_name["area_delta"].unit == "um^2"
    for metric in result.metrics:
        metric.validate()
        assert math.isfinite(metric.value)


def test_to_dict_is_strict_json():
    result = grade()
    payload = result.to_dict()
    assert payload["score"] == payload["reward"] == 0.5
    text = json.dumps(payload, sort_keys=True, allow_nan=False)
    assert json.loads(text)["valid"] is True
    sample = gcd_grader.summarize(result)
    assert sample["reason_codes"] == ["ok"]
    assert sample["status"] == "pass"


def test_invalid_grade_serializes():
    result = grade(area=BASE_AREA * 0.5, wns=-0.05)
    payload = result.to_dict()
    text = json.dumps(payload, sort_keys=True, allow_nan=False)
    assert json.loads(text)["reward"] == 0.0
