"""Correctness-gated GCD area scoring (M1-06).

Pure grader over normalized final-stage metrics, immutable-input
evidence, and an approved (verified) baseline. Area improvements count
only when the fixed design is preserved: final-route completion,
required checks (DRC clean), no unconstrained paths, and fixed WNS/TNS
bounds must all hold, and the protected/toolchain fingerprints must
match the baseline record.

Scoring rule (explicit and bounded)::

    valid reward = 0.5 + 0.5 * clamp((baseline_area - candidate_area)
                                     / baseline_area, -1, 1)
    invalid candidate reward = 0.0

So the stock candidate (``candidate_area == baseline_area``) scores
exactly ``0.5``; a smaller feasible area scores above ``0.5`` (up to
``1.0``); a larger feasible area scores below ``0.5`` (down to
``0.0``); any correctness failure scores ``0.0`` (fail closed).

Score, feasibility, and raw area delta are returned separately:
``feasibility`` reflects the design constraints (routing, checks,
timing) while ``valid`` additionally requires baseline and evidence
trust (approved baseline, matching hashes). Infrastructure failure
carries no trainable reward: see :func:`grade_infra_error`, whose
result is marked ``excluded_from_aggregates`` and must be excluded
from training aggregates.

Reason codes (machine-readable, ``reason_codes`` tuple):

- ``ok``: all gates passed.
- ``timing-violation``: WNS/TNS below the fixed bounds.
- ``drc-dirty``: DRC violation count nonzero.
- ``unconstrained-paths``: nonzero unconstrained-path count.
- ``route-incomplete``: final-route completion not evidenced.
- ``hash-mismatch``: protected/toolchain hash present but unequal.
- ``evidence-mismatch``: evidence check field disagrees with metrics.
- ``baseline-invalid``: baseline record rejected for scoring use
  (includes the ``TBD-unverified`` placeholder and zero/invalid area).
- ``missing-evidence``: evidence absent, incomplete, or a required
  check indicator unknown (fail closed, never defaulted to clean).
- ``nonfinite-metric``: area/slack missing, non-finite, or
  non-positive candidate area.
- ``invalid-metrics``: ``metrics.valid`` is false.
- ``infra-error-excluded``: infrastructure outcome, no trainable reward.

Sign convention: OpenSTA reports negative slack on violation, so the
fixed bounds are ``WNS >= 0`` and ``TNS >= 0`` ns (a metrics-valid
``TNS`` is ``<= 0`` by construction, hence the TNS gate requires
exactly zero). Bounds are absolute task constants
(:data:`WNS_MIN_NS`, :data:`TNS_MIN_NS`), never taken from the
baseline.

Stdlib-only, Python >= 3.10. No EDA tools, Docker, network, or API
keys. Pure functions (no I/O).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from silicon_env.environments.openroad import baseline as bl
from silicon_env.environments.openroad.metrics import AREA_UNIT, SLACK_UNIT, GcdMetrics
from silicon_env.types import ContractError, GradeStatus, Metric

#: Fixed worst-negative-slack bound in ns (OpenSTA sign convention:
#: negative slack is a timing violation, so no violation means >= 0).
WNS_MIN_NS = 0.0

#: Fixed total-negative-slack bound in ns (metrics-valid TNS is <= 0
#: by construction, so this gate requires exactly zero).
TNS_MIN_NS = 0.0

#: Reward baseline: stock (candidate == baseline) scores exactly this.
REWARD_BASE = 0.5

#: Reward scale applied to the clamped relative area improvement.
REWARD_SCALE = 0.5

#: Symmetric clamp bound on the relative area improvement.
REL_IMPROVEMENT_BOUND = 1.0


@dataclass(frozen=True)
class GcdGrade:
    """Outcome of grading one GCD candidate."""

    valid: bool
    reward: float
    feasibility: bool
    area_delta: float
    reason_codes: tuple[str, ...] = ()
    metrics: tuple[Metric, ...] = ()
    status: GradeStatus = GradeStatus.FAIL
    infra_error: bool = False
    excluded_from_aggregates: bool = False
    message: str = ""
    baseline_area: float = 0.0
    candidate_area: float = 0.0

    @property
    def score(self) -> float:
        """Alias for :attr:`reward` (score == reward, always)."""
        return self.reward

    @property
    def ok(self) -> bool:
        """Alias for :attr:`valid` (mirrors ``GcdMetrics.ok``)."""
        return self.valid

    def to_dict(self) -> dict[str, Any]:
        """Return a strict-JSON-serializable diagnosis dict."""
        for metric in self.metrics:
            metric.validate()
        return {
            "valid": self.valid,
            "reward": float(self.reward),
            "score": float(self.reward),
            "feasibility": self.feasibility,
            "area_delta": float(self.area_delta),
            "reason_codes": list(self.reason_codes),
            "metrics": [m.to_dict() for m in self.metrics],
            "status": self.status.value,
            "infra_error": self.infra_error,
            "excluded_from_aggregates": self.excluded_from_aggregates,
            "message": self.message,
            "baseline_area": float(self.baseline_area),
            "candidate_area": float(self.candidate_area),
        }


def summarize(grade: GcdGrade) -> dict[str, Any]:
    """Return a compact diagnosis sample for one :class:`GcdGrade`."""
    return {
        "valid": grade.valid,
        "reward": grade.reward,
        "score": grade.score,
        "feasibility": grade.feasibility,
        "area_delta": grade.area_delta,
        "reason_codes": list(grade.reason_codes),
        "status": grade.status.value,
        "infra_error": grade.infra_error,
        "excluded_from_aggregates": grade.excluded_from_aggregates,
    }


def clamp_rel_improvement(value: float) -> float:
    """Clamp a relative area improvement to ``[-1, 1]`` (inclusive)."""
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ContractError(f"relative improvement must be finite, got {value!r}")
    number = float(value)
    return max(-REL_IMPROVEMENT_BOUND, min(REL_IMPROVEMENT_BOUND, number))


def area_reward(baseline_area: float, candidate_area: float) -> float:
    """Apply the bounded scoring formula to two finite positive areas."""
    for name, area in (("baseline_area", baseline_area), ("candidate_area", candidate_area)):
        if isinstance(area, bool) or not isinstance(area, (int, float)):
            raise ContractError(f"{name} must be a number, got {type(area).__name__}")
        if not math.isfinite(float(area)) or not float(area) > 0:
            raise ContractError(f"{name} must be finite and > 0, got {area!r}")
    base, cand = float(baseline_area), float(candidate_area)
    return REWARD_BASE + REWARD_SCALE * clamp_rel_improvement((base - cand) / base)


def _is_clean_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def grade_gcd_candidate(
    candidate_metrics: GcdMetrics,
    *,
    baseline_record: Mapping[str, Any],
    evidence: Mapping[str, Any] | None = None,
) -> GcdGrade:
    """Grade one candidate's normalized metrics against an approved baseline.

    :param candidate_metrics: normalized final-stage :class:`GcdMetrics`.
    :param baseline_record: approved baseline mapping; must pass
        :func:`baseline.validate_baseline` (the ``TBD-unverified``
        placeholder and zero/invalid-area records fail closed with
        ``baseline-invalid``).
    :param evidence: immutable-input evidence mapping. Requires
        ``protected_hash`` (must equal the baseline's protected hash).
        Optional ``orfs_commit`` / ``image_pinned_ref`` entries, when
        present, must equal the baseline's. Optional ``routed_ok``,
        ``drc_count``, ``unconstrained_paths``, ``wns_ns``, ``tns_ns``
        entries, when present, must agree with ``candidate_metrics``
        (disagreement fails closed with ``evidence-mismatch``).
    :returns: a :class:`GcdGrade` with the bounded reward (``0.0`` when
        invalid), feasibility, raw area delta, and reason codes.
    """
    reasons: list[str] = []

    # --- baseline trust gate -------------------------------------------------
    try:
        record = bl.validate_baseline(baseline_record)
    except (bl.BaselineError, ContractError) as exc:
        return _invalid(
            feasibility=False,
            reasons=("baseline-invalid",),
            message=f"baseline rejected for scoring use: {exc}",
        )
    baseline_metrics = record["metrics"]
    baseline_area = float(baseline_metrics["area_um2"])  # finite and > 0: validated
    baseline_protected = record.get("protected_hash")

    # --- evidence trust gate ---------------------------------------------------
    if not isinstance(evidence, Mapping):
        return _invalid(
            feasibility=_feasible_fields(candidate_metrics),
            reasons=("missing-evidence",),
            message="evidence mapping is missing: fail closed",
            baseline_area=baseline_area,
            candidate_area=_area_or_zero(candidate_metrics),
        )
    protected_hash = evidence.get("protected_hash")
    if not isinstance(protected_hash, str) or not protected_hash:
        reasons.append("missing-evidence")
    elif protected_hash != baseline_protected:
        reasons.append("hash-mismatch")
    for key in ("orfs_commit", "image_pinned_ref"):
        if key in evidence and evidence[key] is not None and evidence[key] != record.get(key):
            if "hash-mismatch" not in reasons:
                reasons.append("hash-mismatch")
    if isinstance(candidate_metrics, GcdMetrics):
        for key, attr in (
            ("routed_ok", "routed_ok"),
            ("drc_count", "drc_count"),
            ("unconstrained_paths", "unconstrained_paths"),
            ("wns_ns", "wns_ns"),
            ("tns_ns", "tns_ns"),
        ):
            if key in evidence and evidence[key] is not None:
                if evidence[key] != getattr(candidate_metrics, attr):
                    if "evidence-mismatch" not in reasons:
                        reasons.append("evidence-mismatch")
                    break

    # --- candidate correctness gates (all must pass for valid) -----------------
    if not isinstance(candidate_metrics, GcdMetrics):
        reasons.append("invalid-metrics")
        candidate_area: float | None = None
        wns = tns = None
    else:
        if not candidate_metrics.valid:
            reasons.append("invalid-metrics")
        candidate_area = _finite_number(candidate_metrics.area_um2)
        if candidate_area is None or not candidate_area > 0:
            if "nonfinite-metric" not in reasons:
                reasons.append("nonfinite-metric")
        wns = _finite_number(candidate_metrics.wns_ns)
        tns = _finite_number(candidate_metrics.tns_ns)
        if wns is None or tns is None:
            if "nonfinite-metric" not in reasons:
                reasons.append("nonfinite-metric")
        elif wns < WNS_MIN_NS or tns < TNS_MIN_NS:
            reasons.append("timing-violation")
        if not candidate_metrics.routed_ok:
            reasons.append("route-incomplete")
        if candidate_metrics.drc_count is None or candidate_metrics.unconstrained_paths is None:
            reasons.append("missing-evidence")
        else:
            if not _is_clean_int(candidate_metrics.drc_count) or candidate_metrics.drc_count != 0:
                reasons.append("drc-dirty")
            if (
                not _is_clean_int(candidate_metrics.unconstrained_paths)
                or candidate_metrics.unconstrained_paths != 0
            ):
                reasons.append("unconstrained-paths")

    feasible = _feasible_fields(candidate_metrics)
    if reasons:
        return _invalid(
            feasibility=feasible,
            reasons=tuple(reasons),
            message=f"candidate fails correctness gates: {'; '.join(reasons)}",
            baseline_area=baseline_area,
            candidate_area=candidate_area if candidate_area is not None else 0.0,
        )

    # --- valid: apply the explicit bounded formula ------------------------------
    assert candidate_area is not None and candidate_area > 0
    reward = REWARD_BASE + REWARD_SCALE * clamp_rel_improvement(
        (baseline_area - candidate_area) / baseline_area
    )
    delta = baseline_area - candidate_area
    typed: list[Metric] = list(candidate_metrics.metrics())
    typed.append(Metric(name="reward", value=reward, unit="score"))
    typed.append(Metric(name="area_delta", value=delta, unit=AREA_UNIT))
    for metric in typed:
        metric.validate()
    return GcdGrade(
        valid=True,
        reward=reward,
        feasibility=True,
        area_delta=delta,
        reason_codes=("ok",),
        metrics=tuple(typed),
        status=GradeStatus.PASS,
        infra_error=False,
        excluded_from_aggregates=False,
        message="candidate passes all correctness gates",
        baseline_area=baseline_area,
        candidate_area=candidate_area,
    )


def _feasible_fields(candidate: Any) -> bool:
    """Return True when the design constraints hold (ignoring trust gates)."""
    if not isinstance(candidate, GcdMetrics):
        return False
    area = _finite_number(candidate.area_um2)
    wns = _finite_number(candidate.wns_ns)
    tns = _finite_number(candidate.tns_ns)
    return bool(
        candidate.valid
        and area is not None
        and area > 0
        and wns is not None
        and tns is not None
        and wns >= WNS_MIN_NS
        and tns >= TNS_MIN_NS
        and candidate.routed_ok
        and _is_clean_int(candidate.drc_count)
        and candidate.drc_count == 0
        and _is_clean_int(candidate.unconstrained_paths)
        and candidate.unconstrained_paths == 0
    )


def _area_or_zero(candidate: Any) -> float:
    area = _finite_number(getattr(candidate, "area_um2", None))
    return area if area is not None and area > 0 else 0.0


def _invalid(
    *,
    feasibility: bool,
    reasons: tuple[str, ...],
    message: str,
    baseline_area: float = 0.0,
    candidate_area: float = 0.0,
) -> GcdGrade:
    reward = 0.0
    typed = [Metric(name="reward", value=reward, unit="score")]
    delta = 0.0
    if (
        math.isfinite(baseline_area)
        and baseline_area > 0
        and math.isfinite(candidate_area)
        and candidate_area > 0
    ):
        delta = baseline_area - candidate_area
        typed.append(Metric(name="area_delta", value=delta, unit=AREA_UNIT))
    return GcdGrade(
        valid=False,
        reward=reward,
        feasibility=feasibility,
        area_delta=delta,
        reason_codes=reasons,
        metrics=tuple(typed),
        status=GradeStatus.FAIL,
        infra_error=False,
        excluded_from_aggregates=False,
        message=message,
        baseline_area=baseline_area if math.isfinite(baseline_area) else 0.0,
        candidate_area=candidate_area if math.isfinite(candidate_area) else 0.0,
    )


def grade_infra_error(reason: str = "infra-error-excluded", detail: str = "") -> GcdGrade:
    """Build a non-trainable grade for an infrastructure failure.

    The result carries ``reward == 0.0`` but is marked
    ``excluded_from_aggregates=True`` with status ``infra_error``: it
    must be excluded from training aggregates (it is not a ``0.0``
    earned by a bad design). Never valid, never feasible.
    """
    codes = ["infra-error-excluded"]
    if isinstance(reason, str) and reason and reason not in codes:
        codes.append(reason)
    message = "infrastructure failure: no trainable reward"
    if isinstance(detail, str) and detail:
        message += f": {detail}"
    return GcdGrade(
        valid=False,
        reward=0.0,
        feasibility=False,
        area_delta=0.0,
        reason_codes=tuple(codes),
        metrics=(Metric(name="reward", value=0.0, unit="score"),),
        status=GradeStatus.INFRA_ERROR,
        infra_error=True,
        excluded_from_aggregates=True,
        message=message,
        baseline_area=0.0,
        candidate_area=0.0,
    )


__all__ = [
    "AREA_UNIT",
    "REL_IMPROVEMENT_BOUND",
    "REWARD_BASE",
    "REWARD_SCALE",
    "SLACK_UNIT",
    "TNS_MIN_NS",
    "WNS_MIN_NS",
    "GcdGrade",
    "area_reward",
    "clamp_rel_improvement",
    "grade_gcd_candidate",
    "grade_infra_error",
    "summarize",
]
