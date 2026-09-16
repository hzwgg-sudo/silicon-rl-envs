"""M2-01 Ibex task and constrained edit-surface tests.

Lightweight by design: no EDA tools, Docker, network, or API keys.
Only real files are the packaged ``toolchain.lock.json`` and the stock
``tasks/ibex/task.json`` manifest (plus the GCD files for separation
regression checks).
"""

import hashlib
import json
import math
from pathlib import Path

import pytest

from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad import ibex_config as ibex
from silicon_env.environments.openroad.preflight import (
    load_toolchain_lock,
    run_preflight,
)
from silicon_env.task import TaskSpec
from silicon_env.types import ContractError
from silicon_env.workspace import WorkspaceManager

INJECTION_STRINGS = [
    "0.5; echo pwned",
    "$(rm -rf /)",
    "`id`",
    "[exec evil]",
    "{evil}",
    "0.5\nPLACE_DENSITY=0.9",
    "0.5\r\nFOO=1",
    "# comment",
    "0.5 & make final",
    "0.5 | tee x",
    "variant$(PLACE_DENSITY)",
    "a'b",
    'a"b',
    "a\\b",
    "a<b",
]


# --- manifest loading -------------------------------------------------------


def test_manifest_loads_and_task_validates():
    manifest = ibex.load_manifest_dict()
    spec = ibex.validate_manifest(manifest)
    spec.validate()
    assert spec.task_id == ibex.IBEX_TASK_ID == "ibex-nangate45"
    assert spec.task_version == ibex.IBEX_TASK_VERSION == "0.1.0"
    assert "submit" in spec.allowed_actions
    assert list(spec.allowed_edit_paths) == [ibex.CANDIDATE_RELPATH]
    # Round-trips through strict M0 contracts.
    assert TaskSpec.from_json(spec.to_json()).to_dict() == spec.to_dict()


def test_manifest_matches_module_constants():
    manifest = ibex.load_manifest_dict()
    knobs = manifest["config_surface"]["knobs"]
    assert knobs["PLACE_DENSITY"]["stock"] == ibex.PLACE_DENSITY_STOCK == 0.30
    assert knobs["CORE_UTILIZATION"]["stock"] == ibex.CORE_UTILIZATION_STOCK == 50.0
    assert ibex.load_task_spec().to_dict() == ibex.make_ibex_task().to_dict()
    fixed = manifest["fixed_design"]
    assert fixed["clock_period_ns"] == 2.20
    assert fixed["clock_name"] == "core_clock"
    assert fixed["design"] == "ibex"
    assert fixed["design_name"] == "ibex_core"
    assert fixed["endpoint"] == "final"
    assert list(fixed["rtl"]) == list(ibex.FIXED_RTL)
    assert len(fixed["rtl"]) == 21  # 20 sorted *.sv + prim_clock_gating.v


def test_stock_and_boundary_values_accepted():
    assert ibex.validate_candidate_config({}) == {}
    stock = ibex.stock_candidate_config()
    assert stock == {"PLACE_DENSITY": 0.3, "CORE_UTILIZATION": 50.0}
    assert ibex.validate_candidate_config(dict(stock)) == stock
    assert ibex.validate_candidate_config({"PLACE_DENSITY": ibex.PLACE_DENSITY_MIN})
    assert ibex.validate_candidate_config({"PLACE_DENSITY": ibex.PLACE_DENSITY_MAX})
    assert ibex.validate_candidate_config(
        {"CORE_UTILIZATION": ibex.CORE_UTILIZATION_MIN}
    )
    assert ibex.validate_candidate_config(
        {"CORE_UTILIZATION": ibex.CORE_UTILIZATION_MAX}
    )
    assert ibex.candidate_with_defaults({}) == stock
    edge = {"PLACE_DENSITY": ibex.PLACE_DENSITY_MIN,
            "CORE_UTILIZATION": ibex.CORE_UTILIZATION_MAX}
    assert json.loads(ibex.dumps_candidate_json(edge)) == edge


