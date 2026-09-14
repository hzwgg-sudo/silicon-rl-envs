"""Typed GCD final-stage metrics parser (M1-04).

Normalizes a small metric set -- cell area, worst/total negative slack
(WNS/TNS), routed completion, and available DRC/unconstrained-path
indicators -- from the pinned final-stage report schema into typed,
unit-explicit :class:`GcdMetrics`.

Pinned schema: ``orfs-26Q2-final-v1``. The parser only accepts
``stage == "final"``; intermediate placement metrics can never
substitute for final routed timing metrics. Missing, malformed,
truncated, or unknown-schema reports produce an explicit invalid
:class:`GcdMetrics` (``valid is False`` with machine-readable
``reasons``), never favorable defaults.

Report formats (declared contract; exact on-disk ORFS 26Q2 names/paths
are pending verification by a real pinned-image run on the Linux route,
blocked on the Mac dev host):

- timing: OpenSTA ``report_checks`` style text carrying WNS/TNS lines,
  e.g. ``wns -0.023 ns`` / ``tns -0.145 ns`` (also accepts
  ``worst slack`` / ``total negative slack`` spellings). Default unit
  is ns; explicit ``ps``/``us``/``ms``/``s`` suffixes are converted to
  ns.
- area: ``report_design_area`` style text carrying a design/cell area
  line, e.g. ``Design area 1234.5 u^2``. Default unit is um^2;
  explicit ``nm^2``/``mm^2`` suffixes are converted to um^2.
- completion: an explicit routed-completion marker, e.g.
  ``routed_completion: true`` or ``detailed routing completed``.
- DRC (optional): violation count text, e.g. ``drc_violations: 0``.
  When absent the count is recorded as unknown (``None`` with a
  ``drc-unknown`` note), never defaulted to zero.

Validity requires: ``stage == "final"``, pinned ``schema``, finite
area/wns/tns (area > 0, tns <= 0), a positive completion marker, zero
unconstrained paths when the indicator is present, and zero DRC
violations when the indicator is present. Unknown DRC/unconstrained
indicators are recorded explicitly as ``None`` plus notes and do not
by themselves invalidate otherwise complete final metrics.

Stdlib-only, Python >= 3.10. No EDA tools, Docker, network, or API
keys. Pure text parsing except for the ``*_files`` convenience
 wrappers, which only read the given local paths.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from silicon_env.types import Metric

#: Pinned final-stage report schema accepted by this parser.
PINNED_SCHEMA = "orfs-26Q2-final-v1"

#: Fixed endpoint this parser accepts metrics from (mirrors
#: ``config.GCD_ENDPOINT`` / ``flow.FLOW_ENDPOINT``).
FINAL_STAGE = "final"

#: Canonical units of the normalized output.
AREA_UNIT = "um^2"
SLACK_UNIT = "ns"


class MetricsError(ValueError):
    """Parser misuse (bad argument types, not report content).

    Malformed report *content* never raises: it yields an invalid
    :class:`GcdMetrics` with explicit reason codes instead.
    """


@dataclass(frozen=True)
class GcdMetrics:
    """Normalized final-stage GCD metrics with validity evidence."""

    stage: str
    schema: str
    area_um2: float | None
    wns_ns: float | None
    tns_ns: float | None
    routed_ok: bool
    drc_count: int | None
    unconstrained_paths: int | None
    valid: bool
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    raw_refs: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Alias for :attr:`valid` (mirrors ``FlowResult.ok``)."""
        return self.valid

    def metrics(self) -> tuple[Metric, ...]:
        """Successfully parsed finite fields as typed metrics with units."""
        out: list[Metric] = []
        if self.area_um2 is not None and math.isfinite(self.area_um2):
            out.append(Metric(name="cell_area", value=self.area_um2, unit=AREA_UNIT))
        if self.wns_ns is not None and math.isfinite(self.wns_ns):
            out.append(Metric(name="wns", value=self.wns_ns, unit=SLACK_UNIT))
        if self.tns_ns is not None and math.isfinite(self.tns_ns):
            out.append(Metric(name="tns", value=self.tns_ns, unit=SLACK_UNIT))
        for metric in out:
            metric.validate()
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "schema": self.schema,
            "area_um2": self.area_um2,
            "wns_ns": self.wns_ns,
            "tns_ns": self.tns_ns,
            "routed_ok": self.routed_ok,
            "drc_count": self.drc_count,
            "unconstrained_paths": self.unconstrained_paths,
            "valid": self.valid,
            "reasons": list(self.reasons),
            "notes": list(self.notes),
            "raw_refs": dict(self.raw_refs),
            "metrics": [m.to_dict() for m in self.metrics()],
        }


