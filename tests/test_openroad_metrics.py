"""M1-04 typed GCD final-stage metrics parser tests.

Lightweight by design: no EDA tools, Docker, network, or API keys. All
report texts are inline strings or small synthetic fixtures under
``tests/fixtures/openroad/`` (each labeled synthetic; real-report
capture from the pinned toolchain is pending a Linux run and is
recorded as blocked, never fabricated).
"""

import math
from pathlib import Path

import pytest

from silicon_env.environments.openroad import flow as gcd_flow
from silicon_env.environments.openroad import metrics as gcd_metrics
from silicon_env.types import StepStatus

FIXTURES = Path(__file__).with_name("fixtures") / "openroad"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def ok_texts() -> tuple[str, str, str]:
    return (
        read_fixture("final_timing_ok.rpt"),
        read_fixture("final_area_ok.rpt"),
        read_fixture("final_drc_clean.rpt"),
    )


# --- happy path --------------------------------------------------------------


def test_valid_final_reports_parse_with_units_and_stage():
    timing, area, drc = ok_texts()
    result = gcd_metrics.parse_final_reports(
        timing, area, drc, timing_ref="timing.rpt", area_ref="area.rpt",
        drc_ref="drc.rpt",
    )
    assert result.valid
    assert result.ok
    assert result.reasons == ()
    assert result.stage == "final"
    assert result.schema == gcd_metrics.PINNED_SCHEMA
    assert result.area_um2 == pytest.approx(1234.5)
    assert result.wns_ns == pytest.approx(-0.023)
    assert result.tns_ns == pytest.approx(-0.145)
    assert result.routed_ok is True
    assert result.drc_count == 0
    assert result.unconstrained_paths == 0
    assert result.raw_refs["timing"] == "timing.rpt"
    assert result.raw_refs["area"] == "area.rpt"
    assert result.raw_refs["drc"] == "drc.rpt"

    typed = {m.name: m for m in result.metrics()}
    assert typed["cell_area"].unit == "um^2"
    assert typed["wns"].unit == "ns"
    assert typed["tns"].unit == "ns"
    assert typed["cell_area"].value == pytest.approx(1234.5)

    sample = gcd_metrics.summarize(result)
    assert sample["valid"] is True
    assert sample["reasons"] == []


def test_zero_slack_is_valid_real_zero():
    timing, area, _ = ok_texts()
    timing = timing.replace("wns: -0.023 ns", "wns: 0.0 ns")
    timing = timing.replace("tns: -0.145 ns", "tns: 0.0 ns")
    result = gcd_metrics.parse_final_reports(timing, area)
    assert result.valid
    assert result.wns_ns == pytest.approx(0.0)
    assert result.tns_ns == pytest.approx(0.0)


def test_negative_slack_is_valid_metrics_not_grader_pass():
    timing, area, _ = ok_texts()
    result = gcd_metrics.parse_final_reports(timing, area)
    assert result.valid  # violated timing is still trustworthy metrics
    assert result.wns_ns < 0
    assert result.tns_ns < 0


def test_opensta_spellings_parse():
    timing = (
        "worst slack -0.050 ns\n"
        "total negative slack -0.300 ns\n"
        "detailed routing completed\n"
    )
    area = "Total cell area 999.0 um^2\n"
    result = gcd_metrics.parse_final_reports(timing, area)
    assert result.valid
    assert result.wns_ns == pytest.approx(-0.05)
    assert result.tns_ns == pytest.approx(-0.30)
    assert result.area_um2 == pytest.approx(999.0)


def test_unit_conversions():
    timing = "wns: -50.0 ps\ntns: -0.2 us\nrouted_completion: true\n"
    area = "Design area 1234500000 nm^2\n"
    result = gcd_metrics.parse_final_reports(timing, area)
    assert result.valid
    assert result.wns_ns == pytest.approx(-0.05)  # ps -> ns
    assert result.tns_ns == pytest.approx(-200.0)  # us -> ns
    assert result.area_um2 == pytest.approx(1234.5)  # nm^2 -> um^2
    assert "unconstrained-unknown" in result.notes
    assert "drc-unknown" in result.notes  # unknown, not defaulted to zero
    assert result.drc_count is None


def test_mm2_area_converts():
    result = gcd_metrics.parse_final_reports(
        "wns: 0 ns\ntns: 0 ns\nrouted_completion: true\n",
        "Design area 0.0012345 mm^2\n",
    )
    assert result.valid
    assert result.area_um2 == pytest.approx(1234.5)


# --- fail-closed: missing / malformed ----------------------------------------


