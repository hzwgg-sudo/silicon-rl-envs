"""First GCD area-under-timing task with a constrained edit surface (M1-02).

The task optimizes physical design only: RTL, SDC, libraries, and grading
inputs are immutable. The agent may override exactly two numeric knobs --
``PLACE_DENSITY`` and ``CORE_UTILIZATION`` -- through the single editable
file :data:`CANDIDATE_RELPATH`. Everything else (notably the fixed 0.46 ns
clock and the RTL sources) is outside the allowed interface, so a candidate
cannot relax timing constraints or alter function through it.

Stock values are read off the pinned ORFS revision (see
``toolchain.lock.json``):

- ``PLACE_DENSITY`` stock ``0.30``: platform default from
  ``flow/platforms/nangate45/config.mk`` (``export PLACE_DENSITY ?= 0.30``);
  the GCD ``config.mk`` does not override it.
- ``CORE_UTILIZATION`` stock ``55``: GCD design default from
  ``flow/designs/nangate45/gcd/config.mk``
  (``export CORE_UTILIZATION ?= 55``).

Bounds are task-supported safe ranges inside the ORFS semantic ranges
(density 0-1, utilization 0-100 percent), recorded here as supported by the
pin. Flow execution, metrics, and the grader land in follow-on tickets
(M1-03+); this module defines no flow, metrics, or grading logic.

Stdlib-only, Python >= 3.10. Validation is pure (no I/O except explicit
manifest loaders, no subprocess, no network).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from silicon_env.task import TaskSpec
from silicon_env.types import (
    Budget,
    ContractError,
    GraderConfig,
    loads_dict_strict,
)

# --- task identity ---------------------------------------------------------

GCD_TASK_ID = "gcd-nangate45"
GCD_TASK_VERSION = "0.1.0"
GCD_GRADER_ID = "gcd-area-grader"
GCD_GRADER_VERSION = "0.1.0"

#: The single editable file. The only entry of ``allowed_edit_paths``.
CANDIDATE_RELPATH = "candidate.json"

#: Fixed physical-design endpoint for the later flow ticket (M1-03).
#: The default ``make`` target builds the full flow ending at final
#: post-detailed-route reports.
GCD_ENDPOINT = "final"

# --- pinned sources (mirror toolchain.lock.json, M1-01) --------------------

ORFS_COMMIT = "036d106273e66855cd5214d49518fd0f0df7de61"
ORFS_TAG = "26Q2"
IMAGE_PINNED_REF = (
    "docker.io/openroad/orfs:26Q2@sha256:"
    "7832ae885e62933fcbfc486fbd9133f8c3bd1206d15c96e93bfad97432947b61"
)
IMAGE_DIGEST = (
    "sha256:7832ae885e62933fcbfc486fbd9133f8c3bd1206d15c96e93bfad97432947b61"
)

SOURCE_REF = f"orfs:{ORFS_COMMIT}:flow/designs/nangate45/gcd"
TOOLCHAIN_REFS = {
    "orfs": ORFS_COMMIT,
    "orfs-image": IMAGE_PINNED_REF,
}

# --- fixed design facts (immutable; not settable via the candidate) --------

FIXED_DESIGN = "gcd"
FIXED_PLATFORM = "nangate45"
FIXED_CLOCK_PERIOD_NS = 0.46
FIXED_CLOCK_NAME = "core_clock"
FIXED_SDC = "flow/designs/nangate45/gcd/constraint.sdc"
FIXED_DESIGN_CONFIG = "flow/designs/nangate45/gcd/config.mk"
FIXED_RTL = ("flow/designs/src/gcd/gcd.v",)
FIXED_CORNERS = "typical (NangateOpenCellLibrary_typical.lib)"
FIXED_LIB = "flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib"

#: Protected (read-only) assets, recorded as groundwork for later tickets.
#: None of these may appear in ``allowed_edit_paths``.
PROTECTED_ASSETS = (
    "flow/designs/src/gcd/gcd.v",
    "flow/designs/nangate45/gcd/constraint.sdc",
    "flow/designs/nangate45/gcd/config.mk",
    "flow/platforms/nangate45/config.mk",
    "flow/platforms/nangate45/lef/NangateOpenCellLibrary.tech.lef",
    "flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib",
    "reports/",
    "results/",
)

# --- config surface: the only two settable knobs ----------------------------

ALLOWED_KEYS = ("PLACE_DENSITY", "CORE_UTILIZATION")

PLACE_DENSITY_MIN = 0.20
PLACE_DENSITY_MAX = 0.80
PLACE_DENSITY_STOCK = 0.30

CORE_UTILIZATION_MIN = 20.0
CORE_UTILIZATION_MAX = 90.0
CORE_UTILIZATION_STOCK = 55.0

_BOUNDS: dict[str, tuple[float, float, float]] = {
    "PLACE_DENSITY": (PLACE_DENSITY_MIN, PLACE_DENSITY_MAX, PLACE_DENSITY_STOCK),
    "CORE_UTILIZATION": (
        CORE_UTILIZATION_MIN,
        CORE_UTILIZATION_MAX,
        CORE_UTILIZATION_STOCK,
    ),
}

#: Characters/sequences that must never survive into a Make/Tcl override.
#: The surface only accepts JSON numbers, so any string value is rejected;
#: values containing these tokens are reported explicitly as injection.
_INJECTION_TOKENS = (
    ";",
    "$",
    "`",
    "[",
    "]",
    "{",
    "}",
    "\n",
    "\r",
    "#",
    "&",
    "|",
    "!",
    "<",
    ">",
    "\\",
    "'",
    '"',
)


def _reject_injection_text(value: str, *, key: str) -> ContractError:
    hits = sorted({tok for tok in _INJECTION_TOKENS if tok in value})
    detail = f" (suspicious tokens: {hits})" if hits else ""
    return ContractError(
        f"candidate[{key!r}] must be a number, got string {value!r}{detail}; "
        "Tcl/Make injection is not allowed"
    )


def _check_numeric(key: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"candidate[{key!r}] must be a number, got bool")
    if isinstance(value, str):
        raise _reject_injection_text(value, key=key)
    if not isinstance(value, (int, float)):
        raise ContractError(
            f"candidate[{key!r}] must be a number, got {type(value).__name__}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ContractError(f"candidate[{key!r}] must be finite, got {value!r}")
    lo, hi, _ = _BOUNDS[key]
    if not lo <= number <= hi:
        raise ContractError(
            f"candidate[{key!r}]={number!r} out of range [{lo}, {hi}]"
        )
    return number


def validate_candidate_config(candidate: Mapping[str, Any]) -> dict[str, float]:
    """Validate a candidate override mapping; return normalized floats.

    Missing keys imply stock values (validated separately via
    :func:`stock_candidate_config`). Unknown keys, non-numeric values,
    out-of-range values, and Tcl/Make injection strings raise
    :class:`ContractError`.

    :param candidate: mapping with zero or more of ``PLACE_DENSITY`` /
        ``CORE_UTILIZATION``.
    """
    if not isinstance(candidate, Mapping):
        raise ContractError(
            f"candidate config must be an object, got {type(candidate).__name__}"
        )
    unknown = sorted(set(candidate) - set(ALLOWED_KEYS))
    if unknown:
        raise ContractError(
            f"candidate config has unknown keys: {unknown} "
            f"(allowed: {list(ALLOWED_KEYS)}); RTL, SDC, clock, and library "
            "overrides are not permitted"
        )
    normalized: dict[str, float] = {}
    for key in ALLOWED_KEYS:
        if key in candidate:
            normalized[key] = _check_numeric(key, candidate[key])
    return normalized


def stock_candidate_config() -> dict[str, float]:
    """Return the stock (default) candidate overrides."""
    stock = {
        "PLACE_DENSITY": float(PLACE_DENSITY_STOCK),
        "CORE_UTILIZATION": float(CORE_UTILIZATION_STOCK),
    }
    # Self-check: stocks must always validate against the declared bounds.
    validate_candidate_config(stock)
    return stock


def candidate_with_defaults(candidate: Mapping[str, Any]) -> dict[str, float]:
    """Validate ``candidate`` and fill missing knobs with stock values."""
    normalized = validate_candidate_config(candidate)
    full = stock_candidate_config()
    full.update(normalized)
    return full


# --- TaskSpec integration (M0 contracts) -------------------------------------

TASK_DIR = Path(__file__).with_name("tasks") / "gcd"
TASK_MANIFEST_PATH = TASK_DIR / "task.json"

ALLOWED_ACTIONS = ("read_file", "write_file", "run_tool", "submit", "noop")


def make_gcd_task(
    *,
    seed: int = 0,
    max_steps: int = 10,
    max_wallclock_s: float = 3600.0,
    max_tool_calls: int = 10,
) -> TaskSpec:
    """Build the canonical GCD :class:`TaskSpec`.

    Only :data:`CANDIDATE_RELPATH` is editable; RTL, SDC, libraries, and
    grading inputs stay immutable. Grading/flow wiring is a placeholder
    for the M1-03 flow ticket (no metrics claimed here).
    """
    task = TaskSpec(
        schema_version=1,
        task_id=GCD_TASK_ID,
        task_version=GCD_TASK_VERSION,
        source_ref=SOURCE_REF,
        toolchain_refs=dict(TOOLCHAIN_REFS),
        seed=seed,
        allowed_actions=ALLOWED_ACTIONS,
        allowed_edit_paths=(CANDIDATE_RELPATH,),
        read_only=False,
        budgets=Budget(
            max_steps=max_steps,
            max_wallclock_s=float(max_wallclock_s),
            max_tool_calls=max_tool_calls,
        ),
        grader=GraderConfig(
            grader_id=GCD_GRADER_ID,
            grader_version=GCD_GRADER_VERSION,
            timeout_s=min(60.0, float(max_wallclock_s)),
        ),
    )
    task.validate()
    return task


def to_task_spec(
    *,
    seed: int = 0,
    max_steps: int = 10,
    max_wallclock_s: float = 3600.0,
    max_tool_calls: int = 10,
) -> TaskSpec:
    """Alias for :func:`make_gcd_task` (flow-ticket convenience)."""
    return make_gcd_task(
        seed=seed,
        max_steps=max_steps,
        max_wallclock_s=max_wallclock_s,
        max_tool_calls=max_tool_calls,
    )


# --- manifest loading --------------------------------------------------------

def load_manifest_dict(
    path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Load the stock task manifest as strict JSON (dict)."""
    target = Path(path) if path is not None else TASK_MANIFEST_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"cannot read GCD manifest {target}: {exc}") from exc
    return loads_dict_strict(text, what="gcd task manifest")