def summarize(metrics: GcdMetrics) -> dict[str, Any]:
    """Return a compact diagnosis sample for one :class:`GcdMetrics`."""
    return {
        "stage": metrics.stage,
        "schema": metrics.schema,
        "valid": metrics.valid,
        "area_um2": metrics.area_um2,
        "wns_ns": metrics.wns_ns,
        "tns_ns": metrics.tns_ns,
        "routed_ok": metrics.routed_ok,
        "drc_count": metrics.drc_count,
        "unconstrained_paths": metrics.unconstrained_paths,
        "reasons": list(metrics.reasons),
        "notes": list(metrics.notes),
        "raw_refs": dict(metrics.raw_refs),
    }


# --- unit conversion ---------------------------------------------------------

_TIME_TO_NS = {"ns": 1.0, "ps": 1e-3, "us": 1e3, "ms": 1e6, "s": 1e9}
_AREA_TO_UM2 = {
    "um^2": 1.0,
    "u^2": 1.0,
    "um2": 1.0,
    "sq_um": 1.0,
    "nm^2": 1e-6,
    "mm^2": 1e6,
}

_NONFINITE_TOKENS = frozenset({"nan", "+nan", "-nan", "inf", "+inf", "-inf",
                               "infinity", "+infinity", "-infinity"})

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def _parse_number(token: str) -> float | None:
    """Parse a numeric token; return None for non-finite/malformed input."""
    text = token.strip().lower()
    if text in _NONFINITE_TOKENS:
        return None
    if not re.fullmatch(_NUMBER, text):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


# --- report line patterns -----------------------------------------------------

_WNS_RES = (
    re.compile(rf"\bwns\b\s*[:=]?\s*({_NUMBER}|[A-Za-z+-]+)\s*(ns|ps|us|ms|s)?\b",
               re.IGNORECASE),
    re.compile(rf"\bworst\s+(negative\s+)?slack\b[^0-9A-Za-z+-]*({_NUMBER}|[A-Za-z+-]+)"
               r"\s*(ns|ps|us|ms|s)?\b", re.IGNORECASE),
)
_TNS_RES = (
    re.compile(rf"\btns\b\s*[:=]?\s*({_NUMBER}|[A-Za-z+-]+)\s*(ns|ps|us|ms|s)?\b",
               re.IGNORECASE),
    re.compile(rf"\btotal\s+(negative\s+)?slack\b[^0-9A-Za-z+-]*({_NUMBER}|[A-Za-z+-]+)"
               r"\s*(ns|ps|us|ms|s)?\b", re.IGNORECASE),
)
_AREA_RES = (
    re.compile(rf"\b(?:design|cell|total(?:\s+cell)?|chip|die)\s+area\b\s*[:=]?\s*"
               rf"({_NUMBER}|[A-Za-z+-]+)\s*(um\^2|u\^2|um2|sq_um|nm\^2|mm\^2)?\b",
               re.IGNORECASE),
    re.compile(rf"\barea\b\s*[:=]\s*({_NUMBER}|[A-Za-z+-]+)"
               r"\s*(um\^2|u\^2|um2|sq_um|nm\^2|mm\^2)?\b", re.IGNORECASE),
)
_UNCONSTRAINED_RES = (
    re.compile(r"\bunconstrained[_ ]?(?:endpoints?|paths?)?\s*[:=]\s*(\d+)",
               re.IGNORECASE),
    re.compile(r"\bfound\s+(\d+)\s+unconstrained\b", re.IGNORECASE),
)
_DRC_COUNT_RES = (
    re.compile(r"\bdrc[_ ]?(?:violations?|count|errors?)?\s*[:=]\s*(\d+)",
               re.IGNORECASE),
    re.compile(r"\btotal\s+violations?\b\s*[:=]?\s*(\d+)", re.IGNORECASE),
    re.compile(r"\bviolations?\b\s*[:=]\s*(\d+)", re.IGNORECASE),
)
_DRC_CLEAN_RES = (
    re.compile(r"\bno\s+drc\s+violations?\b", re.IGNORECASE),
    re.compile(r"\bdrc\s+clean\b", re.IGNORECASE),
    re.compile(r"\b0\s+drc\s+violations?\b", re.IGNORECASE),
)
_COMPLETION_TRUE_RES = (
    re.compile(r"\brouted[_ ]completion\s*[:=]\s*(true|1|ok|complete[sd]?|yes|pass)\b",
               re.IGNORECASE),
    re.compile(r"\b(?:detailed\s+)?routing\s+completed(?:\s+successfully)?\b",
               re.IGNORECASE),
    re.compile(r"\bflow\s+completed\b.*\bfinal\b", re.IGNORECASE),
)
_COMPLETION_FALSE_RES = (
    re.compile(r"\brouted[_ ]completion\s*[:=]\s*(false|0|no|fail(?:ed)?)\b",
               re.IGNORECASE),
    re.compile(r"\brouting\s+(?:not\s+completed|failed|incomplete)\b", re.IGNORECASE),
)
_SCHEMA_RE = re.compile(r"^\s*schema\s*[:=]\s*(.+?)\s*$",
                        re.IGNORECASE | re.MULTILINE)
