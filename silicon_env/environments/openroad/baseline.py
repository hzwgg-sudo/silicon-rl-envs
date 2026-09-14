"""Repeatable GCD reference baseline (M1-05).

Captures one checked reference record for the pinned stock GCD task:
input/tool fingerprints, final-stage metrics (area, WNS/TNS, routed
completion, DRC/unconstrained indicators), validity evidence, declared
comparison tolerances, three-run provenance, and measured resources.

The reward denominator (follow-on ticket) must come from a reproducible
successful run; this module defines how that record is built, stored,
and verified. Runtime/wallclock values and timestamps are provenance
only: they never participate in deterministic equality (fingerprint
comparison covers pins + candidate + protected assets, never timing).

Stdlib-only, Python >= 3.10. No EDA tools, Docker, network, or API
keys. Real three-run capture happens on the Linux route via
``scripts/generate_openroad_baseline.py``; default unit tests inject
fakes. Until real runs exist the checked ``baseline.json`` is an
honest ``TBD-unverified`` record that :func:`validate_baseline`
rejects for scoring use (fail closed, never fabricated metrics).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from silicon_env.environments.openroad import config as gcd

#: Schema version accepted by :func:`validate_baseline`.
BASELINE_SCHEMA_VERSION = 1

#: Status of a fully measured three-run record usable for scoring.
BASELINE_STATUS_VERIFIED = "verified"

#: Status of the placeholder record: present but NOT usable for scoring.
BASELINE_STATUS_TBD = "TBD-unverified"

#: Initial comparison tolerances. PROVISIONAL: tight values proposed
#: before measurement; they must be empirically justified (not loosened
#: to pass) once the real three-run capture lands.
DEFAULT_TOLERANCES: dict[str, float] = {
    "area_rel": 0.01,
    "wns_abs_ns": 0.005,
    "tns_abs_ns": 0.01,
}

TOLERANCES_PROVISIONAL_NOTE = (
    "PROVISIONAL pending real three-run measurement on the Linux route "
    "(see scripts/generate_openroad_baseline.py). Tight initial values; "
    "justify empirically from observed run-to-run spread, never loosen "
    "to make a drifted run pass."
)

#: Number of repeat runs a verified baseline requires.
REQUIRED_RUN_COUNT = 3

#: Metric keys compared under tolerances (deterministic equality set;
#: runtime/timestamps are explicitly excluded).
METRIC_KEYS = ("area_um2", "wns_ns", "tns_ns")


class BaselineError(ValueError):
    """Baseline misuse or failed verification (invalid record, drift)."""


def compute_candidate_hash(candidate: Mapping[str, Any]) -> str:
    """Hash the stock-normalized candidate JSON (sha256 hex)."""
    full = gcd.candidate_with_defaults(candidate)
    canonical = gcd.dumps_candidate_json(full)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fixed_design_facts() -> dict[str, Any]:
    """Return the fixed design facts covered by the protected hash."""
    return {
        "design": gcd.FIXED_DESIGN,
        "platform": gcd.FIXED_PLATFORM,
        "clock_period_ns": gcd.FIXED_CLOCK_PERIOD_NS,
        "clock_name": gcd.FIXED_CLOCK_NAME,
        "sdc": gcd.FIXED_SDC,
        "design_config": gcd.FIXED_DESIGN_CONFIG,
        "rtl": list(gcd.FIXED_RTL),
        "corners": gcd.FIXED_CORNERS,
        "lib": gcd.FIXED_LIB,
        "endpoint": gcd.GCD_ENDPOINT,
    }


def compute_protected_hash(
    protected_assets: Any | None = None,
    fixed_facts: Mapping[str, Any] | None = None,
) -> str:
    """Hash the protected-asset list plus fixed design facts.

    Defaults come from :mod:`config`; pass explicit values only to
    simulate a source/config change (tests use this to prove
    invalidation).
    """
    assets = list(gcd.PROTECTED_ASSETS) if protected_assets is None else list(protected_assets)
    facts = fixed_design_facts() if fixed_facts is None else dict(fixed_facts)
    canonical = json.dumps(
        {"protected_assets": sorted(assets), "fixed_design": facts},
        sort_keys=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint_inputs(
    candidate: Mapping[str, Any] | None = None,
    *,
    orfs_commit: str | None = None,
    image_pinned_ref: str | None = None,
    protected_assets: Any | None = None,
    fixed_facts: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build the deterministic input/tool fingerprint (no runtimes).

    Covers the pinned ORFS commit, the digest-pinned image ref, the
    normalized stock candidate hash, and the protected-asset hash.
    Wallclock/timestamps are never included.
    """
    full = gcd.candidate_with_defaults(dict(candidate) if candidate is not None else {})
    return {
        "orfs_commit": orfs_commit or gcd.ORFS_COMMIT,
        "image_pinned_ref": image_pinned_ref or gcd.IMAGE_PINNED_REF,
        "candidate_hash": compute_candidate_hash(full),
        "protected_hash": compute_protected_hash(protected_assets, fixed_facts),
    }


