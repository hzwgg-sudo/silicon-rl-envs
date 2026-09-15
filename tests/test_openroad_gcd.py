"""M1-02 GCD task and constrained edit-surface tests.

Lightweight by design: no EDA tools, Docker, network, or API keys.
Only real files are the packaged ``toolchain.lock.json`` and the stock
``tasks/gcd/task.json`` manifest.
"""

import json
import math

import pytest

from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad.preflight import load_toolchain_lock
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
    manifest = gcd.load_manifest_dict()
    spec = gcd.validate_manifest(manifest)
    spec.validate()
    assert spec.task_id == gcd.GCD_TASK_ID
    assert spec.task_version == gcd.GCD_TASK_VERSION
    assert "submit" in spec.allowed_actions
    assert list(spec.allowed_edit_paths) == [gcd.CANDIDATE_RELPATH]
    # Round-trips through strict M0 contracts.
    assert TaskSpec.from_json(spec.to_json()).to_dict() == spec.to_dict()


def test_manifest_matches_module_constants():
    manifest = gcd.load_manifest_dict()
    knobs = manifest["config_surface"]["knobs"]
    assert knobs["PLACE_DENSITY"]["stock"] == gcd.PLACE_DENSITY_STOCK
    assert knobs["CORE_UTILIZATION"]["stock"] == gcd.CORE_UTILIZATION_STOCK
    assert gcd.load_task_spec().to_dict() == gcd.make_gcd_task().to_dict()


def test_stock_and_boundary_values_accepted():
    assert gcd.validate_candidate_config({}) == {}
    stock = gcd.stock_candidate_config()
    assert gcd.validate_candidate_config(dict(stock)) == stock
    assert gcd.validate_candidate_config({"PLACE_DENSITY": gcd.PLACE_DENSITY_MIN})
    assert gcd.validate_candidate_config({"PLACE_DENSITY": gcd.PLACE_DENSITY_MAX})
    assert gcd.validate_candidate_config(
        {"CORE_UTILIZATION": gcd.CORE_UTILIZATION_MIN}
    )
    assert gcd.validate_candidate_config(
        {"CORE_UTILIZATION": gcd.CORE_UTILIZATION_MAX}
    )
    full = gcd.candidate_with_defaults({})
    assert full == stock
    # Boundary values survive a strict-JSON round-trip.
    edge = {"PLACE_DENSITY": gcd.PLACE_DENSITY_MIN,
            "CORE_UTILIZATION": gcd.CORE_UTILIZATION_MAX}
    assert json.loads(gcd.dumps_candidate_json(edge)) == edge


def test_manifest_task_json_is_strict():
    text = gcd.TASK_MANIFEST_PATH.read_text(encoding="utf-8")
    assert gcd.load_manifest_dict() == json.loads(text)


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
        gcd.validate_candidate_config({key: 0.5})


def test_clock_and_rtl_cannot_pass_through_surface():
    for attempt in (
        {"CLOCK_PERIOD": 1.0},
        {"clock_period_ns": 0.92},
        {"SDC_FILE": "other.sdc"},
        {"VERILOG_FILES": "other.v"},
        {"PLACE_DENSITY": 0.5, "CLOCK_PERIOD": 0.46},
    ):
        with pytest.raises(ContractError):
            gcd.validate_candidate_config(attempt)
    manifest = gcd.load_manifest_dict()
    assert manifest["fixed_design"]["clock_period_ns"] == 0.60
    assert manifest["fixed_design"]["rtl"] == ["flow/designs/src/gcd/gcd.v"]


# --- ranges / types ---------------------------------------------------------


@pytest.mark.parametrize("key", ["PLACE_DENSITY", "CORE_UTILIZATION"])
def test_out_of_range_rejected(key):
    lo, hi, _ = {
        "PLACE_DENSITY": (
            gcd.PLACE_DENSITY_MIN,
            gcd.PLACE_DENSITY_MAX,
            gcd.PLACE_DENSITY_STOCK,
        ),
        "CORE_UTILIZATION": (
            gcd.CORE_UTILIZATION_MIN,
            gcd.CORE_UTILIZATION_MAX,
            gcd.CORE_UTILIZATION_STOCK,
        ),
    }[key]
    for bad in (lo - 1e-9, hi + 1e-9, -1.0, 1e12, math.nan, math.inf, -math.inf):
        with pytest.raises(ContractError):
            gcd.validate_candidate_config({key: bad})


