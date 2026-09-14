"""Preflight gate for the pinned OpenROAD GCD toolchain (M1-01).

Verifies, *before* any flow starts, that the host (or container) offers
the expected tools at the locked versions, that the required ORFS assets
exist in a pinned checkout, and that the architecture and resource floor
are satisfied.

Stdlib-only, Python >= 3.10. All host observations are injectable so the
gate is fully testable without EDA tools, Docker, or network access::

    from silicon_env.environments.openroad import preflight

    lock = preflight.load_toolchain_lock()
    report = preflight.run_preflight(
        lock,
        probe_tool=lambda name: (True, "v0.63"),
        orfs_checkout=Path("/opt/orfs"),
        machine="x86_64",
        system="linux",
        mem_gb=16.0,
        cpu_count=8,
    )
    assert report.ok

Exit codes used by ``scripts/check_openroad.py``: 0 ok, 1 preflight
failures, 2 usage / unreadable lockfile.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from silicon_env.environments.openroad import OPENROAD_LOCKFILE

SCHEMA_VERSION = 1

#: Tokens that must never appear as (part of) a scored ref. Compared
#: case-insensitively against whole ``/``- or ``:``-separated segments.
MUTABLE_TOKENS = ("latest", "master")

_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")

ProbeFn = Callable[[str], tuple[bool, str]]


class PreflightError(ValueError):
    """Lockfile misuse or unreadable inputs (usage error, exit 2)."""


@dataclass
class CheckReport:
    """Outcome of :func:`run_preflight`."""

    ok: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def message(self) -> str:
        lines = []
        for item in self.failures:
            lines.append(f"FAIL: {item}")
        for item in self.warnings:
            lines.append(f"WARN: {item}")
        if not lines:
            lines.append("preflight ok")
        return "\n".join(lines)


def default_lockfile_path() -> Path:
    return OPENROAD_LOCKFILE


def load_toolchain_lock(path: str | os.PathLike[str] | None = None) -> dict:
    """Parse the lockfile as strict JSON and return the payload dict."""
    target = Path(path) if path is not None else default_lockfile_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreflightError(f"cannot read lockfile {target}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PreflightError(f"lockfile {target} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PreflightError(f"lockfile {target} must decode to an object")
    errors = validate_toolchain_lock(payload)
    if errors:
        raise PreflightError(f"lockfile {target} is invalid: " + "; ".join(errors))
    return payload


def _has_mutable_token(value: str) -> str | None:
    segments = re.split(r"[/:@#\s]+", value.lower())
    for token in MUTABLE_TOKENS:
        if token in segments:
            return token
    return None


def _is_commit(value: object) -> bool:
    return isinstance(value, str) and _COMMIT_RE.match(value) is not None


def validate_toolchain_lock(data: Mapping) -> list[str]:
    """Return a list of validation errors (empty means valid).

    Checks the scored inputs only: schema version, pinned ORFS commit,
    pinned source commits, digest-pinned image ref, and the presence of
    the sections the preflight gate consumes. Never touches the network,
    Docker, or EDA tools.
    """
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return ["lockfile must be an object"]
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for section in (
        "design",
        "orfs",
        "sources",
        "image",
        "tools",
        "required_assets",
        "platform_support",
        "resources",
    ):
        if section not in data:
            errors.append(f"missing required section: {section}")

    orfs = data.get("orfs")
    if isinstance(orfs, Mapping):
        for key in ("repo", "tag", "commit"):
            value = orfs.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"orfs.{key} must be a non-empty string")
            elif _has_mutable_token(value):
                errors.append(f"orfs.{key} {value!r} looks mutable (floating ref)")
        if not _is_commit(orfs.get("commit")):
            errors.append("orfs.commit must be a 40-char hex commit SHA")
    else:
        errors.append("orfs must be an object")

    sources = data.get("sources")
    if isinstance(sources, Mapping):
        if not sources:
            errors.append("sources must not be empty")
        for name, entry in sources.items():
            if not isinstance(entry, Mapping):
                errors.append(f"sources.{name} must be an object")
                continue
            commit = entry.get("commit")
            if not _is_commit(commit):
                errors.append(f"sources.{name}.commit must be a 40-char hex commit SHA")
            for key in ("repo",):
                value = entry.get(key)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"sources.{name}.{key} must be a non-empty string")
                elif _has_mutable_token(value):
                    errors.append(f"sources.{name}.{key} {value!r} looks mutable")
    else:
        errors.append("sources must be an object")

    image = data.get("image")
    if isinstance(image, Mapping):
        digest = image.get("digest")
        if not isinstance(digest, str) or _DIGEST_RE.match(digest) is None:
            errors.append("image.digest must look like 'sha256:<64 hex chars>'")
        pinned = image.get("pinned_ref")
        if not isinstance(pinned, str) or "@sha256:" not in pinned:
            errors.append("image.pinned_ref must be a digest-pinned ref ('...@sha256:...')")
        else:
            token = _has_mutable_token(pinned)
            if token:
                errors.append(f"image.pinned_ref {pinned!r} looks mutable (floating ref)")
        tag = image.get("tag")
        if not isinstance(tag, str) or not tag.strip():
            errors.append("image.tag must be a non-empty string")
        elif _has_mutable_token(tag):
            errors.append(f"image.tag {tag!r} looks mutable (floating ref)")
    else:
        errors.append("image must be an object")

    tools = data.get("tools")
    if isinstance(tools, Mapping):
        if not tools:
            errors.append("tools must not be empty")
    else:
        errors.append("tools must be an object")

    assets = data.get("required_assets")
    if not isinstance(assets, list) or not assets or any(
        not isinstance(a, str) or not a.strip() for a in assets
    ):
        errors.append("required_assets must be a non-empty list of path strings")

    support = data.get("platform_support")
    if isinstance(support, Mapping):
        for key in ("os", "arch"):
            values = support.get(key)
            if not isinstance(values, list) or not values:
                errors.append(f"platform_support.{key} must be a non-empty list")
    else:
        errors.append("platform_support must be an object")
    return errors


def default_probe_tool(name: str) -> tuple[bool, str]:
    """Probe a tool on PATH; return (found, version_string)."""
    if not isinstance(name, str) or not name.strip():
        return (False, "")
    if shutil.which(name) is None:
        return (False, "")
    flag_map = {"openroad": ["-version"], "yosys": ["-V"]}
    args = [name, *(flag_map.get(name, ["--version"]))]
    try:
        proc = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return (True, "")
    try:
        text = proc.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        text = ""
    first_line = text.splitlines()[0].strip() if text else ""
    return (True, first_line)


def _host_memory_gb() -> float | None:
    """Best-effort total RAM in GiB; None when it cannot be determined."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if isinstance(pages, int) and isinstance(page_size, int) and pages > 0:
            return pages * page_size / (1024**3)
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _normalize_arch(machine: str) -> str:
    return machine.strip().lower()


