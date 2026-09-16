"""Read only the pinned ORFS final reports from this invocation's output tree.

ORFS 26Q2 scripts/report_metrics.tcl writes full-precision metrics to
6_report.json; rounded timing and area text remain required evidence.
Routed completion comes from the adapter's fresh final artifacts and exit
status, since upstream does not emit our synthetic completion marker.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad import metrics


def discover_report_texts(
    flow_result: Any, flow_workdir: str | os.PathLike[str] | None = None,
) -> dict[str, str | None]:
    # flow_workdir is retained for callers of the former generator helper.
    # Never fall back to the shared checkout, another variant, or another stage.
    provenance = flow_result.provenance
    output = provenance.get("output_dir")
    start_ns = provenance.get("start_ns")
    empty = {"timing_text": None, "area_text": None, "drc_text": None}
    if not output or not isinstance(start_ns, int):
        return empty
    root = Path(output).resolve()
    variant = provenance.get("variant", "default")
    if variant != "default":
        return empty
    suffix = Path(gcd.FIXED_PLATFORM) / gcd.FIXED_DESIGN / variant

    def read(relative: Path) -> str | None:
        path = root / relative
        try:
            if (path.is_symlink() or not path.is_file()
                    or root not in path.resolve().parents
                    or path.stat().st_mtime_ns < start_ns):
                return None
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    timing = read(Path("reports") / suffix / "6_finish.rpt")
    area = read(Path("logs") / suffix / "6_report.log")
    # Text reports round timing to two decimals, which can hide a negative
    # slack as -0.00. The final JSON carries the full measured precision.
    final_json = read(Path("logs") / suffix / "6_report.json")
    try:
        payload = json.loads(final_json) if final_json is not None else {}
        ws = payload["finish__timing__setup__ws"]
        tns = payload["finish__timing__setup__tns"]
        cell_area = payload["finish__design__instance__area__stdcell"]
        for value in (ws, tns, cell_area):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
                raise ValueError("non-finite final metric")
        timing = f"wns {min(0.0, ws)} ns\ntns {tns} ns\n" if timing else None
        area = f"Design area {cell_area} um^2\n" if area else None
    except (ValueError, TypeError, KeyError):
        timing = area = None
    drc_json = read(Path("logs") / suffix / "5_2_route.json")
    drc = None
    if drc_json is not None:
        # This key is also gated by the pinned GCD rules-base.json.
        try:
            count = json.loads(drc_json)["detailedroute__route__drc_errors"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("invalid DRC count")
            drc = f"drc_violations: {count}"
        except (ValueError, KeyError, TypeError):
            drc = "unparseable routing metrics"
    unconstrained = read(Path("reports") / suffix / "6_unconstrained.rpt")
    if timing is not None and unconstrained is not None:
        if re.search(r"^unconstrained_check_passed: 1$", unconstrained, re.MULTILINE):
            timing += "\nunconstrained_paths: 0\n"
        else:
            match = re.search(r"There (?:is|are) (\d+) unconstrained endpoints?", unconstrained)
            if match:
                timing += f"\nunconstrained_paths: {match.group(1)}\n"
    if timing is not None and flow_result.ok:
        timing += "\nrouted_completion: true\n"
    return {"timing_text": timing, "area_text": area, "drc_text": drc}


def parse_generated_reports(flow_result: Any) -> metrics.GcdMetrics:
    texts = discover_report_texts(flow_result)
    root = flow_result.provenance.get("output_dir", "<missing-output>")
    suffix = f"{gcd.FIXED_PLATFORM}/{gcd.FIXED_DESIGN}/default"
    return metrics.parse_flow_result(
        flow_result, **texts,
        timing_ref=f"{root}/logs/{suffix}/6_report.json",
        area_ref=f"{root}/logs/{suffix}/6_report.json",
        drc_ref=f"{root}/logs/{suffix}/5_2_route.json",
    )