@pytest.mark.parametrize("bad", [True, False, None, [0.5], {"v": 0.5}])
def test_non_numeric_values_rejected(bad):
    with pytest.raises(ContractError):
        gcd.validate_candidate_config({"PLACE_DENSITY": bad})
    with pytest.raises(ContractError):
        gcd.validate_candidate_config({"CORE_UTILIZATION": bad})


@pytest.mark.parametrize("payload", INJECTION_STRINGS)
def test_injection_strings_rejected(payload):
    with pytest.raises(ContractError):
        gcd.validate_candidate_config({"PLACE_DENSITY": payload})
    with pytest.raises(ContractError):
        gcd.validate_candidate_config({"CORE_UTILIZATION": payload})


def test_non_mapping_candidate_rejected():
    for bad in (None, 0.5, "PLACE_DENSITY=0.5", [("PLACE_DENSITY", 0.5)]):
        with pytest.raises(ContractError):
            gcd.validate_candidate_config(bad)


# --- immutable-path policy --------------------------------------------------


def _manager(tmp_path) -> WorkspaceManager:
    template = tmp_path / "template"
    (template / "flow" / "designs" / "src" / "gcd").mkdir(parents=True)
    (template / gcd.CANDIDATE_RELPATH).write_text("{}\n", encoding="utf-8")
    return WorkspaceManager(
        root_dir=tmp_path / "episodes",
        template_dir=template,
        allowed_edit_paths=[gcd.CANDIDATE_RELPATH],
    )


def test_only_candidate_file_is_editable(tmp_path):
    manager = _manager(tmp_path)
    assert manager.is_editable(gcd.CANDIDATE_RELPATH)
    for protected in gcd.PROTECTED_ASSETS:
        assert not manager.is_editable(protected), protected
    assert not manager.is_editable("flow/designs/src/gcd/gcd.v")
    assert not manager.is_editable("flow/designs/nangate45/gcd/constraint.sdc")


def test_manifest_protected_paths_cover_module_assets():
    manifest = gcd.load_manifest_dict()
    for asset in gcd.PROTECTED_ASSETS:
        assert asset in manifest["protected_paths"]
    overlap = set(manifest["protected_paths"]) & set(
        manifest["task"]["allowed_edit_paths"]
    )
    assert overlap == set()


# --- pinned refs ------------------------------------------------------------


def test_pinned_refs_match_lockfile_and_are_immutable():
    lock = load_toolchain_lock()
    assert gcd.ORFS_COMMIT == lock["orfs"]["commit"]
    assert gcd.IMAGE_PINNED_REF == lock["image"]["pinned_ref"]
    assert gcd.FIXED_CLOCK_PERIOD_NS == lock["design"]["clock_period_ns"]
    assert gcd.FIXED_SDC == lock["design"]["sdc"]
    assert list(gcd.FIXED_RTL) == list(lock["design"]["rtl"])
    assert "@sha256:" in gcd.TOOLCHAIN_REFS["orfs-image"]
    assert len(gcd.ORFS_COMMIT) == 40
    for value in (
        lock["orfs"]["commit"],
        lock["image"]["pinned_ref"],
        gcd.TOOLCHAIN_REFS["orfs"],
        gcd.TOOLCHAIN_REFS["orfs-image"],
    ):
        lowered = value.lower()
        assert "latest" not in lowered
        assert "master" not in lowered


def test_to_task_spec_respects_seed_and_budgets():
    spec = gcd.to_task_spec(seed=7, max_steps=5, max_wallclock_s=120.0, max_tool_calls=4)
    spec.validate()
    assert spec.seed == 7
    assert spec.budgets.max_steps == 5
    assert spec.budgets.max_tool_calls == 4


def test_default_grader_budget_supports_real_flow():
    task = gcd.make_gcd_task()
    assert task.grader.timeout_s == 3600.0
    assert gcd.load_task_spec().grader.timeout_s == task.grader.timeout_s
    assert gcd.make_gcd_task(max_wallclock_s=20).grader.timeout_s == 20