def run_preflight(
    lock: Mapping,
    *,
    probe_tool: ProbeFn | None = None,
    orfs_checkout: str | os.PathLike[str] | None = None,
    machine: str | None = None,
    system: str | None = None,
    mem_gb: float | None = None,
    cpu_count: int | None = None,
    min_ram_gb: float | None = None,
    min_cpus: int | None = None,
) -> CheckReport:
    """Run every preflight gate; never raises on check content.

    Only :class:`PreflightError` (bad lock shape) propagates; every
    host/tool mismatch becomes a ``failures`` entry so callers get a
    diagnosable message and a nonzero exit code.
    """
    errors = validate_toolchain_lock(lock)
    if errors:
        raise PreflightError("invalid lockfile: " + "; ".join(errors))
    probe = probe_tool or default_probe_tool
    failures: list[str] = []
    warnings: list[str] = []

    # -- tools + versions -------------------------------------------------
    tools = lock["tools"]
    for name, entry in tools.items():
        try:
            found, version = probe(name)
        except Exception as exc:
            failures.append(f"tool {name!r}: probe crashed ({exc})")
            continue
        if not found:
            failures.append(
                f"tool {name!r} not found on PATH; "
                f"run the Linux route with {lock['image']['pinned_ref']}"
            )
            continue
        expected = entry.get("version") if isinstance(entry, Mapping) else None
        status = expected.get("status") if isinstance(expected, Mapping) else None
        value = expected.get("value") if isinstance(expected, Mapping) else None
        if status == "unverified" or (status is None and not value):
            warnings.append(
                f"tool {name!r} found ({version or 'unknown version'}) but the "
                "locked version is TBD-unverified; a real pinned-image run "
                "must record it before scoring"
            )
        elif status == "any":
            continue
        elif isinstance(value, str) and value:
            if version.strip() != value.strip():
                failures.append(
                    f"tool {name!r} version mismatch: found {version!r}, "
                    f"locked {value!r}"
                )
        else:
            warnings.append(f"tool {name!r} has no locked version to compare against")

    # -- required assets --------------------------------------------------
    checkout_raw = (
        os.fspath(orfs_checkout)
        if orfs_checkout is not None
        else os.environ.get("ORFS_CHECKOUT", "").strip()
    )
    if not checkout_raw:
        failures.append(
            "ORFS checkout path not given; pass --orfs-checkout or set "
            "ORFS_CHECKOUT to the pinned commit "
            f"({lock['orfs']['commit'][:12]}...) before starting a flow"
        )
    else:
        root = Path(checkout_raw)
        if not root.is_dir():
            failures.append(f"ORFS checkout {str(root)!r} does not exist or is not a dir")
        else:
            for rel in lock["required_assets"]:
                if not (root / rel).is_file():
                    failures.append(f"required asset missing: {rel} (under {root})")

    # -- platform ---------------------------------------------------------
    host_system = (system if system is not None else sys.platform).lower()
    allowed_os = [str(o).lower() for o in lock["platform_support"]["os"]]
    if host_system not in allowed_os and not any(
        host_system.startswith(prefix) for prefix in allowed_os
    ):
        failures.append(
            f"unsupported OS {host_system!r} (supported: {allowed_os}); "
            "use the Linux route for EDA runs"
        )
    host_arch = _normalize_arch(machine if machine is not None else platform.machine())
    allowed_arch = {_normalize_arch(str(a)) for a in lock["platform_support"]["arch"]}
    if host_arch not in allowed_arch:
        failures.append(
            f"unsupported architecture {host_arch!r} (supported: "
            f"{sorted(allowed_arch)}); use the Linux route for EDA runs"
        )

    # -- resources --------------------------------------------------------
    required = lock.get("resources", {}).get("required_minimum", {})
    need_ram = float(min_ram_gb) if min_ram_gb is not None else float(required.get("memory_gb", 0))
    need_cpu = int(min_cpus) if min_cpus is not None else int(required.get("cpus", 0))
    host_mem = mem_gb if mem_gb is not None else _host_memory_gb()
    host_cpu = cpu_count if cpu_count is not None else (os.cpu_count() or 0)
    if need_ram and host_mem is not None and host_mem < need_ram:
        failures.append(
            f"insufficient RAM: {host_mem:.1f} GiB available, "
            f"{need_ram:.1f} GiB required for a reference run"
        )
    elif need_ram and host_mem is None:
        warnings.append("total RAM could not be determined; resource floor not enforced")
    if need_cpu and host_cpu and host_cpu < need_cpu:
        failures.append(
            f"insufficient CPUs: {host_cpu} available, {need_cpu} required "
            "for a reference run"
        )

    # -- reference-run honesty gate ---------------------------------------
    ref = lock.get("resources", {}).get("reference_run", {})
    if isinstance(ref, Mapping) and ref.get("status", "").startswith("TBD"):
        warnings.append(
            "reference-run RAM/time is TBD-unverified; scored runs must "
            "measure and record it on the Linux route first"
        )

    return CheckReport(ok=not failures, failures=failures, warnings=warnings)


__all__ = [
    "MUTABLE_TOKENS",
    "ProbeFn",
    "PreflightError",
    "CheckReport",
    "SCHEMA_VERSION",
    "default_lockfile_path",
    "default_probe_tool",
    "load_toolchain_lock",
    "run_preflight",
    "validate_toolchain_lock",
]