def validate_manifest(manifest: Mapping[str, Any]) -> TaskSpec:
    """Validate a manifest dict against this module's constants.

    Checks the embedded :class:`TaskSpec`, the config-surface bounds/stocks,
    the fixed-design facts, and the immutable-path policy (protected assets
    must not intersect ``allowed_edit_paths``). Returns the validated spec.
    """
    if not isinstance(manifest, Mapping):
        raise ContractError("gcd task manifest must be an object")
    for section in ("task", "config_surface", "fixed_design", "protected_paths"):
        if section not in manifest:
            raise ContractError(f"gcd task manifest is missing section: {section!r}")

    spec = TaskSpec.from_dict(manifest["task"])
    if spec.task_id != GCD_TASK_ID:
        raise ContractError(
            f"manifest task_id {spec.task_id!r} != {GCD_TASK_ID!r}"
        )
    if spec.task_version != GCD_TASK_VERSION:
        raise ContractError(
            f"manifest task_version {spec.task_version!r} != {GCD_TASK_VERSION!r}"
        )
    if list(spec.allowed_edit_paths) != [CANDIDATE_RELPATH]:
        raise ContractError(
            "manifest allowed_edit_paths must be exactly "
            f"{[CANDIDATE_RELPATH]!r} (got {list(spec.allowed_edit_paths)!r})"
        )

    surface = manifest["config_surface"]
    if not isinstance(surface, Mapping):
        raise ContractError("config_surface must be an object")
    if surface.get("candidate_file") != CANDIDATE_RELPATH:
        raise ContractError(
            f"config_surface.candidate_file must be {CANDIDATE_RELPATH!r}"
        )
    knobs = surface.get("knobs")
    if not isinstance(knobs, Mapping):
        raise ContractError("config_surface.knobs must be an object")
    if sorted(knobs) != sorted(ALLOWED_KEYS):
        raise ContractError(
            f"config_surface.knobs keys must be {sorted(ALLOWED_KEYS)} "
            f"(got {sorted(knobs) if isinstance(knobs, Mapping) else knobs!r})"
        )
    for key in ALLOWED_KEYS:
        entry = knobs[key]
        if not isinstance(entry, Mapping):
            raise ContractError(f"config_surface.knobs[{key!r}] must be an object")
        lo, hi, stock = _BOUNDS[key]
        for field, expected in (("min", lo), ("max", hi), ("stock", stock)):
            actual = entry.get(field)
            if not isinstance(actual, (int, float)) or isinstance(actual, bool):
                raise ContractError(
                    f"config_surface.knobs[{key!r}].{field} must be a number"
                )
            if float(actual) != float(expected):
                raise ContractError(
                    f"config_surface.knobs[{key!r}].{field}={actual!r} "
                    f"!= module constant {expected!r}"
                )
        validate_candidate_config({key: entry["stock"]})
        validate_candidate_config({key: entry["min"]})
        validate_candidate_config({key: entry["max"]})

    fixed = manifest["fixed_design"]
    if not isinstance(fixed, Mapping):
        raise ContractError("fixed_design must be an object")
    for field, expected in (
        ("design", FIXED_DESIGN),
        ("platform", FIXED_PLATFORM),
        ("orfs_commit", ORFS_COMMIT),
        ("image_pinned_ref", IMAGE_PINNED_REF),
        ("design_config", FIXED_DESIGN_CONFIG),
        ("sdc", FIXED_SDC),
        ("endpoint", GCD_ENDPOINT),
    ):
        if fixed.get(field) != expected:
            raise ContractError(
                f"fixed_design.{field} must be {expected!r} "
                f"(got {fixed.get(field)!r})"
            )
    if fixed.get("clock_period_ns") != FIXED_CLOCK_PERIOD_NS:
        raise ContractError(
            f"fixed_design.clock_period_ns must be {FIXED_CLOCK_PERIOD_NS!r}"
        )
    if list(fixed.get("rtl", [])) != list(FIXED_RTL):
        raise ContractError(f"fixed_design.rtl must be {list(FIXED_RTL)!r}")

    protected = manifest["protected_paths"]
    if not isinstance(protected, list) or not protected:
        raise ContractError("protected_paths must be a non-empty list")
    overlap = sorted(set(protected) & set(spec.allowed_edit_paths))
    if overlap:
        raise ContractError(
            f"protected paths must not be editable (overlap: {overlap})"
        )
    for asset in PROTECTED_ASSETS:
        if asset not in protected:
            raise ContractError(
                f"protected_paths is missing required asset {asset!r}"
            )
    return spec