def test_manifest_task_json_is_strict():
    text = ibex.TASK_MANIFEST_PATH.read_text(encoding="utf-8")
    assert ibex.load_manifest_dict() == json.loads(text)


# --- unknown keys / clock + RTL immutability --------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "CLOCK_PERIOD",
        "clock_period_ns",
        "SDC_FILE",
        "VERILOG_FILES",
        "RTL",
        "ABC_AREA",
        "DESIGN_CONFIG",
        "LIB_FILES",
        "PLACE_DENSITY_LB_ADDON",
        "FLOW_VARIANT",
        "ENDPOINT",
        "anything-else",
    ],
)
def test_unknown_keys_rejected(key):
    with pytest.raises(ContractError):
        ibex.validate_candidate_config({key: 0.5})


def test_clock_and_rtl_cannot_pass_through_surface():
    for attempt in (
        {"CLOCK_PERIOD": 2.0},
        {"clock_period_ns": 2.2},
        {"SDC_FILE": "other.sdc"},
        {"VERILOG_FILES": "other.v"},
        {"PLACE_DENSITY": 0.5, "CLOCK_PERIOD": 2.2},
    ):
        with pytest.raises(ContractError):
            ibex.validate_candidate_config(attempt)
    manifest = ibex.load_manifest_dict()
    assert manifest["fixed_design"]["clock_period_ns"] == 2.20
    assert manifest["fixed_design"]["rtl"] == list(ibex.FIXED_RTL)


# --- ranges / types ---------------------------------------------------------


@pytest.mark.parametrize("key", ["PLACE_DENSITY", "CORE_UTILIZATION"])
def test_out_of_range_rejected(key):
    lo, hi, _ = {
        "PLACE_DENSITY": (
            ibex.PLACE_DENSITY_MIN,
            ibex.PLACE_DENSITY_MAX,
            ibex.PLACE_DENSITY_STOCK,
        ),
        "CORE_UTILIZATION": (
            ibex.CORE_UTILIZATION_MIN,
            ibex.CORE_UTILIZATION_MAX,
            ibex.CORE_UTILIZATION_STOCK,
        ),
    }[key]
    for bad in (lo - 1e-9, hi + 1e-9, -1.0, 1e12, math.nan, math.inf, -math.inf):
        with pytest.raises(ContractError):
            ibex.validate_candidate_config({key: bad})


@pytest.mark.parametrize("bad", [True, False, None, [0.5], {"v": 0.5}])
def test_non_numeric_values_rejected(bad):
    with pytest.raises(ContractError):
        ibex.validate_candidate_config({"PLACE_DENSITY": bad})
    with pytest.raises(ContractError):
        ibex.validate_candidate_config({"CORE_UTILIZATION": bad})


@pytest.mark.parametrize("payload", INJECTION_STRINGS)
def test_injection_strings_rejected(payload):
    with pytest.raises(ContractError):
        ibex.validate_candidate_config({"PLACE_DENSITY": payload})
    with pytest.raises(ContractError):
        ibex.validate_candidate_config({"CORE_UTILIZATION": payload})


def test_non_mapping_candidate_rejected():
    for bad in (None, 0.5, "PLACE_DENSITY=0.5", [("PLACE_DENSITY", 0.5)]):
        with pytest.raises(ContractError):
            ibex.validate_candidate_config(bad)


# --- immutable-path policy --------------------------------------------------


def _manager(tmp_path) -> WorkspaceManager:
    template = tmp_path / "template"
    (template / "flow" / "designs" / "src" / "ibex_sv").mkdir(parents=True)
    (template / ibex.CANDIDATE_RELPATH).write_text("{}\n", encoding="utf-8")
    return WorkspaceManager(
        root_dir=tmp_path / "episodes",
        template_dir=template,
        allowed_edit_paths=[ibex.CANDIDATE_RELPATH],
    )


