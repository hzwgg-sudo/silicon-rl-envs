"""Pinned GCD flow with fresh, isolated output directories.

The source checkout must match the locked Git revision and have no modified
or extra flow inputs. Each invocation passes WORK_HOME=<scratch>/outputs,
NUM_CORES=1 and the explicit final target to Make. Final GDS/DEF/netlist
and report paths are checked against the pinned upstream source; runtime
verification with the actual EDA image remains outstanding.

The task seed is forwarded to the detailed router through OR_SEED. Other
stochastic stages have no explicit seed control wired by this adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad.preflight import load_toolchain_lock
from silicon_env.environments.openroad.sources import verify_checkout
from silicon_env.types import StepStatus, require_seed

# --- fixed endpoint identity -------------------------------------------------

#: Registered tool name the adapter invokes via the runner protocol.
#: Register it on a real backend as e.g.
#: ``ToolRunner(tools={"openroad-flow": ["make"]},
#: env_allowlist=[*DEFAULT_ENV_ALLOWLIST, *FLOW_ENV_KEYS])``.
FLOW_TOOL_NAME = "openroad-flow"

#: Fixed physical-design endpoint (mirrors ``config.GCD_ENDPOINT``).
FLOW_ENDPOINT = gcd.GCD_ENDPOINT

#: ``flow/`` directory inside the pinned ORFS checkout (lockfile workdir tail).
FLOW_DIRNAME = "flow"

#: Candidate + override records materialized in the fresh scratch dir.
CANDIDATE_FILENAME = gcd.CANDIDATE_RELPATH
OVERRIDES_FILENAME = "gcd_overrides.mk"

#: Env keys the pinned flow environment sets. A real ``ToolRunner`` must
#: include these in its ``env_allowlist`` (in addition to its defaults).
FLOW_ENV_KEYS = ("FLOW_VARIANT", "OMP_NUM_THREADS", "TZ", "PYTHONDONTWRITEBYTECODE")

#: The pinned detail router supports OR_SEED; other stages remain uncontrolled.
SEED_PASSTHROUGH_SUPPORTED = True
SEED_PASSTHROUGH_NOTE = (
    "The pinned detail_route.tcl accepts OR_SEED. The effective router seed is "
    "the requested nonnegative seed modulo 2**31 (signed-int range). "
    "This controls detailed routing only, not all internal stochastic stages."
)

#: Unsupported nondeterminism controls, recorded in every provenance dict.
UNSUPPORTED_NONDETERMINISM_CONTROLS = (
    "seed controls for stages other than detailed routing",
    "tool-internal thread scheduling beyond OMP_NUM_THREADS=1",
    "host kernel / filesystem timestamp granularity below 1ns",
)

# --- stage-aware artifact contract -------------------------------------------

#: Declared stage sequence ending at the fixed endpoint.
STAGE_ORDER = ("synth", "floorplan", "place", "cts", "route", "final")

#: Intermediate stage -> representative output basename (under the
#: ``results/<platform>/<design>/<variant>/`` prefix). Declared contract;
#: on-disk verification is pending on the Linux route.
STAGE_MARKER_BASENAMES: dict[str, str] = {
    "synth": "1_synth.v",
    "floorplan": "2_floorplan.odb",
    "place": "3_place.odb",
    "cts": "4_cts.odb",
    "route": "5_route.odb",
}

#: Required final-stage basenames (GDS + DEF + netlist).
REQUIRED_FINAL_BASENAMES = ("6_final.gds", "6_final.def", "6_final.v")

#: Optional final log basename (runner stdout/stderr logs always exist via
#: the runner ``log_dir``; the ORFS-side stage log is best-effort).
OPTIONAL_FINAL_LOG_BASENAME = "6_report.log"


class FlowError(ValueError):
    """Adapter misuse (bad checkout/scratch/timeout/runner wiring)."""


@dataclass(frozen=True)
class FlowResult:
    """Structured outcome of one fixed-endpoint flow invocation."""

    status: StepStatus
    stage_reached: str
    artifacts: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    @property
    def ok(self) -> bool:
        """True only when the declared ``final`` endpoint completed fresh."""
        return self.status == StepStatus.SUCCESS and self.stage_reached == "final"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "stage_reached": self.stage_reached,
            "artifacts": dict(self.artifacts),
            "provenance": dict(self.provenance),
            "message": self.message,
        }


def _results_prefix(lock_design: Mapping[str, Any], variant: str) -> str:
    return f"results/{lock_design['platform']}/{lock_design['name']}/{variant}"


def _logs_prefix(lock_design: Mapping[str, Any], variant: str) -> str:
    return f"logs/{lock_design['platform']}/{lock_design['name']}/{variant}"


def resolve_flow_plan(lock: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the fixed command + env + artifact paths from the lockfile.

    Returns a dict with ``flow_subdir``, ``make_argv`` (executable first),
    ``pinned_env``, ``variant``, ``required_finals`` (relpaths under the
    flow dir), ``optional_final_log``, and ``stage_markers``
    (stage -> relpath). Pure + local-file only (reads the packaged
    lockfile when ``lock`` is None).
    """
    payload = dict(lock) if lock is not None else load_toolchain_lock()
    execution = payload.get("execution", {})
    if not isinstance(execution, Mapping):
        raise FlowError("lockfile execution section must be an object")
    flow_command = execution.get("flow_command", [])
    if not isinstance(flow_command, (list, tuple)) or not flow_command:
        raise FlowError("lockfile execution.flow_command must be a non-empty list")
    if any(not isinstance(a, str) or not a for a in flow_command):
        raise FlowError("lockfile execution.flow_command must be a list of strings")
    base_env = execution.get("env", {})
    if not isinstance(base_env, Mapping):
        raise FlowError("lockfile execution.env must be an object")
    design = payload.get("design", {})
    if not isinstance(design, Mapping) or not design.get("name"):
        raise FlowError("lockfile design section must carry a name")
    variant = str(base_env.get("FLOW_VARIANT", "base")) or "base"

    pinned_env = {str(k): str(v) for k, v in base_env.items()}
    pinned_env.setdefault("OMP_NUM_THREADS", "1")
    pinned_env.setdefault("TZ", "UTC")
    pinned_env["PYTHONDONTWRITEBYTECODE"] = "1"

    prefix = _results_prefix(design, variant)
    required_finals = tuple(f"{prefix}/{name}" for name in REQUIRED_FINAL_BASENAMES)
    optional_log = f"{_logs_prefix(design, variant)}/{OPTIONAL_FINAL_LOG_BASENAME}"
    stage_markers = {
        stage: f"{prefix}/{STAGE_MARKER_BASENAMES[stage]}" for stage in STAGE_MARKER_BASENAMES
    }
    return {
        "flow_subdir": FLOW_DIRNAME,
        "make_argv": [str(a) for a in flow_command],
        "pinned_env": pinned_env,
        "variant": variant,
        "required_finals": required_finals,
        "optional_final_log": optional_log,
        "stage_markers": stage_markers,
    }