def _as_floats(metrics: Any) -> dict[str, float | None]:
    if hasattr(metrics, "area_um2"):
        return {
            "area_um2": metrics.area_um2,
            "wns_ns": metrics.wns_ns,
            "tns_ns": metrics.tns_ns,
        }
    if isinstance(metrics, Mapping):
        return {
            "area_um2": metrics.get("area_um2"),
            "wns_ns": metrics.get("wns_ns"),
            "tns_ns": metrics.get("tns_ns"),
        }
    raise BaselineError(f"metrics must be a GcdMetrics or mapping, got {type(metrics).__name__}")


def _check_tolerances(tolerances: Mapping[str, Any]) -> dict[str, float]:
    try:
        area_rel = float(tolerances["area_rel"])
        wns_abs = float(tolerances["wns_abs_ns"])
        tns_abs = float(tolerances["tns_abs_ns"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BaselineError(
            f"invalid tolerances (need area_rel/wns_abs_ns/tns_abs_ns): {exc}"
        ) from exc
    for name, value in (
        ("area_rel", area_rel),
        ("wns_abs_ns", wns_abs),
        ("tns_abs_ns", tns_abs),
    ):
        if not math.isfinite(value) or value < 0 or (name == "area_rel" and value == 0):
            raise BaselineError(f"invalid tolerance {name}={value!r}: must be finite and > 0")
    return {"area_rel": area_rel, "wns_abs_ns": wns_abs, "tns_abs_ns": tns_abs}


def within_tolerances(
    a: Any,
    b: Any,
    tolerances: Mapping[str, Any],
) -> bool:
    """Return True when two metric sets match within tolerances.

    Boundary semantics (inclusive): a difference exactly equal to the
    tolerance passes (``<=``); anything strictly greater fails.
    Area uses a relative bound scaled by the baseline magnitude
    (``|a-b| <= area_rel * |b|``); WNS/TNS use absolute ns bounds.
    Non-finite or non-positive-baseline-area inputs never match.
    The comparison is inclusive in real arithmetic; binary
    floating-point rounding may move an exactly-on-the-boundary decimal
    value by 1 ulp (fail-closed direction is not guaranteed there, so
    keep tolerances away from the observed spread).
    """
    tols = _check_tolerances(tolerances)
    fa = _as_floats(a)
    fb = _as_floats(b)
    area_a, area_b = fa["area_um2"], fb["area_um2"]
    if not isinstance(area_a, (int, float)) or not isinstance(area_b, (int, float)):
        return False
    if not math.isfinite(area_a) or not math.isfinite(area_b):
        return False
    if not area_b > 0 or not area_a > 0:
        return False
    if abs(area_a - area_b) > tols["area_rel"] * abs(area_b):
        return False
    for key, tol_key in (("wns_ns", "wns_abs_ns"), ("tns_ns", "tns_abs_ns")):
        va, vb = fa[key], fb[key]
        if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
            return False
        if not math.isfinite(va) or not math.isfinite(vb):
            return False
        if abs(va - vb) > tols[tol_key]:
            return False
    return True


def metrics_to_record(metrics: Any) -> dict[str, Any]:
    """Normalize a GcdMetrics (or mapping) to the stored metric dict."""
    vals = _as_floats(metrics)
    if isinstance(metrics, Mapping):
        routed_ok = metrics.get("routed_ok")
        drc_count = metrics.get("drc_count")
        unconstrained = metrics.get("unconstrained_paths")
        valid = metrics.get("valid")
    else:
        routed_ok = getattr(metrics, "routed_ok", None)
        drc_count = getattr(metrics, "drc_count", None)
        unconstrained = getattr(metrics, "unconstrained_paths", None)
        valid = getattr(metrics, "valid", None)
    return {
        "area_um2": vals["area_um2"],
        "wns_ns": vals["wns_ns"],
        "tns_ns": vals["tns_ns"],
        "routed_ok": bool(routed_ok),
        "drc_count": drc_count,
        "unconstrained_paths": unconstrained,
        "valid": bool(valid),
    }


def _require_constraints(entry: Mapping[str, Any]) -> None:
    for key in METRIC_KEYS:
        value = entry.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise BaselineError(f"non-finite or invalid baseline {key}")
    if entry["area_um2"] <= 0 or entry["wns_ns"] < 0 or entry["tns_ns"] != 0:
        raise BaselineError(f"baseline fails fixed area/timing constraints: {dict(entry)}")
    for key in ("drc_count", "unconstrained_paths"):
        if type(entry.get(key)) is not int or entry[key] != 0:
            raise BaselineError(f"baseline requires measured clean {key}: {entry.get(key)!r}")
    if entry.get("valid") is not True or entry.get("routed_ok") is not True:
        raise BaselineError("baseline requires valid routed completion")


def build_baseline_record(
    metrics_list: Any,
    *,
    tolerances: Mapping[str, Any] | None = None,
    run_ids: Any | None = None,
    seeds: Any | None = None,
    tool_versions: Mapping[str, Any] | None = None,
    resources: Mapping[str, Any] | None = None,
    created_by: str = "scripts/generate_openroad_baseline.py",
) -> dict[str, Any]:
    """Build a verified baseline record from exactly three run metrics.

    Raises :class:`BaselineError` on any validity failure
    (``valid`` false, non-finite metrics, non-positive area, missing
    routed completion) or on unexplained metric drift (any run outside
    tolerances of the first run). The stored ``metrics`` aggregate is
    the mean of the three runs; per-run values are kept under ``runs``.
    Never fabricates values: only the supplied measured metrics are
    stored.
    """
    tols = _check_tolerances(
        dict(tolerances) if tolerances is not None else dict(DEFAULT_TOLERANCES)
    )
    runs = list(metrics_list) if metrics_list is not None else []
    if len(runs) != REQUIRED_RUN_COUNT:
        raise BaselineError(
            f"baseline needs exactly {REQUIRED_RUN_COUNT} runs, got {len(runs)}"
        )
    normalized = [metrics_to_record(m) for m in runs]
    for index, entry in enumerate(normalized):
        problems: list[str] = []
        if not entry["valid"]:
            problems.append("invalid-metrics")
        for key in METRIC_KEYS:
            value = entry[key]
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                problems.append(f"non-finite-{key}")
        if isinstance(entry["area_um2"], (int, float)) and not entry["area_um2"] > 0:
            problems.append("area-nonpositive")
        if not entry["routed_ok"]:
            problems.append("missing-completion")
        if problems:
            raise BaselineError(f"run {index} fails validity ({'; '.join(problems)}): fail closed")
    for entry in normalized:
        _require_constraints(entry)
    reference = normalized[0]
    for index in (1, 2):
        if not within_tolerances(normalized[index], reference, tols):
            raise BaselineError(
                f"run {index} drifts outside tolerances vs run 0: fail closed "
                f"(area {normalized[index]['area_um2']!r} vs {reference['area_um2']!r}, "
                f"wns {normalized[index]['wns_ns']!r} vs {reference['wns_ns']!r}, "
                f"tns {normalized[index]['tns_ns']!r} vs {reference['tns_ns']!r})"
            )
    if run_ids is not None and len(list(run_ids)) != REQUIRED_RUN_COUNT:
        raise BaselineError("run_ids must cover exactly 3 runs")
    if seeds is not None and len(list(seeds)) != REQUIRED_RUN_COUNT:
        raise BaselineError("seeds must cover exactly 3 runs")
    resolved_ids = list(run_ids) if run_ids is not None else [f"run-{i}" for i in range(3)]
    resolved_seeds = list(seeds) if seeds is not None else [0, 1, 2]
    stock = gcd.stock_candidate_config()
    fingerprint = fingerprint_inputs(stock)
    aggregate = {
        key: sum(entry[key] for entry in normalized) / REQUIRED_RUN_COUNT for key in METRIC_KEYS
    }
    record = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "status": BASELINE_STATUS_VERIFIED,
        "task_id": gcd.GCD_TASK_ID,
        "task_version": gcd.GCD_TASK_VERSION,
        "orfs_commit": fingerprint["orfs_commit"],
        "image_pinned_ref": fingerprint["image_pinned_ref"],
        "candidate": stock,
        "candidate_hash": fingerprint["candidate_hash"],
        "protected_hash": fingerprint["protected_hash"],
        "metrics": {
            **aggregate,
            "routed_ok": True,
            "drc_count": normalized[0]["drc_count"],
            "unconstrained_paths": normalized[0]["unconstrained_paths"],
            "valid": True,
        },
        "tolerances": dict(tols),
        "tolerances_note": TOLERANCES_PROVISIONAL_NOTE,
        "provenance": {
            "run_ids": resolved_ids,
            "seeds": resolved_seeds,
            "tool_versions": dict(tool_versions) if tool_versions is not None else None,
            "generator": created_by,
        },
        "runs": [
            {"run_id": rid, "seed": seed, "metrics": entry}
            for rid, seed, entry in zip(resolved_ids, resolved_seeds, normalized)
        ],
        "resources": dict(resources) if resources is not None else {
            "wallclock_s": None,
            "peak_rss_gb": None,
            "status": BASELINE_STATUS_TBD,
            "notes": "measured on the Linux route during generation",
        },
    }
    return validate_baseline(record)


def load_baseline(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Parse a baseline JSON file; raise :class:`BaselineError` if unreadable."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise BaselineError(f"cannot read baseline {target}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BaselineError(f"baseline {target} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BaselineError(f"baseline {target} must decode to an object")
    return payload


def validate_baseline(
    record: Mapping[str, Any],
    *,
    expected_fingerprint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail-closed validation of a baseline record for scoring use.

    Rejects unknown schemas, task-id/version drift, ``TBD-unverified``
    placeholders, zero/invalid/non-finite baseline metrics, bad
    tolerances, thin provenance, and any fingerprint mismatch (pinned
    source, image, candidate, or protected-asset change invalidates the
    record). Returns the record as a plain dict on success.
    """
    if not isinstance(record, Mapping):
        raise BaselineError("baseline record must be an object")
    if record.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise BaselineError(
            f"unknown baseline schema {record.get('schema_version')!r} "
            f"(expected {BASELINE_SCHEMA_VERSION}): fail closed"
        )
    if record.get("task_id") != gcd.GCD_TASK_ID:
        raise BaselineError(f"baseline task_id {record.get('task_id')!r} != {gcd.GCD_TASK_ID!r}")
    if record.get("task_version") != gcd.GCD_TASK_VERSION:
        raise BaselineError(
            f"baseline task_version {record.get('task_version')!r} != {gcd.GCD_TASK_VERSION!r}"
        )
    if record.get("status") != BASELINE_STATUS_VERIFIED:
        raise BaselineError(
            f"baseline status {record.get('status')!r} is not usable for scoring "
            f"(need {BASELINE_STATUS_VERIFIED!r}): fail closed"
        )
    current = fingerprint_inputs(record.get("candidate", {}))
    for key in ("orfs_commit", "image_pinned_ref", "candidate_hash", "protected_hash"):
        stored = record.get(key)
        if stored != current[key]:
            raise BaselineError(
                f"baseline fingerprint mismatch on {key} "
                f"(stored {stored!r} != current {current[key]!r}): "
                "source/config/toolchain change invalidates the baseline"
            )
    if gcd.candidate_with_defaults(record.get("candidate", {})) != gcd.stock_candidate_config():
        raise BaselineError(
            "baseline candidate is not the pinned stock candidate: "
            "the reference record only covers the stock task"
        )
    if expected_fingerprint is not None:
        for key in ("orfs_commit", "image_pinned_ref", "candidate_hash", "protected_hash"):
            if key in expected_fingerprint and record.get(key) != expected_fingerprint[key]:
                raise BaselineError(
                    f"baseline fingerprint mismatch on {key} vs expected: fail closed"
                )
    metrics = record.get("metrics")
    if not isinstance(metrics, Mapping):
        raise BaselineError("baseline metrics must be an object")
    for key in METRIC_KEYS:
        value = metrics.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise BaselineError(f"invalid baseline metric {key}={value!r}: must be finite")
    if not metrics["area_um2"] > 0:
        raise BaselineError(f"invalid baseline area {metrics['area_um2']!r}: must be > 0")
    if not metrics.get("valid") or not metrics.get("routed_ok"):
        raise BaselineError("invalid baseline metrics: must be valid with routed completion")
    _require_constraints(metrics)
    _check_tolerances(record.get("tolerances", {}))
    provenance = record.get("provenance")
    if not isinstance(provenance, Mapping):
        raise BaselineError("baseline provenance must be an object")
    for key in ("run_ids", "seeds"):
        values = provenance.get(key)
        if not isinstance(values, list) or len(values) != REQUIRED_RUN_COUNT:
            raise BaselineError(
                f"baseline provenance.{key} must list exactly {REQUIRED_RUN_COUNT} entries"
            )
    runs = record.get("runs")
    if not isinstance(runs, list) or len(runs) != REQUIRED_RUN_COUNT:
        raise BaselineError(f"baseline runs must list exactly {REQUIRED_RUN_COUNT} entries")
    for run in runs:
        if not isinstance(run, Mapping) or not isinstance(run.get("metrics"), Mapping):
            raise BaselineError("baseline run must contain metrics")
        _require_constraints(run["metrics"])
    return dict(record)


def check_metrics_against_baseline(
    baseline: Mapping[str, Any],
    candidate_metrics: Any,
) -> list[str]:
    """Compare candidate metrics to a validated baseline.

    Returns reason codes (empty list means a match). The baseline is
    validated first, so a TBD/unverified or drifted-pin record fails
    closed via :class:`BaselineError` before any comparison. Reasons:
    ``candidate-invalid``, ``metric-drift`` (numeric tolerances),
    ``completion-mismatch``, ``drc-mismatch``,
    ``unconstrained-mismatch``.
    """
    record = validate_baseline(baseline)
    tols = _check_tolerances(record["tolerances"])
    stored = record["metrics"]
    entry = metrics_to_record(candidate_metrics)
    reasons: list[str] = []
    if not entry["valid"]:
        reasons.append("candidate-invalid")
        return reasons
    if not within_tolerances(entry, stored, tols):
        reasons.append("metric-drift")
    if bool(entry["routed_ok"]) != bool(stored.get("routed_ok")):
        reasons.append("completion-mismatch")
    for key, code in (
        ("drc_count", "drc-mismatch"),
        ("unconstrained_paths", "unconstrained-mismatch"),
    ):
        expected = stored.get(key)
        if expected is not None and entry.get(key) != expected:
            reasons.append(code)
    return reasons


def summarize(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact diagnosis sample (no runtimes in the match set)."""
    metrics = record.get("metrics", {}) if isinstance(record, Mapping) else {}
    provenance = record.get("provenance", {}) if isinstance(record, Mapping) else {}
    return {
        "schema_version": record.get("schema_version"),
        "status": record.get("status"),
        "task_id": record.get("task_id"),
        "task_version": record.get("task_version"),
        "orfs_commit": record.get("orfs_commit"),
        "candidate_hash": record.get("candidate_hash"),
        "protected_hash": record.get("protected_hash"),
        "metrics": {k: metrics.get(k) for k in (*METRIC_KEYS, "routed_ok", "valid")},
        "tolerances": dict(record.get("tolerances", {})),
        "run_ids": list(provenance.get("run_ids", [])),
    }


__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "BASELINE_STATUS_TBD",
    "BASELINE_STATUS_VERIFIED",
    "DEFAULT_TOLERANCES",
    "METRIC_KEYS",
    "REQUIRED_RUN_COUNT",
    "TOLERANCES_PROVISIONAL_NOTE",
    "BaselineError",
    "build_baseline_record",
    "check_metrics_against_baseline",
    "compute_candidate_hash",
    "compute_protected_hash",
    "fingerprint_inputs",
    "fixed_design_facts",
    "load_baseline",
    "metrics_to_record",
    "summarize",
    "validate_baseline",
    "within_tolerances",
]