def test_only_candidate_file_is_editable(tmp_path):
    manager = _manager(tmp_path)
    assert manager.is_editable(ibex.CANDIDATE_RELPATH)
    for protected in ibex.PROTECTED_ASSETS:
        assert not manager.is_editable(protected), protected
    assert not manager.is_editable("flow/designs/src/ibex_sv/ibex_core.sv")
    assert not manager.is_editable("flow/designs/nangate45/ibex/constraint.sdc")


def test_manifest_protected_paths_cover_module_assets():
    manifest = ibex.load_manifest_dict()
    for asset in ibex.PROTECTED_ASSETS:
        assert asset in manifest["protected_paths"]
    overlap = set(manifest["protected_paths"]) & set(
        manifest["task"]["allowed_edit_paths"]
    )
    assert overlap == set()


# --- pinned refs + source hashes --------------------------------------------


def test_pinned_refs_match_lockfile_and_are_immutable():
    lock = load_toolchain_lock()
    assert ibex.ORFS_COMMIT == lock["orfs"]["commit"]
    assert ibex.IMAGE_PINNED_REF == lock["image"]["pinned_ref"]
    assert "@sha256:" in ibex.TOOLCHAIN_REFS["orfs-image"]
    assert len(ibex.ORFS_COMMIT) == 40
    for value in (
        lock["orfs"]["commit"],
        lock["image"]["pinned_ref"],
        ibex.TOOLCHAIN_REFS["orfs"],
        ibex.TOOLCHAIN_REFS["orfs-image"],
    ):
        lowered = value.lower()
        assert "latest" not in lowered
        assert "master" not in lowered


def test_task_sdc_hash_matches_packaged_file_and_differs_from_gcd():
    manifest = ibex.load_manifest_dict()
    digest = hashlib.sha256(ibex.FIXED_SDC_PATH.read_bytes()).hexdigest()
    assert manifest["fixed_design"]["sdc_sha256"] == digest
    assert ibex.FIXED_SDC_PATH.is_file()
    gcd_manifest = gcd.load_manifest_dict()
    assert manifest["fixed_design"]["sdc_sha256"] != gcd_manifest["fixed_design"]["sdc_sha256"]
    assert manifest["fixed_design"]["sdc"] != gcd_manifest["fixed_design"]["sdc"]


# --- source-identity separation from GCD ------------------------------------


def test_ibex_and_gcd_identities_are_separate():
    ibex_manifest = ibex.load_manifest_dict()
    gcd_manifest = gcd.load_manifest_dict()
    assert ibex.IBEX_TASK_ID != gcd.GCD_TASK_ID
    assert ibex.CANDIDATE_RELPATH != gcd.CANDIDATE_RELPATH
    assert ibex_manifest["fixed_design"]["design"] != gcd_manifest["fixed_design"]["design"]
    assert (
        ibex_manifest["fixed_design"]["clock_period_ns"]
        != gcd_manifest["fixed_design"]["clock_period_ns"]
    )
    assert set(ibex_manifest["fixed_design"]["rtl"]).isdisjoint(
        set(gcd_manifest["fixed_design"]["rtl"])
    )
    # Task-scoped protected paths are disjoint; shared platform files and
    # the reports/results outputs contract are intentionally identical.
    ibex_task_scoped = {
        p
        for p in ibex_manifest["protected_paths"]
        if "/ibex" in p or p.startswith("tasks/ibex")
    }
    gcd_task_scoped = {
        p
        for p in gcd_manifest["protected_paths"]
        if "/gcd" in p or p.startswith("tasks/gcd")
    }
    assert ibex_task_scoped and gcd_task_scoped
    assert ibex_task_scoped.isdisjoint(gcd_task_scoped)
    # Stock candidates differ (CORE_UTILIZATION 50 vs 55).
    assert ibex.stock_candidate_config() != gcd.stock_candidate_config()


