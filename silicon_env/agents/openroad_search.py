"""Deterministic bounded-search GCD baseline agent (M1-09).

Tries a fixed-order grid of at most four legal config pairs (stock plus
three variants spanning the supported bounds), running each through the
``openroad-flow`` tool and tracking the best feasible observed
candidate under a stable tie-breaker:

1. smallest observed area (candidates with no parsed area sort last),
2. lowest ``PLACE_DENSITY``,
3. lowest ``CORE_UTILIZATION``,
4. earliest grid index.

The winner is written back to ``candidate.json`` and submitted, so the
trusted clean-room evaluator produces the final grade. When no
candidate is feasible (or the budget runs out), the agent submits the
stock candidate (or the best so far) and reports no improvement
honestly instead of fabricating one.

Stdlib-only, Python >= 3.10. No LLM, no RL, no training, no randomness:
given the same seed and the same tool metrics, the action sequence and
selection are identical. Artifact saving goes through the existing
environment trace (no custom paths outside ``work_root``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from silicon_env.environments.openroad import config as gcd
from silicon_env.environments.openroad import flow as gcd_flow
from silicon_env.task import Action
from silicon_env.types import StepStatus

#: Declared caps: at most four grid candidates, hence at most four flow calls.
SEARCH_MAX_CANDIDATES = 4
SEARCH_MAX_TOOL_CALLS = 4

#: Score of the do-nothing stock reference (see the grader formula).
STOCK_REFERENCE_SCORE = 0.5

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_AREA_RE = re.compile(
    rf"\b(?:cell_area|design area|cell area|area_um2|area)\b\s*[:=]\s*({_NUMBER})"
    r"\s*(um\^2|u\^2|um2)?\b",
    re.IGNORECASE,
)
_WNS_RE = re.compile(rf"\bwns\b\s*[:=]?\s*({_NUMBER})\s*(ns|ps|us|ms|s)?\b", re.IGNORECASE)
_TNS_RE = re.compile(rf"\btns\b\s*[:=]?\s*({_NUMBER})\s*(ns|ps|us|ms|s)?\b", re.IGNORECASE)
_COMPLETION_RE = re.compile(
    r"routed_completion\s*[:=]\s*(true|1|ok|completed?|yes|pass)"
    r"|routing\s+completed|flow\s+completed",
    re.IGNORECASE,
)

_TIME_TO_NS = {"ns": 1.0, "ps": 1e-3, "us": 1e3, "ms": 1e6, "s": 1e9}


def search_candidates() -> tuple[dict[str, float], ...]:
    """Return the fixed-order grid of at most four legal config pairs.

    Stock first, then three variants spanning the supported bounds
    deterministically (no randomness, no seed dependence).
    """
    grid = (
        {"PLACE_DENSITY": float(gcd.PLACE_DENSITY_STOCK),
         "CORE_UTILIZATION": float(gcd.CORE_UTILIZATION_STOCK)},
        {"PLACE_DENSITY": float(gcd.PLACE_DENSITY_MIN),
         "CORE_UTILIZATION": float(gcd.CORE_UTILIZATION_MIN)},
        {"PLACE_DENSITY": float(gcd.PLACE_DENSITY_MAX),
         "CORE_UTILIZATION": float(gcd.CORE_UTILIZATION_MAX)},
        {"PLACE_DENSITY": 0.50, "CORE_UTILIZATION": float(gcd.CORE_UTILIZATION_STOCK)},
    )
    for entry in grid:
        gcd.validate_candidate_config(entry)
    return grid


def parse_observed_metrics(text: Any) -> dict[str, float | bool | None]:
    """Extract best-effort metrics from a sanitized observation tail.

    Never raises on content: unparseable text yields ``None`` fields and
    ``feasible_text=False``. Feasibility itself is decided by the flow
    :class:`StepStatus`, not by this text; the parsed area only orders
    feasible candidates.
    """
    if not isinstance(text, str):
        text = str(text)
    area: float | None = None
    match = _AREA_RE.search(text)
    if match:
        try:
            area = float(match.group(1))
        except ValueError:
            area = None
        if area is not None and not math.isfinite(area):
            area = None
    wns: float | None = None
    match = _WNS_RE.search(text)
    if match:
        try:
            raw = float(match.group(1))
        except ValueError:
            raw = None  # type: ignore[assignment]
        if raw is not None and math.isfinite(raw):
            unit = (match.group(2) or "ns").lower()
            wns = raw * _TIME_TO_NS.get(unit, 1.0)
    tns: float | None = None
    match = _TNS_RE.search(text)
    if match:
        try:
            raw = float(match.group(1))
        except ValueError:
            raw = None  # type: ignore[assignment]
        if raw is not None and math.isfinite(raw):
            unit = (match.group(2) or "ns").lower()
            tns = raw * _TIME_TO_NS.get(unit, 1.0)
    return {
        "area_um2": area,
        "wns_ns": wns,
        "tns_ns": tns,
        "completion_text": bool(_COMPLETION_RE.search(text)),
    }


@dataclass
class CandidateRecord:
    """One evaluated grid entry."""

    index: int
    candidate: dict[str, float]
    feasible: bool = False
    area_um2: float | None = None
    message: str = ""

    def rank_key(self) -> tuple[float, float, float, int]:
        """Stable ordering: area, density, utilization, grid index."""
        area = self.area_um2 if self.area_um2 is not None else math.inf
        return (
            float(area),
            float(self.candidate.get("PLACE_DENSITY", math.inf)),
            float(self.candidate.get("CORE_UTILIZATION", math.inf)),
            int(self.index),
        )


@dataclass
class SearchResult:
    """Outcome of one bounded-search episode."""

    actions: tuple[str, ...] = ()
    records: tuple[CandidateRecord, ...] = ()
    best_index: int = 0
    submitted_candidate: dict[str, float] = field(default_factory=dict)
    submitted_text: str = ""
    grade: Any = None
    improved: bool = False
    budget_limited: bool = False
    terminated_early: bool = False
    seed: int = 0

    @property
    def score(self) -> float:
        """Trusted final score (0.0 when no grade was produced)."""
        if self.grade is None:
            return 0.0
        return float(getattr(self.grade, "score", 0.0))


def _snapshot(env: Any) -> dict[str, Any]:
    tracker = getattr(env, "budget_tracker", None)
    if tracker is None:
        return {}
    try:
        snapshot = tracker.snapshot()
    except Exception:
        return {}
    return dict(snapshot) if isinstance(snapshot, dict) else {}


def _remaining(env: Any) -> tuple[int, int]:
    snapshot = _snapshot(env)
    try:
        steps = int(snapshot.get("remaining_steps", 0))
    except (TypeError, ValueError):
        steps = 0
    try:
        tools = int(snapshot.get("remaining_tool_calls", 0))
    except (TypeError, ValueError):
        tools = 0
    return (max(0, steps), max(0, tools))


def _usage(env: Any) -> tuple[int, int]:
    snapshot = _snapshot(env)
    try:
        return (int(snapshot.get("steps_used", 0)), int(snapshot.get("tool_calls_used", 0)))
    except (TypeError, ValueError):
        return (0, 0)


def _workspace_text(env: Any) -> str:
    workspace = getattr(env, "workspace", None)
    if workspace is None:
        return ""
    try:
        return workspace.read_text(gcd.CANDIDATE_RELPATH)
    except Exception:
        return ""


def _select_best(records: list[CandidateRecord]) -> int:
    feasible = [rec for rec in records if rec.feasible]
    if not feasible:
        return 0
    return min(feasible, key=lambda rec: rec.rank_key()).index


def run_search_agent(
    env: Any,
    task: Any,
    seed: int,
    *,
    candidates: tuple[Mapping[str, Any], ...] | None = None,
) -> SearchResult:
    """Run the bounded search: evaluate the grid, submit the winner.

    :param env: GCD environment exposing ``reset`` / ``step`` / ``submit``.
    :param task: task spec to reset with.
    :param seed: episode seed (recorded; ordering is fixed regardless).
    :param candidates: optional override grid (at most four entries, each
        a legal candidate mapping). Defaults to :func:`search_candidates`.
    :returns: a :class:`SearchResult` with the action log, per-candidate
        records, the winner, and the trusted final grade.
    """
    grid = search_candidates() if candidates is None else tuple(candidates)
    if len(grid) > SEARCH_MAX_CANDIDATES:
        raise ValueError(
            f"candidate grid holds {len(grid)} entries, cap is {SEARCH_MAX_CANDIDATES}"
        )
    normalized: list[dict[str, float]] = []
    for entry in grid:
        if not isinstance(entry, Mapping):
            raise ValueError(f"candidate grid entries must be mappings, got {type(entry).__name__}")
        normalized.append(gcd.candidate_with_defaults(dict(entry)))

    actions: list[str] = []
    records: list[CandidateRecord] = []
    budget_limited = False
    terminated_early = False
    tool_calls = 0

    env.reset(task, seed=seed)
    for index, candidate in enumerate(normalized):
        if bool(getattr(env, "done", False)):
            terminated_early = True
            break
        remaining_steps, remaining_tools = _remaining(env)
        # Each probe costs write + run (2 steps, 1 tool call); the final
        # write-back + submit needs 2 more steps. Stop before exceeding.
        if remaining_steps < 4 or remaining_tools < 1 or tool_calls >= SEARCH_MAX_TOOL_CALLS:
            budget_limited = True
            break
        write = env.step(
            Action(
                schema_version=1,
                action_type="write_file",
                params={
                    "path": gcd.CANDIDATE_RELPATH,
                    "content": gcd.dumps_candidate_json(candidate),
                },
            )
        )
        actions.append(f"write_file:{index}")
        if write.done:
            terminated_early = True
            break
        run = env.step(
            Action(
                schema_version=1,
                action_type="run_tool",
                params={"tool": gcd_flow.FLOW_TOOL_NAME},
            )
        )
        actions.append(f"run_tool:{index}")
        tool_calls += 1
        text = "\n".join(
            [
                run.observation.stdout_tail,
                run.observation.stderr_tail,
                run.message,
            ]
        )
        parsed = parse_observed_metrics(text)
        feasible = run.status == StepStatus.SUCCESS
        records.append(
            CandidateRecord(
                index=index,
                candidate=dict(candidate),
                feasible=bool(feasible),
                area_um2=parsed["area_um2"],  # type: ignore[arg-type]
                message=run.message,
            )
        )
        if run.done:
            # TIMEOUT / INFRA_ERROR terminates the episode: no new work.
            terminated_early = True
            break

    best_index = _select_best(records) if records else 0
    winner = dict(normalized[best_index]) if normalized else gcd.stock_candidate_config()

    grade = None
    submitted_text = ""
    if not bool(getattr(env, "done", False)):
        remaining_steps, _ = _remaining(env)
        if remaining_steps < 1:
            budget_limited = True
        else:
            current = _workspace_text(env)
            want = gcd.dumps_candidate_json(winner) + "\n"
            if current != want:
                if remaining_steps < 2:
                    # No room to rewrite honestly: submit in place.
                    budget_limited = True
                else:
                    rewrite = env.step(
                        Action(
                            schema_version=1,
                            action_type="write_file",
                            params={"path": gcd.CANDIDATE_RELPATH, "content": want},
                        )
                    )
                    actions.append(f"write_file:best-{best_index}")
                    if rewrite.done:
                        terminated_early = True
            if not bool(getattr(env, "done", False)) and _remaining(env)[0] >= 1:
                grade = env.submit()
                actions.append("submit")
                submitted_text = want if grade is not None else ""
            else:
                budget_limited = True
    else:
        terminated_early = True

    if grade is not None:
        improved = bool(getattr(grade, "passed", False)) and (
            float(getattr(grade, "score", 0.0)) > STOCK_REFERENCE_SCORE
        )
    else:
        improved = False

    _, tools_used = _usage(env)
    assert tool_calls <= SEARCH_MAX_TOOL_CALLS, f"search exceeded tool cap: {tool_calls}"
    assert tools_used <= SEARCH_MAX_TOOL_CALLS, f"env tool calls exceed cap: {tools_used}"

    return SearchResult(
        actions=tuple(actions),
        records=tuple(records),
        best_index=best_index,
        submitted_candidate=dict(winner),
        submitted_text=submitted_text,
        grade=grade,
        improved=bool(improved),
        budget_limited=budget_limited,
        terminated_early=terminated_early,
        seed=seed,
    )


__all__ = [
    "SEARCH_MAX_CANDIDATES",
    "SEARCH_MAX_TOOL_CALLS",
    "STOCK_REFERENCE_SCORE",
    "CandidateRecord",
    "SearchResult",
    "parse_observed_metrics",
    "run_search_agent",
    "search_candidates",
]
