"""Episode action/execution budget enforcement (M0-05).

Shared, deterministic counters over the existing
:class:`silicon_env.types.Budget` contract. No I/O, no subprocess, no
threads; stdlib-only, Python >= 3.10.

Semantics (deterministic, checked BEFORE dispatch):

- Every agent action -- valid or invalid -- consumes exactly one step
  (``steps_used``) when recorded. Invalid actions therefore consume a
  step but never a tool call. Rationale: an agent that emits invalid
  actions forever must still terminate; not charging steps would allow
  infinite invalid-action loops outside the budget.
- Every dispatched tool attempt consumes one step *plus* one tool call,
  charged at reserve/dispatch time. Failed attempts (nonzero exit,
  timeout, launch failure) are charged identically to successes: the
  quota guards execution cost, not outcome. Callers must reserve first
  (``reserve_action`` / ``reserve_tool_call``) and only then launch.
- Wall time is measured with an injectable monotonic clock
  (``clock=time.monotonic`` by default; tests pass a fake clock).
- Exhaustion priority is deterministic when several limits trip at
  once: ``max_wallclock_s`` > ``max_steps`` > ``max_tool_calls``.
  ``exhausted_reason`` is one of ``"max_wallclock_s"``,
  ``"max_steps"``, ``"max_tool_calls"``, or ``None`` when live.
- Per-call deadlines never exceed the remaining wall time: use
  :meth:`BudgetTracker.effective_timeout` (or the timeout returned by
  :meth:`BudgetTracker.reserve_tool_call`) as the ``timeout_s`` passed
  to :meth:`silicon_env.runner.ToolRunner.run`. An exhausted episode
  must not launch additional tools -- reserve raises
  :class:`BudgetExhausted` instead of returning a timeout.

Non-goals: no token billing, no distributed quotas, no reward shaping,
no memory monitoring.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from silicon_env.types import Budget, ContractError

REASON_WALLCLOCK = "max_wallclock_s"
REASON_STEPS = "max_steps"
REASON_TOOL_CALLS = "max_tool_calls"

ClockFn = Callable[[], float]


class BudgetExhausted(ValueError):
    """Raised when a reserve/check finds the episode budget exhausted."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or f"episode budget exhausted: {reason}")


def _require_clock(clock: Any) -> ClockFn:
    if not callable(clock):
        raise ContractError(f"clock must be callable, got {type(clock).__name__}")
    return clock


def _require_nonnegative_timeout(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field_name} must be a number, got {type(value).__name__}")
    result = float(value)
    if not (result >= 0) or result != result or result in (float("inf"), float("-inf")):
        raise ContractError(f"{field_name} must be a finite number >= 0, got {value!r}")
    return result


def _require_positive_timeout(value: Any, *, field_name: str) -> float:
    result = _require_nonnegative_timeout(value, field_name=field_name)
    if result <= 0:
        raise ContractError(f"{field_name} must be > 0, got {value!r}")
    return result


@dataclass
class ToolAllowance:
    """Result of a successful tool-call reservation (before dispatch)."""

    effective_timeout_s: float
    steps_used: int
    tool_calls_used: int


