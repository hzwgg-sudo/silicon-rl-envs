"""First Ibex area-under-timing task with a constrained edit surface (M2-01).

The task optimizes physical design only: RTL, SDC, libraries, and grading
inputs are immutable. The agent may override exactly two numeric knobs --
``PLACE_DENSITY`` and ``CORE_UTILIZATION`` -- through the single editable
file :data:`CANDIDATE_RELPATH`. Everything else (notably the fixed 2.20 ns
clock and the Ibex RTL sources) is outside the allowed interface, so a
candidate cannot relax timing constraints or alter function through it.

This module mirrors ``config.py`` (GCD, M1-02) on purpose: the scope of
#20 forbids a generic plugin framework and forbids ``if design == ...``
branches in the generic core (``flow.py`` / ``evaluator.py`` /
``grader.py``), so the second task gets a narrow task-specific config with
the same action space. Validation semantics are identical to GCD's; only
the fixed-design facts and the ``CORE_UTILIZATION`` stock differ.

Stock values are read off the pinned ORFS revision (see
``toolchain.lock.json`` -- the same pin GCD qualified, commit
``036d106273e66855cd5214d49518fd0f0df7de61``, image
``docker.io/openroad/orfs:26Q2@sha256:7832...47b61``):

- ``PLACE_DENSITY`` stock ``0.30``: platform default from
  ``flow/platforms/nangate45/config.mk`` (``export PLACE_DENSITY ?= 0.30``);
  the Ibex ``config.mk`` does not override it (it only sets
  ``PLACE_DENSITY_LB_ADDON = 0.20``).
- ``CORE_UTILIZATION`` stock ``50``: Ibex design default from
  ``flow/designs/nangate45/ibex/config.mk``
  (``export CORE_UTILIZATION ?= 50``) -- documented adjustment from GCD's
  ``55``; demanded by the upstream Ibex defaults.

Bounds reuse GCD's task-supported safe ranges inside the ORFS semantic
ranges (density 0-1, utilization 0-100 percent). Flow execution, metrics,
and the grader land in follow-on tickets (M2-02+); this module defines no
flow, metrics, or grading logic.

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

IBEX_TASK_ID = "ibex-nangate45"
IBEX_TASK_VERSION = "0.1.0"
IBEX_GRADER_ID = "ibex-area-grader"
IBEX_GRADER_VERSION = "0.1.0"

#: The single editable file. The only entry of ``allowed_edit_paths``.
#: A separate scratch filename from GCD's so the two tasks never share
#: candidate state or baseline fingerprints.
CANDIDATE_RELPATH = "candidate_ibex.json"

#: Fixed physical-design endpoint for the later flow ticket (M2-02+).
#: The default ``make`` target builds the full flow ending at final
#: post-detailed-route reports.
IBEX_ENDPOINT = "final"

# --- pinned sources (same ORFS profile pin as GCD, M1-01) -------------------

ORFS_COMMIT = "036d106273e66855cd5214d49518fd0f0df7de61"
ORFS_TAG = "26Q2"
IMAGE_PINNED_REF = (
    "docker.io/openroad/orfs:26Q2@sha256:"
    "7832ae885e62933fcbfc486fbd9133f8c3bd1206d15c96e93bfad97432947b61"
)
IMAGE_DIGEST = (
    "sha256:7832ae885e62933fcbfc486fbd9133f8c3bd1206d15c96e93bfad97432947b61"
)

SOURCE_REF = f"orfs:{ORFS_COMMIT}:flow/designs/nangate45/ibex"
TOOLCHAIN_REFS = {
    "orfs": ORFS_COMMIT,
    "orfs-image": IMAGE_PINNED_REF,
}

# --- fixed design facts (immutable; not settable via the candidate) --------

FIXED_DESIGN = "ibex"
FIXED_DESIGN_NAME = "ibex_core"
FIXED_PLATFORM = "nangate45"
FIXED_CLOCK_PERIOD_NS = 2.20
FIXED_CLOCK_NAME = "core_clock"
FIXED_CLOCK_PORT = "clk_i"
FIXED_SDC = "tasks/ibex/constraint.sdc"
FIXED_SDC_PATH = Path(__file__).parent / FIXED_SDC
FIXED_DESIGN_CONFIG = "flow/designs/nangate45/ibex/config.mk"
FIXED_UPSTREAM_SDC = "flow/designs/nangate45/ibex/constraint.sdc"
#: ``VERILOG_FILES`` in the Ibex ``config.mk`` is
#: ``sort(wildcard $(DESIGN_HOME)/src/ibex_sv/*.sv)`` plus the synthesis
#: shim below; the vendor lowRISC prim directory is a Verilog *include*
#: dir, not a synthesised source. ``SYNTH_HDL_FRONTEND = slang``.
FIXED_RTL = (
    "flow/designs/src/ibex_sv/ibex_alu.sv",
    "flow/designs/src/ibex_sv/ibex_compressed_decoder.sv",
    "flow/designs/src/ibex_sv/ibex_controller.sv",
    "flow/designs/src/ibex_sv/ibex_core.sv",
    "flow/designs/src/ibex_sv/ibex_counter.sv",
    "flow/designs/src/ibex_sv/ibex_cs_registers.sv",
    "flow/designs/src/ibex_sv/ibex_csr.sv",
    "flow/designs/src/ibex_sv/ibex_decoder.sv",
    "flow/designs/src/ibex_sv/ibex_ex_block.sv",
    "flow/designs/src/ibex_sv/ibex_fetch_fifo.sv",
    "flow/designs/src/ibex_sv/ibex_id_stage.sv",
    "flow/designs/src/ibex_sv/ibex_if_stage.sv",
    "flow/designs/src/ibex_sv/ibex_load_store_unit.sv",
    "flow/designs/src/ibex_sv/ibex_multdiv_fast.sv",
    "flow/designs/src/ibex_sv/ibex_multdiv_slow.sv",
    "flow/designs/src/ibex_sv/ibex_pkg.sv",
    "flow/designs/src/ibex_sv/ibex_pmp.sv",
    "flow/designs/src/ibex_sv/ibex_prefetch_buffer.sv",
    "flow/designs/src/ibex_sv/ibex_register_file_ff.sv",
    "flow/designs/src/ibex_sv/ibex_wb_stage.sv",
    "flow/designs/src/ibex_sv/syn/rtl/prim_clock_gating.v",
)
FIXED_VERILOG_INCLUDE_DIRS = (
    "flow/designs/src/ibex_sv/vendor/lowrisc_ip/prim/rtl/",
)
FIXED_SYNTH_HDL_FRONTEND = "slang"
FIXED_CORNERS = "typical (NangateOpenCellLibrary_typical.lib)"
FIXED_LIB = "flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib"

#: Protected (read-only) assets, recorded as groundwork for later tickets.
#: None of these may appear in ``allowed_edit_paths``. Ibex-specific paths
#: are disjoint from GCD's task paths; shared platform files appear in
#: both tasks' manifests by path identity (same pin, same bytes).
PROTECTED_ASSETS = (
    FIXED_SDC,
    FIXED_UPSTREAM_SDC,
    FIXED_DESIGN_CONFIG,
    *FIXED_RTL,
    "flow/platforms/nangate45/config.mk",
    "flow/platforms/nangate45/lef/NangateOpenCellLibrary.tech.lef",
    "flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib",
    "reports/",
    "results/",
)

#: Upstream assets the preflight gate must find in a pinned checkout before
#: any Ibex flow may start. Kept here (not in the shared ``toolchain.lock``,
#: whose ``required_assets``/``design`` sections stay GCD-scoped so GCD lock
#: verification is untouched) and mirrored in ``tasks/ibex/task.json``.
REQUIRED_ASSETS = (
    "flow/Makefile",
    "flow/designs/nangate45/ibex/config.mk",
    "flow/designs/nangate45/ibex/constraint.sdc",
    *FIXED_RTL,
    "flow/platforms/nangate45/config.mk",
    "flow/platforms/nangate45/lef/NangateOpenCellLibrary.tech.lef",
    "flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib",
)

# --- config surface: the only two settable knobs (same as GCD) --------------

ALLOWED_KEYS = ("PLACE_DENSITY", "CORE_UTILIZATION")

PLACE_DENSITY_MIN = 0.20
PLACE_DENSITY_MAX = 0.80
PLACE_DENSITY_STOCK = 0.30

CORE_UTILIZATION_MIN = 20.0
CORE_UTILIZATION_MAX = 90.0
CORE_UTILIZATION_STOCK = 50.0

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

TASK_DIR = Path(__file__).with_name("tasks") / "ibex"
TASK_MANIFEST_PATH = TASK_DIR / "task.json"

ALLOWED_ACTIONS = ("read_file", "write_file", "run_tool", "submit", "noop")


def make_ibex_task(
    *,
    seed: int = 0,
    max_steps: int = 10,
    max_wallclock_s: float = 7200.0,
    max_tool_calls: int = 10,
) -> TaskSpec:
    """Build the canonical Ibex :class:`TaskSpec`.

    Only :data:`CANDIDATE_RELPATH` is editable; RTL, SDC, libraries, and
    grading inputs stay immutable. The default wall-clock budget is wider
    than GCD's 3600 s: Ibex is a full RISC-V core (~20x the GCD cell
    count class) and the stock flow has not been timed yet at this pin
    (measurement lands in M2-02); 7200 s is an estimate, not a measured
    bound. The grader deadline matches the task wall-clock budget.
    """
    task = TaskSpec(
        schema_version=1,
        task_id=IBEX_TASK_ID,
        task_version=IBEX_TASK_VERSION,
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
            grader_id=IBEX_GRADER_ID,
            grader_version=IBEX_GRADER_VERSION,
            timeout_s=min(7200.0, float(max_wallclock_s)),
        ),
    )
    task.validate()
    return task


def to_task_spec(
    *,
    seed: int = 0,
    max_steps: int = 10,
    max_wallclock_s: float = 7200.0,
    max_tool_calls: int = 10,
) -> TaskSpec:
    """Alias for :func:`make_ibex_task` (flow-ticket convenience)."""
    return make_ibex_task(
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
        raise ContractError(f"cannot read Ibex manifest {target}: {exc}") from exc
    return loads_dict_strict(text, what="ibex task manifest")


def validate_manifest(manifest: Mapping[str, Any]) -> TaskSpec:
    """Validate a manifest dict against this module's constants.

    Checks the embedded :class:`TaskSpec`, the config-surface bounds/stocks,
    the fixed-design facts, and the immutable-path policy (protected assets
    must not intersect ``allowed_edit_paths``). Returns the validated spec.
    """
    if not isinstance(manifest, Mapping):
        raise ContractError("ibex task manifest must be an object")
    for section in ("task", "config_surface", "fixed_design", "protected_paths"):
        if section not in manifest:
            raise ContractError(
                f"ibex task manifest is missing section: {section!r}"
            )

    spec = TaskSpec.from_dict(manifest["task"])
    if spec.task_id != IBEX_TASK_ID:
        raise ContractError(
            f"manifest task_id {spec.task_id!r} != {IBEX_TASK_ID!r}"
        )
    if spec.task_version != IBEX_TASK_VERSION:
        raise ContractError(
            f"manifest task_version {spec.task_version!r} != {IBEX_TASK_VERSION!r}"
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
        ("design_name", FIXED_DESIGN_NAME),
        ("platform", FIXED_PLATFORM),
        ("orfs_commit", ORFS_COMMIT),
        ("image_pinned_ref", IMAGE_PINNED_REF),
        ("design_config", FIXED_DESIGN_CONFIG),
        ("upstream_sdc", FIXED_UPSTREAM_SDC),
        ("sdc", FIXED_SDC),
        ("endpoint", IBEX_ENDPOINT),
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
    if fixed.get("clock_name") != FIXED_CLOCK_NAME:
        raise ContractError(
            f"fixed_design.clock_name must be {FIXED_CLOCK_NAME!r}"
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
    "FIXED_CLOCK_PORT",
    "FIXED_CORNERS",
    "FIXED_DESIGN",
    "FIXED_DESIGN_CONFIG",
    "FIXED_DESIGN_NAME",
    "FIXED_LIB",
    "FIXED_PLATFORM",
    "FIXED_RTL",
    "FIXED_SDC",
    "FIXED_SYNTH_HDL_FRONTEND",
    "FIXED_UPSTREAM_SDC",
    "FIXED_VERILOG_INCLUDE_DIRS",
    "IBEX_ENDPOINT",
    "IBEX_GRADER_ID",
    "IBEX_GRADER_VERSION",
    "IBEX_TASK_ID",
    "IBEX_TASK_VERSION",
    "IMAGE_DIGEST",
    "IMAGE_PINNED_REF",
    "ORFS_COMMIT",
    "ORFS_TAG",
    "PLACE_DENSITY_MAX",
    "PLACE_DENSITY_MIN",
    "PLACE_DENSITY_STOCK",
    "PROTECTED_ASSETS",
    "REQUIRED_ASSETS",
    "SOURCE_REF",
    "TASK_DIR",
    "TASK_MANIFEST_PATH",
    "TOOLCHAIN_REFS",
    "candidate_with_defaults",
    "dumps_candidate_json",
    "load_manifest_dict",
    "load_task_spec",
    "make_ibex_task",
    "stock_candidate_config",
    "to_task_spec",
    "validate_candidate_config",
    "validate_manifest",
]