_STAGE_RE = re.compile(r"^\s*stage\s*[:=]\s*(\S+)\s*$",
                       re.IGNORECASE | re.MULTILINE)


def _search_timed(patterns: tuple[re.Pattern[str], ...], text: str,
                  ) -> tuple[str, str] | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            raw = list(match.groups())
            if len(raw) == 3:  # "worst/total ... slack" spelling
                _, value, unit = raw
            else:  # "wns:"/"tns:" spelling
                value, unit = raw[0], raw[1] if len(raw) > 1 else None
            if not isinstance(value, str):
                continue
            if not isinstance(unit, str) or unit.lower() not in _TIME_TO_NS:
                unit = ""
            return value, unit
    return None


def _search_area(patterns: tuple[re.Pattern[str], ...], text: str,
                 ) -> tuple[str, str] | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            value, unit = match.group(1), (match.group(2) or "")
            return value, unit
    return None


def _check_arg_types(timing_text: Any, area_text: Any, drc_text: Any,
                     stage: Any, schema: Any) -> None:
    for name, value in (("timing_text", timing_text), ("area_text", area_text)):
        if value is not None and not isinstance(value, str):
            raise MetricsError(f"{name} must be a string or None")
    if drc_text is not None and not isinstance(drc_text, str):
        raise MetricsError("drc_text must be a string or None")
    if not isinstance(stage, str):
        raise MetricsError("stage must be a string")
    if not isinstance(schema, str):
        raise MetricsError("schema must be a string")


