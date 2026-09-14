"""M1-01 OpenROAD GCD toolchain tests.

Lightweight by design: no EDA tools, Docker, network, or API keys. Tool
probes, architecture, OS, resources, and the ORFS checkout are all
injected fakes; only real file is the packaged ``toolchain.lock.json``.
"""

import copy
import importlib.util
import json
import os
from pathlib import Path

import pytest

from silicon_env.environments.openroad import (
    EXPECTED_TOOLS,
    OPENROAD_DESIGN,
    OPENROAD_LOCKFILE,
    OPENROAD_PLATFORM,
)
from silicon_env.environments.openroad.preflight import (
    CheckReport,
    PreflightError,
    load_toolchain_lock,
    run_preflight,
    validate_toolchain_lock,
)

GOOD_MACHINE = "x86_64"
GOOD_SYSTEM = "linux"


def load_lock() -> dict:
    return load_toolchain_lock(OPENROAD_LOCKFILE)


def make_checkout(tmp_path: Path, lock: dict) -> Path:
    root = tmp_path / "orfs"
    for rel in lock["required_assets"]:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("placeholder\n", encoding="utf-8")
    return root


def good_probe(name: str) -> tuple[bool, str]:
    return (True, f"{name} fake-version")


def good_kwargs(tmp_path: Path, lock: dict) -> dict:
    return {
        "probe_tool": good_probe,
        "orfs_checkout": make_checkout(tmp_path, lock),
        "machine": GOOD_MACHINE,
        "system": GOOD_SYSTEM,
        "mem_gb": 16.0,
        "cpu_count": 8,
    }


# --- lockfile -------------------------------------------------------------


def test_lockfile_exists_and_is_strict_json():
    assert OPENROAD_LOCKFILE.is_file()
    text = OPENROAD_LOCKFILE.read_text(encoding="utf-8")
    assert load_toolchain_lock(OPENROAD_LOCKFILE) == json.loads(text)


def test_lockfile_validates_clean():
    assert validate_toolchain_lock(load_lock()) == []


def test_lockfile_has_no_mutable_scored_refs():
    lock = load_lock()
    scored = [
        lock["orfs"]["tag"],
        lock["orfs"]["commit"],
        lock["image"]["tag"],
        lock["image"]["pinned_ref"],
        lock["image"]["digest"],
    ]
    scored += [entry["commit"] for entry in lock["sources"].values()]
    for value in scored:
        lowered = value.lower()
        assert "latest" not in lowered, f"mutable ref: {value!r}"
        assert "master" not in lowered, f"mutable ref: {value!r}"
    assert "@sha256:" in lock["image"]["pinned_ref"]


def test_lockfile_records_unverified_versions_honestly():
    lock = load_lock()
    for name in ("openroad", "yosys"):
        entry = lock["tools"][name]["version"]
        assert entry["status"] == "unverified"
        assert entry["value"] is None
    ref = lock["resources"]["reference_run"]
    assert ref["status"].startswith("TBD")
    assert ref["wallclock_s"] is None and ref["peak_rss_gb"] is None


def test_shared_constants_match_lockfile():
    lock = load_lock()
    assert OPENROAD_DESIGN == lock["design"]["name"]
    assert OPENROAD_PLATFORM == lock["design"]["platform"]
    assert set(EXPECTED_TOOLS) <= set(lock["tools"])


def test_reject_floating_image_tag():
    lock = load_lock()
    bad = copy.deepcopy(lock)
    bad["image"]["tag"] = "latest"
    bad["image"]["pinned_ref"] = "docker.io/openroad/orfs:latest"
    errors = validate_toolchain_lock(bad)
    assert errors, "floating image tag must be rejected"
    with pytest.raises(PreflightError):
        run_preflight(bad, **good_kwargs(Path(os.environ.get("TMPDIR", "/tmp")), bad))


def test_reject_short_commit():
    lock = load_lock()
    bad = copy.deepcopy(lock)
    bad["orfs"]["commit"] = "036d1062"
    assert validate_toolchain_lock(bad)


def test_load_missing_lockfile_raises_usage_error(tmp_path):
    with pytest.raises(PreflightError):
        load_toolchain_lock(tmp_path / "nope.json")


# --- preflight ------------------------------------------------------------


def test_preflight_ok_with_all_good_fakes(tmp_path):
    lock = load_lock()
    report = run_preflight(lock, **good_kwargs(tmp_path, lock))
    assert isinstance(report, CheckReport)
    assert report.ok, report.message()
    assert report.failures == []
    # TBD entries (binary versions, reference run) must surface as warnings.
    assert report.warnings, "expected TBD-unverified warnings"


