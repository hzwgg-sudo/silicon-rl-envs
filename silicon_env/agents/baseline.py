"""Deterministic no-op GCD baseline agent (M1-09).

Submits the stock candidate without edits (plus at most one inspect
read), establishing task usability and the do-nothing reference score
(``0.5`` when the trusted baseline matches the stock candidate).

Stdlib-only, Python >= 3.10. No LLM, no RL, no training, no EDA tools.
The agent only drives the public observation/action interface
(``reset`` / ``step`` / ``submit``) and never exceeds its declared call
cap. Artifact saving goes through the existing environment trace (no
custom paths outside ``work_root``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from silicon_env.environments.openroad import config as gcd
from silicon_env.task import Action

#: Declared call cap: at most one inspect read plus the final submit.
NOOP_MAX_ACTIONS = 2


@dataclass(frozen=True)
class NoopResult:
    """Outcome of one no-op baseline episode."""

    actions: tuple[str, ...] = ()
    steps_used: int = 0
    tool_calls_used: int = 0
    candidate_text: str = ""
    grade: Any = None
    seed: int = 0

    @property
    def score(self) -> float:
        """Trusted final score (0.0 when no grade was produced)."""
        grade = self.grade
        if grade is None:
            return 0.0
        return float(getattr(grade, "score", 0.0))


def _remaining_steps(env: Any) -> int:
    tracker = getattr(env, "budget_tracker", None)
    if tracker is None:
        return 0
    try:
        snapshot = tracker.snapshot()
    except Exception:
        return 0
    try:
        return int(snapshot.get("remaining_steps", 0))
    except (TypeError, ValueError):
        return 0


def _usage(env: Any) -> tuple[int, int]:
    tracker = getattr(env, "budget_tracker", None)
    if tracker is None:
        return (0, 0)
    try:
        snapshot = tracker.snapshot()
        return (int(snapshot.get("steps_used", 0)), int(snapshot.get("tool_calls_used", 0)))
    except Exception:
        return (0, 0)


def _read_action() -> Action:
    return Action(
        schema_version=1,
        action_type="read_file",
        params={"path": gcd.CANDIDATE_RELPATH},
    )


def run_noop_agent(env: Any, task: Any, seed: int, *, inspect: bool = True) -> NoopResult:
    """Run the no-op baseline: reset, optionally inspect, submit stock.

    :param env: GCD environment exposing ``reset`` / ``step`` / ``submit``.
    :param task: task spec to reset with.
    :param seed: episode seed (recorded; the action sequence is fixed).
    :param inspect: when True (default), perform one bounded
        ``read_file`` of the stock candidate before submitting, budget
        permitting. The workspace is never edited and no tool ever runs.
    :returns: a :class:`NoopResult` with the action log and trusted grade.
    """
    actions: list[str] = []
    env.reset(task, seed=seed)
    if inspect and _remaining_steps(env) >= 2 and not bool(getattr(env, "done", False)):
        env.step(_read_action())
        actions.append("read_file:candidate.json")
    candidate_text = ""
    workspace = getattr(env, "workspace", None)
    if workspace is not None:
        try:
            candidate_text = workspace.read_text(gcd.CANDIDATE_RELPATH)
        except Exception:
            candidate_text = ""
    grade = None
    if not bool(getattr(env, "done", False)):
        if _remaining_steps(env) >= 1:
            grade = env.submit()
            actions.append("submit")
    steps_used, tool_calls_used = _usage(env)
    assert len(actions) <= NOOP_MAX_ACTIONS, f"noop exceeded call cap: {actions}"
    return NoopResult(
        actions=tuple(actions),
        steps_used=steps_used,
        tool_calls_used=tool_calls_used,
        candidate_text=candidate_text,
        grade=grade,
        seed=seed,
    )


__all__ = ["NOOP_MAX_ACTIONS", "NoopResult", "run_noop_agent"]