def parse_final_reports(
    timing_text: str | None = None,
    area_text: str | None = None,
    drc_text: str | None = None,
    *,
    stage: str = FINAL_STAGE,
    schema: str = PINNED_SCHEMA,
    timing_ref: str = "<timing_text>",
    area_ref: str = "<area_text>",
    drc_ref: str = "<drc_text>",
) -> GcdMetrics:
    """Parse pinned-schema final-stage report texts into :class:`GcdMetrics`.

    :param timing_text: final routed timing report text (OpenSTA
        ``report_checks`` style). ``None``/empty means the report is
        missing and yields invalid metrics.
    :param area_text: final design-area report text
        (``report_design_area`` style). Same missing-data rule.
    :param drc_text: optional DRC report text. ``None`` records an
        explicit unknown (``drc_count is None`` + ``drc-unknown`` note).
    :param stage: source stage label; anything but ``"final"`` yields
        invalid metrics with reason ``wrong-stage``.
    :param schema: caller-declared report schema; anything but
        :data:`PINNED_SCHEMA` yields invalid metrics with reason
        ``unknown-schema`` (fail closed, never a favorable default).
    :param timing_ref / area_ref / drc_ref: raw report references
        retained verbatim for diagnosis.
    """
    _check_arg_types(timing_text, area_text, drc_text, stage, schema)
    for name, value in (("timing_ref", timing_ref), ("area_ref", area_ref),
                        ("drc_ref", drc_ref)):
        if not isinstance(value, str):
            raise MetricsError(f"{name} must be a string")

    raw_refs = {"timing": timing_ref, "area": area_ref}
    if drc_text is not None:
        raw_refs["drc"] = drc_ref

    reasons: list[str] = []
    notes: list[str] = []

    def invalid(**values: Any) -> GcdMetrics:
        return GcdMetrics(
            stage=stage if isinstance(stage, str) else str(stage),
            schema=schema if isinstance(schema, str) else str(schema),
            area_um2=values.get("area_um2"),
            wns_ns=values.get("wns_ns"),
            tns_ns=values.get("tns_ns"),
            routed_ok=bool(values.get("routed_ok", False)),
            drc_count=values.get("drc_count"),
            unconstrained_paths=values.get("unconstrained_paths"),
            valid=False,
            reasons=tuple(reasons),
            notes=tuple(notes),
            raw_refs=dict(raw_refs),
        )

    # --- schema / stage gates (fail closed) ----------------------------------
    if schema != PINNED_SCHEMA:
        reasons.append("unknown-schema")
        return invalid()
    if stage != FINAL_STAGE:
        reasons.append("wrong-stage")

    # Embedded headers, when present, must agree (real tool output has no
    # embedded header; the check only fires when a header is present).
    for label, text in (("timing", timing_text), ("area", area_text)):
        if isinstance(text, str) and text.strip():
            schema_match = _SCHEMA_RE.search(text)
            if schema_match and schema_match.group(1) != PINNED_SCHEMA:
                if "unknown-schema" not in reasons:
                    reasons.append("unknown-schema")
            stage_match = _STAGE_RE.search(text)
            if stage_match and stage_match.group(1).lower() != FINAL_STAGE:
                if "wrong-stage" not in reasons:
                    reasons.append("wrong-stage")

    # --- presence gates --------------------------------------------------------
    if not isinstance(timing_text, str) or not timing_text.strip():
        if "missing-timing-report" not in reasons:
            reasons.append("missing-timing-report")
    if not isinstance(area_text, str) or not area_text.strip():
        if "missing-area-report" not in reasons:
            reasons.append("missing-area-report")

    # --- timing: WNS / TNS -----------------------------------------------------
    wns_ns: float | None = None
    tns_ns: float | None = None
    if isinstance(timing_text, str) and timing_text.strip():
        wns_hit = _search_timed(_WNS_RES, timing_text)
        if wns_hit is None:
            reasons.append("missing-wns")
        else:
            raw_value, raw_unit = wns_hit
            parsed = _parse_number(raw_value)
            if parsed is None:
                reasons.append("non-finite-wns")
            else:
                unit = (raw_unit or "ns").lower()
                if unit not in _TIME_TO_NS:
                    reasons.append("non-finite-wns")
                else:
                    wns_ns = parsed * _TIME_TO_NS[unit]
        tns_hit = _search_timed(_TNS_RES, timing_text)
        if tns_hit is None:
            reasons.append("missing-tns")
        else:
            raw_value, raw_unit = tns_hit
            parsed = _parse_number(raw_value)
            if parsed is None:
                reasons.append("non-finite-tns")
            else:
                unit = (raw_unit or "ns").lower()
                if unit not in _TIME_TO_NS:
                    reasons.append("non-finite-tns")
                else:
                    tns_ns = parsed * _TIME_TO_NS[unit]
                    if tns_ns > 0:
                        reasons.append("tns-positive")
    else:
        if "missing-wns" not in reasons:
            reasons.append("missing-wns")
        if "missing-tns" not in reasons:
            reasons.append("missing-tns")

    # --- area ------------------------------------------------------------------
    area_um2: float | None = None
    if isinstance(area_text, str) and area_text.strip():
        area_hit = _search_area(_AREA_RES, area_text)
        if area_hit is None:
            reasons.append("missing-area")
        else:
            raw_value, raw_unit = area_hit
            parsed = _parse_number(raw_value)
            if parsed is None:
                reasons.append("non-finite-area")
            else:
                unit = (raw_unit or "um^2").lower()
                if unit not in _AREA_TO_UM2:
                    reasons.append("non-finite-area")
                else:
                    area_um2 = parsed * _AREA_TO_UM2[unit]
                    if not area_um2 > 0:
                        reasons.append("area-nonpositive")
                        area_um2 = None if not math.isfinite(area_um2) else area_um2
    else:
        if "missing-area" not in reasons:
            reasons.append("missing-area")

    # --- routed completion -----------------------------------------------------
    combined = f"{timing_text or ''}\n{area_text or ''}"
    routed_ok = any(p.search(combined) for p in _COMPLETION_TRUE_RES)
    if any(p.search(combined) for p in _COMPLETION_FALSE_RES):
        routed_ok = False
    if not routed_ok and "missing-completion" not in reasons:
        reasons.append("missing-completion")

    # --- unconstrained paths ---------------------------------------------------
    unconstrained_paths: int | None = None
    if isinstance(timing_text, str) and timing_text.strip():
        for pattern in _UNCONSTRAINED_RES:
            match = pattern.search(timing_text)
            if match:
                unconstrained_paths = int(match.group(1))
                break
    if unconstrained_paths is None:
        notes.append("unconstrained-unknown")
    elif unconstrained_paths > 0:
        reasons.append("unconstrained-paths")

    # --- DRC -------------------------------------------------------------------
    drc_count: int | None = None
    if drc_text is None:
        notes.append("drc-unknown")
    elif not drc_text.strip():
        reasons.append("drc-unparseable")
        notes.append("drc-unknown")
    else:
        for pattern in _DRC_COUNT_RES:
            match = pattern.search(drc_text)
            if match:
                drc_count = int(match.group(1))
                break
        if drc_count is None and any(p.search(drc_text) for p in _DRC_CLEAN_RES):
            drc_count = 0
        if drc_count is None:
            reasons.append("drc-unparseable")
            notes.append("drc-unknown")
        elif drc_count > 0:
            reasons.append("drc-violations")

    if reasons:
        return invalid(
            area_um2=area_um2,
            wns_ns=wns_ns,
            tns_ns=tns_ns,
            routed_ok=routed_ok,
            drc_count=drc_count,
            unconstrained_paths=unconstrained_paths,
        )
    return GcdMetrics(
        stage=stage,
        schema=schema,
        area_um2=area_um2,
        wns_ns=wns_ns,
        tns_ns=tns_ns,
        routed_ok=routed_ok,
        drc_count=drc_count,
        unconstrained_paths=unconstrained_paths,
        valid=True,
        reasons=(),
        notes=tuple(notes),
        raw_refs=dict(raw_refs),
    )