def test_missing_reports_are_invalid_not_defaults():
    result = gcd_metrics.parse_final_reports(None, None)
    assert not result.valid
    assert "missing-timing-report" in result.reasons
    assert "missing-area-report" in result.reasons
    assert result.area_um2 is None
    assert result.wns_ns is None
    assert result.metrics() == ()


def test_empty_strings_are_invalid():
    result = gcd_metrics.parse_final_reports("", "   ")
    assert not result.valid
    assert "missing-timing-report" in result.reasons
    assert "missing-area-report" in result.reasons


def test_absent_keys_are_invalid():
    result = gcd_metrics.parse_final_reports(
        "wns: -0.01 ns\nrouted_completion: true\n",  # no tns
        "some unrelated log line\n",  # no area
    )
    assert not result.valid
    assert "missing-tns" in result.reasons
    assert "missing-area" in result.reasons
    # The present WNS value is still retained for diagnosis.
    assert result.wns_ns == pytest.approx(-0.01)


def test_nan_and_inf_are_invalid():
    for bad in ("nan", "NaN", "inf", "-inf", "infinity"):
        result = gcd_metrics.parse_final_reports(
            f"wns: {bad}\ntns: -0.1 ns\nrouted_completion: true\n",
            "Design area 100.0 u^2\n",
        )
        assert not result.valid, bad
        assert "non-finite-wns" in result.reasons
    result = gcd_metrics.parse_final_reports(
        "wns: -0.1 ns\ntns: nan ns\nrouted_completion: true\n",
        "Design area nan u^2\n",
    )
    assert not result.valid
    assert "non-finite-tns" in result.reasons
    assert "non-finite-area" in result.reasons


def test_truncated_report_fixture_is_invalid():
    timing = read_fixture("final_timing_truncated.rpt")
    area = read_fixture("final_area_ok.rpt")
    result = gcd_metrics.parse_final_reports(timing, area)
    assert not result.valid
    assert "missing-wns" in result.reasons
    assert "missing-tns" in result.reasons
    assert "missing-completion" in result.reasons


def test_unknown_schema_is_invalid():
    timing, area, _ = ok_texts()
    result = gcd_metrics.parse_final_reports(timing, area, schema="orfs-99Q9-x")
    assert not result.valid
    assert result.reasons == ("unknown-schema",)


def test_embedded_unknown_schema_header_is_invalid():
    timing, area, _ = ok_texts()
    timing = timing.replace(
        "schema: orfs-26Q2-final-v1", "schema: orfs-99Q9 bogus")
    result = gcd_metrics.parse_final_reports(timing, area)
    assert not result.valid
    assert "unknown-schema" in result.reasons


def test_zero_area_is_invalid_not_real_zero():
    timing, area, _ = ok_texts()
    result = gcd_metrics.parse_final_reports(
        timing, area.replace("1234.5", "0.0"))
    assert not result.valid
    assert "area-nonpositive" in result.reasons


def test_positive_tns_is_malformed():
    timing, area, _ = ok_texts()
    timing = timing.replace("tns: -0.145 ns", "tns: 0.5 ns")
    result = gcd_metrics.parse_final_reports(timing, area)
    assert not result.valid
    assert "tns-positive" in result.reasons


def test_missing_completion_marker_is_invalid():
    timing, area, _ = ok_texts()
    timing = timing.replace("routed_completion: true\n", "")
    result = gcd_metrics.parse_final_reports(timing, area)
    assert not result.valid
    assert "missing-completion" in result.reasons
    assert result.routed_ok is False


def test_explicit_negative_completion_is_invalid():
    timing, area, _ = ok_texts()
    timing = timing.replace("routed_completion: true", "routed_completion: false")
    result = gcd_metrics.parse_final_reports(timing, area)
    assert not result.valid
    assert "missing-completion" in result.reasons


# --- wrong stage can never substitute ----------------------------------------


def test_wrong_stage_label_is_invalid():
    timing, area, _ = ok_texts()
    result = gcd_metrics.parse_final_reports(timing, area, stage="place")
    assert not result.valid
    assert "wrong-stage" in result.reasons


def test_place_fixture_cannot_substitute_for_final():
    timing = read_fixture("place_timing.rpt")
    area = read_fixture("final_area_ok.rpt")
    result = gcd_metrics.parse_final_reports(timing, area)
    assert not result.valid
    assert "wrong-stage" in result.reasons


def test_flow_result_stage_flows_into_parser():
    timing, area, _ = ok_texts()
    failed = gcd_flow.FlowResult(
        status=StepStatus.TOOL_FAILURE,
        stage_reached="place",
        artifacts={},
        provenance={},
        message="failed at place",
    )
    result = gcd_metrics.parse_flow_result(failed, timing_text=timing,
                                           area_text=area)
    assert not result.valid
    assert "wrong-stage" in result.reasons
    assert result.raw_refs["flow_status"] == "tool_failure"
    assert result.raw_refs["flow_stage_reached"] == "place"


