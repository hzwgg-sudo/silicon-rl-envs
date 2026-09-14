"""M1-05 repeatable GCD reference baseline tests.

Lightweight by design: no EDA tools, Docker, network, or API keys.
All runs are fake :class:`GcdMetrics` (or a fake ``run_once``
callable); the only real files are the packaged ``baseline.json``
placeholder (asserted TBD-unverified, never scored) and
``toolchain.lock.json`` pins via ``config``.
"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from silicon_env.environments.openroad import baseline as bl
from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad.metrics import GcdMetrics

TASK_BASELINE = Path(bl.__file__).with_name("tasks") / "gcd" / "baseline.json"
GENERATOR_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "generate_openroad_baseline.py"
)

AREA = 1234.5
WNS = -0.023
TNS = -0.145
TOLS = {"area_rel": 0.01, "wns_abs_ns": 0.005, "tns_abs_ns": 0.01}


def make_metrics(
    area: float = AREA,
    wns: float = WNS,
    tns: float = TNS,
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


def make_record(**overrides) -> dict:
    record = bl.build_baseline_record(
        [make_metrics(), make_metrics(), make_metrics()],
        tolerances=dict(TOLS),
        run_ids=["run-0", "run-1", "run-2"],
        seeds=[0, 1, 2],
    )
    record.update(overrides)
    return record


def load_generator():
    assert GENERATOR_SCRIPT.is_file()
    spec = importlib.util.spec_from_file_location("generate_openroad_baseline", GENERATOR_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- fingerprint -------------------------------------------------------------


def test_fingerprint_stable_and_excludes_runtimes():
    first = bl.fingerprint_inputs(gcd.stock_candidate_config())
    second = bl.fingerprint_inputs(gcd.stock_candidate_config())
    assert first == second
    assert first["orfs_commit"] == gcd.ORFS_COMMIT
    assert first["image_pinned_ref"] == gcd.IMAGE_PINNED_REF
    assert len(first["candidate_hash"]) == 64
    assert len(first["protected_hash"]) == 64
    assert set(first) == {"orfs_commit", "image_pinned_ref", "candidate_hash", "protected_hash"}


def test_orfs_commit_change_invalidates():
    record = make_record()
    tampered = copy.deepcopy(record)
    tampered["orfs_commit"] = "0" * 40
    with pytest.raises(bl.BaselineError, match="fingerprint mismatch"):
        bl.validate_baseline(tampered)


def test_image_ref_change_invalidates():
    record = make_record()
    tampered = copy.deepcopy(record)
    tampered["image_pinned_ref"] = record["image_pinned_ref"] + "-tampered"
    with pytest.raises(bl.BaselineError, match="fingerprint mismatch"):
        bl.validate_baseline(tampered)


def test_candidate_change_invalidates_even_with_recomputed_hash():
    record = make_record()
    tampered = copy.deepcopy(record)
    tampered["candidate"] = {"PLACE_DENSITY": 0.5, "CORE_UTILIZATION": 55.0}
    tampered["candidate_hash"] = bl.compute_candidate_hash(tampered["candidate"])
    with pytest.raises(bl.BaselineError, match="not the pinned stock candidate"):
        bl.validate_baseline(tampered)


def test_stale_candidate_hash_invalidates():
    record = make_record()
    tampered = copy.deepcopy(record)
    tampered["candidate_hash"] = "0" * 64
    with pytest.raises(bl.BaselineError, match="fingerprint mismatch"):
        bl.validate_baseline(tampered)


def test_protected_asset_change_invalidates():
    record = make_record()
    expected = bl.fingerprint_inputs(
        gcd.stock_candidate_config(),
        protected_assets=[*gcd.PROTECTED_ASSETS, "flow/designs/nangate45/gcd/extra.sdc"],
    )
    with pytest.raises(bl.BaselineError, match="fingerprint mismatch"):
        bl.validate_baseline(record, expected_fingerprint=expected)


def test_unknown_schema_rejected():
    record = make_record()
    record["schema_version"] = 999
    with pytest.raises(bl.BaselineError, match="unknown baseline schema"):
        bl.validate_baseline(record)


def test_task_drift_rejected():
    record = make_record()
    record["task_version"] = "9.9.9"
    with pytest.raises(bl.BaselineError, match="task_version"):
        bl.validate_baseline(record)


# --- tolerance boundaries (inclusive) ----------------------------------------


def test_area_boundary_just_inside_passes_just_outside_fails():
    # Exact binary values (1.25, 1024.0) so the boundary is not
    # blurred by decimal-float rounding; semantics stay inclusive (<=).
    tols = {"area_rel": 0.25, "wns_abs_ns": 0.5, "tns_abs_ns": 0.5}
    base = {"area_um2": 1024.0, "wns_ns": -4.0, "tns_ns": -8.0}
    inside = dict(base, area_um2=1280.0)  # exactly +25%
    outside = dict(base, area_um2=1280.0 + 1e-6)
    assert bl.within_tolerances(inside, base, tols)
    assert not bl.within_tolerances(outside, base, tols)


def test_wns_boundary_just_inside_passes_just_outside_fails():
    tols = {"area_rel": 0.25, "wns_abs_ns": 0.25, "tns_abs_ns": 0.5}
    base = {"area_um2": 1024.0, "wns_ns": -4.0, "tns_ns": -8.0}
    inside = dict(base, wns_ns=-3.75)  # exactly +0.25 ns
    outside = dict(base, wns_ns=-3.75 + 1e-9)
    assert bl.within_tolerances(inside, base, tols)
    assert not bl.within_tolerances(outside, base, tols)


def test_tns_boundary_just_inside_passes_just_outside_fails():
    tols = {"area_rel": 0.25, "wns_abs_ns": 0.5, "tns_abs_ns": 0.25}
    base = {"area_um2": 1024.0, "wns_ns": -4.0, "tns_ns": -8.0}
    inside = dict(base, tns_ns=-8.25)  # exactly -0.25 ns
    outside = dict(base, tns_ns=-8.25 - 1e-9)
    assert bl.within_tolerances(inside, base, tols)
    assert not bl.within_tolerances(outside, base, tols)


def test_nonfinite_and_zero_area_never_match():
    base = {"area_um2": AREA, "wns_ns": WNS, "tns_ns": TNS}
    assert not bl.within_tolerances(dict(base, area_um2=float("nan")), base, TOLS)
    assert not bl.within_tolerances(dict(base, wns_ns=float("inf")), base, TOLS)
    assert not bl.within_tolerances(dict(base, area_um2=0.0), base, TOLS)
    assert not bl.within_tolerances(base, dict(base, area_um2=0.0), TOLS)


def test_check_metrics_end_to_end_drift_reason():
    record = make_record()
    assert bl.check_metrics_against_baseline(record, make_metrics()) == []
    drifted = make_metrics(area=AREA * 1.05)
    assert bl.check_metrics_against_baseline(record, drifted) == ["metric-drift"]
    assert bl.check_metrics_against_baseline(record, make_metrics(valid=False)) == [
        "candidate-invalid"
    ]


def test_invalid_tolerances_rejected():
    base = {"area_um2": AREA, "wns_ns": WNS, "tns_ns": TNS}
    with pytest.raises(bl.BaselineError, match="invalid tolerance"):
        bl.within_tolerances(base, base, {"area_rel": 0.0, "wns_abs_ns": 0.005, "tns_abs_ns": 0.01})


# --- TBD placeholder fails closed --------------------------------------------


def test_checked_baseline_is_honest_tbd_placeholder():
    assert TASK_BASELINE.is_file()
    payload = json.loads(TASK_BASELINE.read_text(encoding="utf-8"))
    assert payload["status"] == "TBD-unverified"
    assert payload["metrics"]["area_um2"] is None
    assert payload["metrics"]["wns_ns"] is None
    assert payload["metrics"]["tns_ns"] is None
    assert payload["candidate_hash"] == "TBD-unverified"
    assert "blocked" in json.dumps(payload).lower() or "TBD" in json.dumps(payload)


def test_tbd_baseline_fails_scoring_use():
    payload = bl.load_baseline(TASK_BASELINE)
    with pytest.raises(bl.BaselineError, match="not usable for scoring"):
        bl.validate_baseline(payload)


def test_zero_area_baseline_rejected():
    record = make_record()
    record["metrics"] = dict(record["metrics"], area_um2=0.0)
    with pytest.raises(bl.BaselineError, match="invalid baseline area"):
        bl.validate_baseline(record)


# --- generator with fake runs --------------------------------------------------


def test_generator_consistent_record_for_identical_fakes(tmp_path):
    gen = load_generator()
    seen: list[Path] = []

    def fake_run_once(scratch_dir: Path, seed: int) -> GcdMetrics:
        assert scratch_dir.is_dir()
        assert list(scratch_dir.iterdir()) == []  # fresh workspace
        seen.append(scratch_dir)
        return make_metrics()

    out = tmp_path / "baseline.json"
    record = gen.generate_baseline(output_path=out, run_once=fake_run_once, seeds=[0, 1, 2])
    assert len(seen) == 3
    assert len({str(p) for p in seen}) == 3  # distinct fresh workspaces
    assert out.is_file()
    stored = json.loads(out.read_text(encoding="utf-8"))
    assert stored["status"] == "verified"
    assert stored["metrics"]["area_um2"] == pytest.approx(AREA)
    assert stored["provenance"]["seeds"] == [0, 1, 2]
    assert len(stored["provenance"]["run_ids"]) == 3
    assert stored["resources"]["wallclock_s"] >= 0.0
    bl.validate_baseline(bl.load_baseline(out))

    out2 = tmp_path / "baseline2.json"
    record2 = gen.generate_baseline(output_path=out2, run_once=fake_run_once, seeds=[0, 1, 2])
    for key in ("metrics", "candidate_hash", "protected_hash", "orfs_commit"):
        assert record2[key] == record[key]  # deterministic modulo measured resources


def test_generator_fails_on_invalid_fake_and_writes_nothing(tmp_path):
    gen = load_generator()
    out = tmp_path / "baseline.json"
    with pytest.raises(bl.BaselineError, match="validity"):
        gen.generate_baseline(
            output_path=out,
            run_once=lambda scratch, seed: make_metrics(valid=False),
        )
    assert not out.exists()


def test_generator_fails_on_drifted_fake_and_writes_nothing(tmp_path):
    gen = load_generator()

    def drifting(scratch: Path, seed: int) -> GcdMetrics:
        if seed == 2:
            return make_metrics(area=AREA * 1.05)
        return make_metrics()

    out = tmp_path / "baseline.json"
    with pytest.raises(bl.BaselineError, match="drift"):
        gen.generate_baseline(output_path=out, run_once=drifting, seeds=[0, 1, 2])
    assert not out.exists()


def test_generator_script_usage_and_bogus_checkout(tmp_path, monkeypatch):
    gen = load_generator()
    monkeypatch.delenv("ORFS_CHECKOUT", raising=False)
    assert gen.main([]) == 2  # missing --orfs-checkout without $ORFS_CHECKOUT
    out = tmp_path / "baseline.json"
    assert (
        gen.main(["--orfs-checkout", str(tmp_path / "nope"), "--output", str(out)]) == 1
    )
    assert not out.exists()


def test_build_requires_exactly_three_runs():
    with pytest.raises(bl.BaselineError, match="exactly 3 runs"):
        bl.build_baseline_record([make_metrics(), make_metrics()])