def parse_final_report_files(
    *,
    timing_path: str | Path,
    area_path: str | Path,
    drc_path: str | Path | None = None,
    stage: str = FINAL_STAGE,
    schema: str = PINNED_SCHEMA,
    encoding: str = "utf-8",
) -> GcdMetrics:
    """Read report files from local paths and parse them as final metrics.

    Unreadable/missing files yield invalid metrics (reason
    ``file-unreadable`` plus the relevant missing-report reason), never
    a raise for content problems. Non-string path arguments raise
    :class:`MetricsError`.
    """
    for name, value in (("timing_path", timing_path), ("area_path", area_path)):
        if not isinstance(value, (str, Path)):
            raise MetricsError(f"{name} must be a path string")
    if drc_path is not None and not isinstance(drc_path, (str, Path)):
        raise MetricsError("drc_path must be a path string or None")
    if not isinstance(encoding, str) or not encoding:
        raise MetricsError("encoding must be a non-empty string")

    reasons: list[str] = []

    def _read(path: str | Path, label: str) -> str | None:
        try:
            return Path(path).read_text(encoding=encoding)
        except OSError:
            if label not in reasons:
                reasons.append(label)
            return None

    timing_text = _read(timing_path, "missing-timing-report")
    area_text = _read(area_path, "missing-area-report")
    drc_text = _read(drc_path, "missing-drc-report") if drc_path is not None else None

    if timing_text is None or area_text is None:
        # Fail closed but still surface per-field diagnosis via the core
        # parser; merge file-level reasons in front.
        core = parse_final_reports(
            timing_text if timing_text is not None else "",
            area_text if area_text is not None else "",
            drc_text,
            stage=stage,
            schema=schema,
            timing_ref=str(timing_path),
            area_ref=str(area_path),
            drc_ref=str(drc_path) if drc_path is not None else "<drc_text>",
        )
        merged = ["file-unreadable", *reasons]
        merged.extend(r for r in core.reasons if r not in merged)
        return GcdMetrics(
            stage=core.stage,
            schema=core.schema,
            area_um2=core.area_um2,
            wns_ns=core.wns_ns,
            tns_ns=core.tns_ns,
            routed_ok=core.routed_ok,
            drc_count=core.drc_count,
            unconstrained_paths=core.unconstrained_paths,
            valid=False,
            reasons=tuple(merged),
            notes=core.notes,
            raw_refs=dict(core.raw_refs),
        )
    return parse_final_reports(
        timing_text,
        area_text,
        drc_text,
        stage=stage,
        schema=schema,
        timing_ref=str(timing_path),
        area_ref=str(area_path),
        drc_ref=str(drc_path) if drc_path is not None else "<drc_text>",
    )