def _require_timeout(timeout_s: Any) -> float:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise FlowError(f"timeout_s must be a number, got {type(timeout_s).__name__}")
    value = float(timeout_s)
    if not (value > 0) or value != value or value in (float("inf"), float("-inf")):
        raise FlowError(f"timeout_s must be a positive finite number, got {timeout_s!r}")
    return value


def _resolve_checkout(orfs_checkout: str | os.PathLike[str]) -> Path:
    raw = os.fspath(orfs_checkout) if isinstance(orfs_checkout, os.PathLike) else orfs_checkout
    if not isinstance(raw, str) or not raw.strip():
        raise FlowError("orfs_checkout must be a non-empty path")
    root = Path(raw).resolve()
    if not root.is_dir():
        raise FlowError(f"orfs_checkout does not exist or is not a dir: {raw!r}")
    if not (root / FLOW_DIRNAME / "Makefile").is_file():
        raise FlowError(
            f"orfs_checkout {root} has no {FLOW_DIRNAME}/Makefile; "
            "pass the pinned ORFS checkout (see required_assets)"
        )
    return root


def _resolve_fresh_scratch(scratch_dir: str | os.PathLike[str], *, checkout: Path) -> Path:
    raw = os.fspath(scratch_dir) if isinstance(scratch_dir, os.PathLike) else scratch_dir
    if not isinstance(raw, str) or not raw.strip():
        raise FlowError("scratch_dir must be a non-empty path")
    scratch = Path(raw).resolve()
    if scratch == checkout:
        raise FlowError("scratch_dir must not be the ORFS checkout itself")
    if checkout in scratch.parents or scratch in checkout.parents:
        raise FlowError(
            "scratch_dir must live outside the pinned checkout "
            "(scratch writes must never mutate the checkout)"
        )
    try:
        scratch.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FlowError(f"cannot create scratch_dir {raw!r}: {exc}") from exc
    if any(scratch.iterdir()):
        raise FlowError(f"scratch_dir must be a fresh empty directory: {scratch}")
    return scratch