def test_preflight_missing_tool_fails(tmp_path):
    lock = load_lock()

    def probe(name: str) -> tuple[bool, str]:
        return (False, "") if name == "openroad" else (True, "v")

    kwargs = good_kwargs(tmp_path, lock)
    kwargs["probe_tool"] = probe
    report = run_preflight(lock, **kwargs)
    assert not report.ok
    assert any("openroad" in f for f in report.failures)


def test_preflight_version_mismatch_fails(tmp_path):
    lock = copy.deepcopy(load_lock())
    lock["tools"]["yosys"]["version"] = {"status": "verified", "value": "yosys v9.99"}
    kwargs = good_kwargs(tmp_path, lock)
    report = run_preflight(lock, **kwargs)
    assert not report.ok
    assert any("yosys" in f and "mismatch" in f for f in report.failures)


def test_preflight_version_match_passes_tool_gate(tmp_path):
    lock = copy.deepcopy(load_lock())
    lock["tools"]["yosys"]["version"] = {"status": "verified", "value": "yosys fake-version"}
    kwargs = good_kwargs(tmp_path, lock)
    report = run_preflight(lock, **kwargs)
    assert not any("mismatch" in f for f in report.failures)


def test_preflight_missing_asset_fails(tmp_path):
    lock = load_lock()
    kwargs = good_kwargs(tmp_path, lock)
    root = kwargs["orfs_checkout"]
    missing = root / lock["required_assets"][0]
    missing.unlink()
    report = run_preflight(lock, **kwargs)
    assert not report.ok
    assert any("missing" in f for f in report.failures)


def test_preflight_missing_checkout_fails(tmp_path):
    lock = load_lock()
    kwargs = good_kwargs(tmp_path, lock)
    kwargs["orfs_checkout"] = tmp_path / "does-not-exist"
    report = run_preflight(lock, **kwargs)
    assert not report.ok
    assert any("checkout" in f for f in report.failures)


def test_preflight_wrong_arch_fails(tmp_path):
    lock = load_lock()
    kwargs = good_kwargs(tmp_path, lock)
    kwargs["machine"] = "arm64"
    report = run_preflight(lock, **kwargs)
    assert not report.ok
    assert any("architecture" in f for f in report.failures)


def test_preflight_wrong_os_fails(tmp_path):
    lock = load_lock()
    kwargs = good_kwargs(tmp_path, lock)
    kwargs["system"] = "darwin"
    report = run_preflight(lock, **kwargs)
    assert not report.ok
    assert any("OS" in f for f in report.failures)


def test_preflight_insufficient_ram_fails(tmp_path):
    lock = load_lock()
    kwargs = good_kwargs(tmp_path, lock)
    kwargs["mem_gb"] = 1.0
    report = run_preflight(lock, **kwargs)
    assert not report.ok
    assert any("RAM" in f for f in report.failures)


def test_preflight_configurable_floor_overrides_lockfile(tmp_path):
    lock = load_lock()
    kwargs = good_kwargs(tmp_path, lock)
    kwargs["mem_gb"] = 16.0
    kwargs["min_ram_gb"] = 64.0
    assert not run_preflight(lock, **kwargs).ok
    kwargs["min_ram_gb"] = 1.0
    kwargs["min_cpus"] = 1
    assert run_preflight(lock, **kwargs).ok


def test_check_script_is_importable_and_wires_exit_codes(tmp_path):
    script = Path(__file__).resolve().parent.parent / "scripts" / "check_openroad.py"
    assert script.is_file()
    spec = importlib.util.spec_from_file_location("check_openroad", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lock = load_lock()
    root = make_checkout(tmp_path, lock)
    # Real local tools are absent; point PATH lookups at fakes via env is
    # unnecessary -- instead assert the CLI at least parses and runs the
    # usage-error path cleanly for a missing lockfile.
    assert module.main(["--lockfile", str(tmp_path / "nope.json")]) == 2
    _ = root  # checkout used by other tests; CLI happy-path is covered by run_preflight


RUN_REAL_PROBES = os.environ.get("SILICON_RUN_OPENROAD_PROBES", "") == "1"


@pytest.mark.skipif(
    not RUN_REAL_PROBES, reason="needs real pinned image/tools (opt-in only)"
)
def test_real_pinned_tool_probes():
    """Opt-in only: pull the pinned image and record version probes.

    Never runs in default pytest. Requires a Linux x86_64 host with a
    container runtime and network access. Blocked on the Mac dev laptop.
    """
    lock = load_lock()
    report = run_preflight(lock, orfs_checkout=os.environ.get("ORFS_CHECKOUT", ""))
    assert report.ok, report.message()