def parse_flow_result(
    flow_result: Any,
    *,
    timing_text: str | None = None,
    area_text: str | None = None,
    drc_text: str | None = None,
    schema: str = PINNED_SCHEMA,
    timing_ref: str = "<timing_text>",
    area_ref: str = "<area_text>",
    drc_ref: str = "<drc_text>",
) -> GcdMetrics:
    """Parse reports in the context of a :class:`FlowResult`.

    The flow's ``stage_reached`` becomes the parser stage label, so any
    non-final flow outcome deterministically yields invalid metrics
    (``wrong-stage``). Flow status/endpoint are retained in ``raw_refs``
    for diagnosis. ``flow_result=None`` raises :class:`MetricsError`
    (caller misuse); a failed flow itself yields invalid metrics, never
    a raise.
    """
    if flow_result is None:
        raise MetricsError("flow_result must not be None")
    stage = getattr(flow_result, "stage_reached", "")
    if not isinstance(stage, str):
        raise MetricsError("flow_result.stage_reached must be a string")
    status = getattr(getattr(flow_result, "status", None), "value", "")
    endpoint = ""
    provenance = getattr(flow_result, "provenance", None)
    if isinstance(provenance, Mapping):
        endpoint = str(provenance.get("endpoint", ""))
    refs = {
        "timing": timing_ref,
        "area": area_ref,
        "flow_status": str(status),
        "flow_stage_reached": str(stage),
    }
    if endpoint:
        refs["flow_endpoint"] = endpoint
    metrics = parse_final_reports(
        timing_text,
        area_text,
        drc_text,
        stage=stage,
        schema=schema,
        timing_ref=timing_ref,
        area_ref=area_ref,
        drc_ref=drc_ref,
    )
    merged_refs = dict(metrics.raw_refs)
    merged_refs.update({k: v for k, v in refs.items()
                        if k not in ("timing", "area")})
    return GcdMetrics(
        stage=metrics.stage,
        schema=metrics.schema,
        area_um2=metrics.area_um2,
        wns_ns=metrics.wns_ns,
        tns_ns=metrics.tns_ns,
        routed_ok=metrics.routed_ok,
        drc_count=metrics.drc_count,
        unconstrained_paths=metrics.unconstrained_paths,
        valid=metrics.valid,
        reasons=metrics.reasons,
        notes=metrics.notes,
        raw_refs=merged_refs,
    )


__all__ = [
    "AREA_UNIT",
    "FINAL_STAGE",
    "PINNED_SCHEMA",
    "SLACK_UNIT",
    "GcdMetrics",
    "MetricsError",
    "parse_final_report_files",
    "parse_final_reports",
    "parse_flow_result",
    "summarize",
]