def test_flow_result_none_is_misuse():
    with pytest.raises(gcd_metrics.MetricsError):
        gcd_metrics.parse_flow_result(None, timing_text="x", area_text="y")


# --- DRC / unconstrained evidence --------------------------------------------


def test_drc_violations_invalidate():
    timing, area, _ = ok_texts()
    result = gcd_metrics.parse_final_reports(
        timing, area, "drc_violations: 7\n")
    assert not result.valid
    assert "drc-violations" in result.reasons
    assert result.drc_count == 7


def test_unparseable_drc_report_is_invalid():
    timing, area, _ = ok_texts()
    result = gcd_metrics.parse_final_reports(timing, area, "no drc data here\n")
    assert not result.valid
    assert "drc-unparseable" in result.reasons
    assert result.drc_count is None


def test_unconstrained_paths_invalidate():
    timing, area, _ = ok_texts()
    timing = timing.replace("unconstrained_paths: 0", "unconstrained_paths: 3")
    result = gcd_metrics.parse_final_reports(timing, area)
    assert not result.valid
    assert "unconstrained-paths" in result.reasons
    assert result.unconstrained_paths == 3


def test_missing_indicators_are_unknown_not_favorable():
    result = gcd_metrics.parse_final_reports(
        "wns: -0.01 ns\ntns: -0.02 ns\nrouted_completion: true\n",
        "Design area 500.0 u^2\n",
    )
    assert result.valid
    assert result.drc_count is None
    assert result.unconstrained_paths is None
    assert "drc-unknown" in result.notes
    assert "unconstrained-unknown" in result.notes


# --- file entry points --------------------------------------------------------


def test_parse_files_round_trip_fixtures(tmp_path):
    timing_path = tmp_path / "timing.rpt"
    area_path = tmp_path / "area.rpt"
    drc_path = tmp_path / "drc.rpt"
    timing, area, drc = ok_texts()
    timing_path.write_text(timing, encoding="utf-8")
    area_path.write_text(area, encoding="utf-8")
    drc_path.write_text(drc, encoding="utf-8")
    result = gcd_metrics.parse_final_report_files(
        timing_path=timing_path, area_path=area_path, drc_path=drc_path)
    assert result.valid
    assert result.raw_refs["timing"] == str(timing_path)
    assert result.drc_count == 0


def test_parse_files_missing_inputs_are_invalid(tmp_path):
    result = gcd_metrics.parse_final_report_files(
        timing_path=tmp_path / "no-timing.rpt",
        area_path=tmp_path / "no-area.rpt",
    )
    assert not result.valid
    assert "file-unreadable" in result.reasons
    assert "missing-timing-report" in result.reasons
    assert "missing-area-report" in result.reasons


def test_parse_files_from_checked_in_fixtures():
    result = gcd_metrics.parse_final_report_files(
        timing_path=FIXTURES / "final_timing_ok.rpt",
        area_path=FIXTURES / "final_area_ok.rpt",
        drc_path=FIXTURES / "final_drc_clean.rpt",
    )
    assert result.valid
    assert result.area_um2 == pytest.approx(1234.5)

    bad = gcd_metrics.parse_final_report_files(
        timing_path=FIXTURES / "final_timing_truncated.rpt",
        area_path=FIXTURES / "final_area_ok.rpt",
    )
    assert not bad.valid


# --- misuse raises ------------------------------------------------------------


@pytest.mark.parametrize("bad", [123, 4.5, b"wns: 0", ["x"]])
def test_non_string_report_arguments_raise(bad):
    with pytest.raises(gcd_metrics.MetricsError):
        gcd_metrics.parse_final_reports(bad, "Design area 1 u^2\n")
    with pytest.raises(gcd_metrics.MetricsError):
        gcd_metrics.parse_final_reports("wns: 0\n", bad)


def test_to_dict_round_trip_preserves_evidence():
    timing, area, _ = ok_texts()
    result = gcd_metrics.parse_final_reports(
        timing, area, timing_ref="t", area_ref="a")
    payload = result.to_dict()
    assert payload["valid"] is True
    assert payload["raw_refs"] == {"timing": "t", "area": "a"}
    assert payload["metrics"][0]["unit"] == "um^2"
    for entry in payload["metrics"]:
        assert math.isfinite(entry["value"])


def test_fixtures_are_labeled_synthetic():
    for name in ("final_timing_ok.rpt", "final_area_ok.rpt",
                 "final_drc_clean.rpt", "final_timing_truncated.rpt",
                 "place_timing.rpt"):
        text = read_fixture(name)
        assert "SYNTHETIC" in text
        assert "NOT a measured real-tool report" in text