def test_gcd_behavior_unchanged():
    spec = gcd.load_task_spec()
    assert (gcd.GCD_TASK_ID, gcd.GCD_TASK_VERSION) == ("gcd-nangate45", "0.2.0")
    assert gcd.stock_candidate_config() == {"PLACE_DENSITY": 0.3, "CORE_UTILIZATION": 55.0}
    assert gcd.FIXED_CLOCK_PERIOD_NS == 0.60
    assert spec.task_id == "gcd-nangate45"


# --- required assets / absent dependencies ----------------------------------


def test_required_assets_mirror_module_and_are_flow_scoped():
    manifest = ibex.load_manifest_dict()
    required = manifest["required_assets"]
    assert required == list(ibex.REQUIRED_ASSETS)
    assert len(required) >= len(ibex.FIXED_RTL) + 4
    assert "flow/designs/nangate45/ibex/config.mk" in required
    assert "flow/designs/nangate45/ibex/constraint.sdc" in required
    assert all(isinstance(rel, str) and rel.startswith("flow/") for rel in required)


def test_missing_required_assets_are_detectable(tmp_path):
    required = ibex.load_manifest_dict()["required_assets"]
    missing = [rel for rel in required if not (tmp_path / rel).is_file()]
    assert missing == required  # empty checkout -> every dep reported absent
    # ... while the packaged task SDC itself is present in this repo.
    assert ibex.FIXED_SDC_PATH.is_file()


# --- resource / preflight failure -------------------------------------------


def test_under_resourced_host_fails_preflight():
    lock = load_toolchain_lock()

    def probe(name):
        entry = lock["tools"][name]
        version = (entry.get("version") or {}).get("value")
        return (True, version or "present")

    report = run_preflight(
        lock,
        probe_tool=probe,
        orfs_checkout=None,
        machine="x86_64",
        system="linux",
        mem_gb=0.5,
        cpu_count=1,
    )
    assert not report.ok
    assert any("insufficient RAM" in failure for failure in report.failures)
    assert any("insufficient CPUs" in failure for failure in report.failures)


# --- no design-name conditionals in the generic core -------------------------


def test_generic_core_has_no_ibex_branches():
    from silicon_env.environments.openroad import OPENROAD_LOCKFILE

    package_dir = OPENROAD_LOCKFILE.parent
    for module in (
        "flow.py",
        "evaluator.py",
        "grader.py",
        "environment.py",
        "metrics.py",
        "reports.py",
        "preflight.py",
        "sources.py",
        "config.py",
        "baseline.py",
    ):
        text = (package_dir / module).read_text(encoding="utf-8")
        assert "ibex" not in text.lower(), f"{module} must not branch on ibex"
    cli_text = (package_dir.parent.parent / "cli.py").read_text(encoding="utf-8")
    assert "ibex" not in cli_text.lower(), "cli.py must not branch on ibex"


def test_ibex_config_does_not_import_gcd_config():
    text = Path(ibex.__file__).read_text(encoding="utf-8")
    assert "import config" not in text
    assert "from silicon_env.environments.openroad import config" not in text


# --- budgets -----------------------------------------------------------------


def test_to_task_spec_respects_seed_and_budgets():
    spec = ibex.to_task_spec(seed=7, max_steps=5, max_wallclock_s=120.0, max_tool_calls=4)
    spec.validate()
    assert spec.seed == 7
    assert spec.budgets.max_steps == 5
    assert spec.budgets.max_tool_calls == 4


def test_default_grader_budget_supports_real_flow():
    task = ibex.make_ibex_task()
    assert task.grader.timeout_s == 7200.0
    assert ibex.load_task_spec().grader.timeout_s == task.grader.timeout_s
    assert ibex.make_ibex_task(max_wallclock_s=20).grader.timeout_s == 20