class BudgetTracker:
    """Mutable episode counters bound to a frozen :class:`Budget`.

    :param budget: validated limits (``max_steps``, ``max_wallclock_s``,
        ``max_tool_calls``). The tracker holds a reference; ``Budget``
        itself is frozen so limits cannot drift mid-episode.
    :param clock: monotonic seconds source. Defaults to
        :func:`time.monotonic`. Tests inject a fake clock.
    """

    def __init__(self, budget: Budget, *, clock: ClockFn | None = None) -> None:
        if not isinstance(budget, Budget):
            raise ContractError("budget must be a Budget")
        budget.validate()
        self._budget = budget
        self._clock: ClockFn = _require_clock(clock) if clock is not None else time.monotonic
        start = float(self._clock())
        if start != start or start in (float("inf"), float("-inf")):
            raise ContractError(f"clock() must return a finite float, got {start!r}")
        self._start = start
        self._steps_used = 0
        self._tool_calls_used = 0

    # -- read-only views -------------------------------------------------

    @property
    def budget(self) -> Budget:
        return self._budget

    @property
    def steps_used(self) -> int:
        return self._steps_used

    @property
    def tool_calls_used(self) -> int:
        return self._tool_calls_used

    def elapsed_s(self) -> float:
        """Seconds since tracker creation per the injected clock (>= 0)."""
        now = float(self._clock())
        if now != now or now in (float("inf"), float("-inf")):
            raise ContractError(f"clock() must return a finite float, got {now!r}")
        return max(0.0, now - self._start)

    def remaining_steps(self) -> int:
        return max(0, self._budget.max_steps - self._steps_used)

    def remaining_tool_calls(self) -> int:
        return max(0, self._budget.max_tool_calls - self._tool_calls_used)

    def remaining_wallclock_s(self) -> float:
        return max(0.0, float(self._budget.max_wallclock_s) - self.elapsed_s())

    def exhausted_reason(self) -> str | None:
        """Deterministic first-tripped limit, or ``None`` when live.

        Priority: wallclock, then steps, then tool calls.
        """
        if self.remaining_wallclock_s() <= 0:
            return REASON_WALLCLOCK
        if self._steps_used >= self._budget.max_steps:
            return REASON_STEPS
        if self._tool_calls_used >= self._budget.max_tool_calls:
            return REASON_TOOL_CALLS
        return None

    @property
    def exhausted(self) -> bool:
        return self.exhausted_reason() is not None

    # -- reservation (check BEFORE dispatch; no mutation on refusal) -----

    def check_action(self) -> None:
        """Raise :class:`BudgetExhausted` if no action allowance remains.

        Applies equally to valid and invalid actions: both consume a
        step, so both require a remaining step and remaining wall time.
        Does not mutate counters.
        """
        reason = self.exhausted_reason()
        if reason == REASON_WALLCLOCK:
            raise BudgetExhausted(reason, "episode wallclock budget exhausted")
        if self._steps_used >= self._budget.max_steps:
            raise BudgetExhausted(REASON_STEPS, "episode step budget exhausted")
        if self._tool_calls_used >= self._budget.max_tool_calls and self.remaining_steps() <= 0:
            # Steps already cover this, but keep the priority explicit.
            raise BudgetExhausted(REASON_STEPS, "episode step budget exhausted")
        # Pure non-tool actions are not gated on the tool-call quota:
        # only steps + wallclock apply here.

    def check_tool_call(self, requested_timeout_s: float | None = None) -> float:
        """Return the clamped per-call deadline without mutating counters.

        Raises :class:`BudgetExhausted` if the episode cannot launch
        another tool (wallclock, steps, or tool calls exhausted).
        The returned timeout never exceeds the remaining wall time.
        """
        if requested_timeout_s is not None:
            _require_positive_timeout(requested_timeout_s, field_name="requested_timeout_s")
        reason = self.exhausted_reason()
        if reason is not None:
            raise BudgetExhausted(reason, f"episode budget exhausted: {reason}")
        remaining = self.remaining_wallclock_s()
        if remaining <= 0:
            raise BudgetExhausted(REASON_WALLCLOCK, "episode wallclock budget exhausted")
        if requested_timeout_s is None:
            return remaining
        return min(float(requested_timeout_s), remaining)

    def effective_timeout(self, requested_timeout_s: float | None = None) -> float:
        """Clamp a requested per-call deadline to the remaining wall time.

        Same as :meth:`check_tool_call` but named for the call site that
        forwards the result to ``ToolRunner.run(timeout_s=...)``.
        """
        return self.check_tool_call(requested_timeout_s)

    def reserve_action(self, *, valid: bool = True) -> None:
        """Reserve allowance for one action BEFORE dispatch/validation.

        Call this before validating or executing the action so that
        failed/invalid attempts cannot bypass the quota. Does not
        mutate counters -- follow with :meth:`consume_action`.
        ``valid`` is accepted for call-site clarity but plays no role:
        invalid actions require the same allowance.
        """
        _ = valid
        self.check_action()

    def reserve_tool_call(self, requested_timeout_s: float | None = None) -> ToolAllowance:
        """Reserve allowance for one tool launch BEFORE dispatch.

        Returns the clamped :class:`ToolAllowance` (effective timeout
        never exceeds remaining wall time). Does not mutate counters --
        call :meth:`consume_tool_call` immediately before dispatch so
        failed attempts and timeouts are charged consistently.
        Raises :class:`BudgetExhausted` without mutating when exhausted.
        """
        effective = self.check_tool_call(requested_timeout_s)
        return ToolAllowance(
            effective_timeout_s=effective,
            steps_used=self._steps_used,
            tool_calls_used=self._tool_calls_used,
        )

    # -- consumption (charge AFTER reserve, including failures) -----------

    def consume_action(self, *, valid: bool = True) -> str | None:
        """Charge one step; invalid actions consume a step, never a tool call.

        Returns the post-charge :meth:`exhausted_reason` (``None`` while
        live) so callers can persist the snapshot with its terminal
        reason. Raises :class:`BudgetExhausted` without mutating if the
        episode was already exhausted before this action.
        """
        _ = valid  # invalid actions are charged identically (one step).
        self.check_action()
        self._steps_used += 1
        return self.exhausted_reason()

    def consume_tool_call(self, *, valid: bool = True) -> str | None:
        """Charge one step plus one tool call for a dispatched attempt.

        Call immediately before dispatch, exactly once per attempt regardless of
        outcome (success, tool failure, timeout, or launch failure) so
        failed attempts are charged consistently. Returns the
        post-charge exhausted reason. Raises :class:`BudgetExhausted`
        without mutating if exhausted before dispatch.
        """
        _ = valid  # a dispatched tool call always implies a valid dispatch slot.
        self.check_tool_call()
        self._steps_used += 1
        self._tool_calls_used += 1
        return self.exhausted_reason()

    # -- persistence ------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return a strict-JSON-serializable snapshot of limits + usage."""
        elapsed = self.elapsed_s()
        remaining_wc = max(0.0, float(self._budget.max_wallclock_s) - elapsed)
        reason = self.exhausted_reason()
        return {
            "max_steps": self._budget.max_steps,
            "max_tool_calls": self._budget.max_tool_calls,
            "max_wallclock_s": float(self._budget.max_wallclock_s),
            "steps_used": self._steps_used,
            "tool_calls_used": self._tool_calls_used,
            "elapsed_s": float(elapsed),
            "remaining_steps": self.remaining_steps(),
            "remaining_tool_calls": self.remaining_tool_calls(),
            "remaining_wallclock_s": float(remaining_wc),
            "exhausted": reason is not None,
            "exhausted_reason": reason,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.snapshot()

    @classmethod
    def restore(
        cls,
        budget: Budget,
        snapshot: Mapping[str, Any],
        *,
        clock: ClockFn | None = None,
        elapsed_s: float | None = None,
    ) -> BudgetTracker:
        """Rebuild a tracker from :meth:`snapshot` counters.

        Wall time cannot be restored exactly from counters alone (the
        clock keeps moving), so the caller passes either the snapshot's
        ``elapsed_s`` (a fake/test clock is then advanced accordingly)
        or relies on a fresh start. By default the restored tracker
        backdates ``start = clock() - snapshot.elapsed_s`` so
        ``elapsed_s()`` resumes where the snapshot left off. Pass
        ``elapsed_s=0.0`` explicitly to restart the wallclock instead.
        """
        if not isinstance(snapshot, Mapping):
            raise ContractError("snapshot must be an object")
        for key in ("steps_used", "tool_calls_used"):
            value = snapshot.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"snapshot[{key!r}] must be an int >= 0")
        resumed_elapsed: float
        if elapsed_s is None:
            raw = snapshot.get("elapsed_s", 0.0)
            resumed_elapsed = _require_nonnegative_timeout(raw, field_name="snapshot[elapsed_s]")
        else:
            resumed_elapsed = _require_nonnegative_timeout(elapsed_s, field_name="elapsed_s")
        tracker = cls(budget, clock=clock)
        tracker._steps_used = int(snapshot["steps_used"])
        tracker._tool_calls_used = int(snapshot["tool_calls_used"])
        if tracker._steps_used > budget.max_steps or tracker._tool_calls_used > (
            budget.max_tool_calls
        ):
            raise ContractError("snapshot counters exceed budget limits")
        # Backdate start so elapsed resumes deterministically.
        tracker._start = float(tracker._clock()) - resumed_elapsed
        return tracker


__all__ = [
    "REASON_STEPS",
    "REASON_TOOL_CALLS",
    "REASON_WALLCLOCK",
    "BudgetExhausted",
    "BudgetTracker",
    "ToolAllowance",
]