def load_task_spec(
    path: str | os.PathLike[str] | None = None,
) -> TaskSpec:
    """Load the stock manifest and return its validated :class:`TaskSpec`."""
    return validate_manifest(load_manifest_dict(path))


def dumps_candidate_json(candidate: Mapping[str, Any]) -> str:
    """Validate ``candidate`` (with stock defaults) and dump strict JSON."""
    full = candidate_with_defaults(candidate)
    try:
        return json.dumps(full, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ContractError(f"candidate is not strict-JSON serializable: {exc}") from exc


__all__ = [
    "ALLOWED_ACTIONS",
    "ALLOWED_KEYS",
    "CANDIDATE_RELPATH",
    "CORE_UTILIZATION_MAX",
    "CORE_UTILIZATION_MIN",
    "CORE_UTILIZATION_STOCK",
    "FIXED_CLOCK_NAME",
    "FIXED_CLOCK_PERIOD_NS",
    "FIXED_CORNERS",
    "FIXED_DESIGN",
    "FIXED_DESIGN_CONFIG",
    "FIXED_LIB",
    "FIXED_PLATFORM",
    "FIXED_RTL",
    "FIXED_SDC",
    "GCD_ENDPOINT",
    "GCD_GRADER_ID",
    "GCD_GRADER_VERSION",
    "GCD_TASK_ID",
    "GCD_TASK_VERSION",
    "IMAGE_DIGEST",
    "IMAGE_PINNED_REF",
    "ORFS_COMMIT",
    "ORFS_TAG",
    "PLACE_DENSITY_MAX",
    "PLACE_DENSITY_MIN",
    "PLACE_DENSITY_STOCK",
    "PROTECTED_ASSETS",
    "SOURCE_REF",
    "TASK_DIR",
    "TASK_MANIFEST_PATH",
    "TOOLCHAIN_REFS",
    "candidate_with_defaults",
    "dumps_candidate_json",
    "load_manifest_dict",
    "load_task_spec",
    "make_gcd_task",
    "stock_candidate_config",
    "to_task_spec",
    "validate_candidate_config",
    "validate_manifest",
]