def _candidate_sha256(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _render_overrides_mk(full: Mapping[str, float]) -> str:
    lines = [
        "# Generated by silicon_env M1-03 flow adapter (provenance record).",
        "# Authoritative overrides travel on the make command line; this file",
        "# only records the candidate for replay. Only PLACE_DENSITY and",
        "# CORE_UTILIZATION may appear here.",
    ]
    for key in gcd.ALLOWED_KEYS:
        lines.append(f"{key} = {full[key]!r}")
    return "\n".join(lines) + "\n"


def _iso_now(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()


def summarize(result: FlowResult) -> dict[str, Any]:
    """Return a compact report sample for one :class:`FlowResult`."""
    provenance = result.provenance
    return {
        "endpoint": provenance.get("endpoint", FLOW_ENDPOINT),
        "status": result.status.value,
        "stage_reached": result.stage_reached,
        "ok": result.ok,
        "artifact_count": len(result.artifacts),
        "artifacts": sorted(result.artifacts),
        "missing_artifacts": list(provenance.get("missing_artifacts", [])),
        "stale_artifacts": list(provenance.get("stale_artifacts", [])),
        "orfs_commit": provenance.get("orfs_commit"),
        "image_pinned_ref": provenance.get("image_pinned_ref"),
        "seed_requested": provenance.get("seed_requested"),
        "candidate_sha256": provenance.get("candidate_sha256"),
        "duration_s": provenance.get("duration_s"),
    }


def run_gcd_flow(
    candidate: Mapping[str, Any],
    *,
    orfs_checkout: str | os.PathLike[str],
    scratch_dir: str | os.PathLike[str],
    runner: Any,
    seed: int,
    timeout_s: float,
    env_overrides: Mapping[str, str] | None = None,
    tool_versions: Mapping[str, str] | None = None,
    lock: Mapping[str, Any] | None = None,
) -> FlowResult:
    """Run one candidate through the fixed GCD flow to the final endpoint.

    :param candidate: override mapping with zero or more of
        ``PLACE_DENSITY`` / ``CORE_UTILIZATION`` (missing keys imply stock;
        validated via :func:`config.validate_candidate_config`).
    :param orfs_checkout: clean, pinned ORFS checkout; outputs go to scratch.
    :param scratch_dir: fresh empty directory outside the checkout; receives
        ``candidate.json``, ``gcd_overrides.mk``, and the runner logs.
    :param runner: object with a ``run(tool, args, *, cwd, log_dir, env,
        timeout_s)`` method returning a ``RunResult`` (``ToolRunner`` or
        ``ContainerRunner`` protocol).
    :param seed: recorded and forwarded to the detailed router (modulo 2**31).
    :param timeout_s: positive finite deadline forwarded to the runner.
    :param env_overrides: optional extra/override env entries (recorded in
        provenance; pinned keys win unless explicitly overridden here).
    :param tool_versions: optional pre-probed ``{tool: version}`` mapping
        recorded in provenance when available.
    :param lock: optional toolchain lock payload (defaults to packaged).
    """
    full = gcd.candidate_with_defaults(candidate)  # raises ContractError
    seed_value = require_seed(seed)  # raises ContractError
    deadline = _require_timeout(timeout_s)
    if runner is None or not callable(getattr(runner, "run", None)):
        raise FlowError("runner must expose a callable run(tool, args, ...) method")
    if env_overrides is not None and not isinstance(env_overrides, Mapping):
        raise FlowError("env_overrides must be a mapping of str to str or None")
    if tool_versions is not None and not isinstance(tool_versions, Mapping):
        raise FlowError("tool_versions must be a mapping of str to str or None")

    checkout = _resolve_checkout(orfs_checkout)
    source_errors = verify_checkout(checkout, gcd.ORFS_COMMIT)
    if source_errors:
        raise FlowError("; ".join(source_errors))
    scratch = _resolve_fresh_scratch(scratch_dir, checkout=checkout)
    plan = resolve_flow_plan(lock)
    flow_dir = checkout / plan["flow_subdir"]

    canonical = gcd.dumps_candidate_json(full)
    candidate_hash = _candidate_sha256(canonical)
    (scratch / CANDIDATE_FILENAME).write_text(canonical + "\n", encoding="utf-8")
    (scratch / OVERRIDES_FILENAME).write_text(_render_overrides_mk(full), encoding="utf-8")

    make_exe, *base_args = plan["make_argv"]
    knob_args = [f"{key}={full[key]!r}" for key in gcd.ALLOWED_KEYS]
    output_dir = scratch / "outputs"
    output_dir.mkdir()
    evidence_script = Path(__file__).parent / "scripts" / "final_evidence.tcl"
    argv = [*base_args, *knob_args, f"WORK_HOME={output_dir}", "NUM_CORES=1",
            f"POST_FINAL_REPORT_TCL={evidence_script}", f"OR_SEED={seed_value % (2**31)}",
            "-j1", "final"]
    env: dict[str, str] = dict(plan["pinned_env"])
    if env_overrides:
        for key, value in env_overrides.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise FlowError("env_overrides must map str names to str values")
            if key in plan["pinned_env"] and value != plan["pinned_env"][key]:
                raise FlowError(f"cannot override pinned environment key {key}")
            env[key] = value

    start_epoch = time.time()
    start_ns = time.time_ns()
    log_dir = scratch / "logs"
    run_result: Any = None
    run_error = ""
    try:
        run_result = runner.run(
            FLOW_TOOL_NAME,
            argv,
            cwd=str(flow_dir),
            log_dir=str(log_dir),
            env=dict(env),
            timeout_s=deadline,
        )
    except Exception as exc:  # runner misuse / launch crash: never success
        run_error = f"{type(exc).__name__}: {exc}"
    end_epoch = time.time()

    required_finals: Sequence[str] = plan["required_finals"]
    stage_markers: Mapping[str, str] = plan["stage_markers"]
    optional_log: str = plan["optional_final_log"]

    def _fresh(relpath: str) -> tuple[bool, bool]:
        """Return (exists, fresh) for a flow-relative artifact path."""
        target = output_dir / relpath
        try:
            if not target.is_file() or target.is_symlink():
                return (False, False)
            mtime_ns = target.stat().st_mtime_ns
        except OSError:
            return (False, False)
        return (True, mtime_ns >= start_ns)

    artifacts: dict[str, str] = {}
    missing: list[str] = []
    stale: list[str] = []
    for relpath in (*required_finals, optional_log):
        exists, fresh = _fresh(relpath)
        if fresh:
            artifacts[relpath] = str(output_dir / relpath)
        elif exists:
            stale.append(relpath)
        elif relpath in required_finals:
            missing.append(relpath)

    stage_reached = "not-started"
    for stage in STAGE_ORDER:
        if stage == "final":
            continue
        exists, fresh = _fresh(stage_markers[stage])
        if fresh:
            stage_reached = stage
    finals_fresh = all(r in artifacts for r in required_finals)
    if finals_fresh:
        stage_reached = "final"

    provenance: dict[str, Any] = {
        "endpoint": FLOW_ENDPOINT,
        "orfs_commit": gcd.ORFS_COMMIT,
        "image_pinned_ref": gcd.IMAGE_PINNED_REF,
        "seed_requested": seed_value,
        "router_seed": seed_value % (2**31),
        "seed_passthrough_supported": SEED_PASSTHROUGH_SUPPORTED,
        "seed_passthrough_note": SEED_PASSTHROUGH_NOTE,
        "unsupported_nondeterminism_controls": list(UNSUPPORTED_NONDETERMINISM_CONTROLS),
        "candidate": dict(full),
        "candidate_sha256": candidate_hash,
        "candidate_path": str(scratch / CANDIDATE_FILENAME),
        "overrides_path": str(scratch / OVERRIDES_FILENAME),
        "flow_workdir": str(flow_dir),
        "output_dir": str(output_dir),
        "start_ns": start_ns,
        "scratch_dir": str(scratch),
        "tool_name": FLOW_TOOL_NAME,
        "make_executable": make_exe,
        "command_argv": [make_exe, *argv],
        "env": dict(env),
        "variant": plan["variant"],
        "timeout_s": deadline,
        "start_epoch_s": start_epoch,
        "start_iso": _iso_now(start_epoch),
        "end_epoch_s": end_epoch,
        "end_iso": _iso_now(end_epoch),
        "duration_s": max(0.0, end_epoch - start_epoch),
        "missing_artifacts": missing,
        "stale_artifacts": stale,
        "required_finals": list(required_finals),
        "optional_final_log": optional_log,
        "layout_verification": (
            "source-verified, pending-linux-run: stage/final relpaths match ORFS 26Q2 "
            "GCD contract; on-disk verification awaits a real pinned-image run"
        ),
    }
    if callable(getattr(runner, "provenance", None)):
        provenance["runner_profile"] = runner.provenance()
    if tool_versions is not None:
        provenance["tool_versions"] = {str(k): str(v) for k, v in tool_versions.items()}
    else:
        provenance["tool_versions"] = None
        provenance["tool_versions_note"] = (
            "tool versions TBD until a real pinned-image run probes them"
        )

    def _finish(status: StepStatus, message: str, *, run: Any = run_result) -> FlowResult:
        if run is not None:
            provenance["runner_status"] = getattr(
                getattr(run, "status", None), "value", str(getattr(run, "status", ""))
            )
            provenance["runner_exit_code"] = getattr(run, "exit_code", None)
            provenance["runner_duration_s"] = getattr(run, "duration_s", None)
            for key in ("stdout_path", "stderr_path", "log_dir"):
                value = getattr(run, key, "")
                if value:
                    provenance[f"runner_{key}"] = str(value)
            if getattr(run, "error", ""):
                provenance["runner_error"] = str(run.error)
        else:
            provenance["runner_error"] = run_error
        return FlowResult(
            status=status,
            stage_reached=stage_reached,
            artifacts=dict(artifacts),
            provenance=provenance,
            message=message,
        )

    if run_result is None:
        return _finish(StepStatus.INFRA_ERROR, f"flow runner raised before launch: {run_error}")
    status = getattr(run_result, "status", None)
    if status == StepStatus.TIMEOUT or getattr(run_result, "timed_out", False):
        return _finish(StepStatus.TIMEOUT, "flow exceeded its deadline")
    if status == StepStatus.INFRA_ERROR or not getattr(run_result, "launched", True):
        return _finish(StepStatus.INFRA_ERROR, "flow infrastructure failure")
    if status != StepStatus.SUCCESS:
        return _finish(
            StepStatus.TOOL_FAILURE,
            f"flow tool failed (exit={getattr(run_result, 'exit_code', None)}) "
            f"at stage {stage_reached!r}; endpoint {FLOW_ENDPOINT!r} not reached",
        )
    if not finals_fresh:
        problems = [*missing, *[f"{p} (stale)" for p in stale]]
        detail = "; ".join(problems) if problems else "unknown artifact loss"
        return _finish(
            StepStatus.TOOL_FAILURE,
            f"flow exited 0 but final endpoint {FLOW_ENDPOINT!r} is incomplete: "
            f"{detail}; never labeled success",
        )
    compact = json.dumps(
        {"endpoint": FLOW_ENDPOINT, "candidate_sha256": candidate_hash[:12]},
        sort_keys=True,
    )
    return _finish(StepStatus.SUCCESS, f"gcd flow completed endpoint 'final' {compact}")


__all__ = [
    "CANDIDATE_FILENAME",
    "FLOW_DIRNAME",
    "FLOW_ENDPOINT",
    "FLOW_ENV_KEYS",
    "FLOW_TOOL_NAME",
    "OPTIONAL_FINAL_LOG_BASENAME",
    "OVERRIDES_FILENAME",
    "REQUIRED_FINAL_BASENAMES",
    "SEED_PASSTHROUGH_NOTE",
    "SEED_PASSTHROUGH_SUPPORTED",
    "STAGE_MARKER_BASENAMES",
    "STAGE_ORDER",
    "UNSUPPORTED_NONDETERMINISM_CONTROLS",
    "FlowError",
    "FlowResult",
    "resolve_flow_plan",
    "run_gcd_flow",
    "summarize",
]
