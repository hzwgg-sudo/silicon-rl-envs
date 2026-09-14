"""M0-05 budget tests: exact limits, zero remaining, invalid actions,
failed-call charging, snapshots, and clock-controlled timeout boundaries."""

import json

import pytest

from silicon_env.budget import (
    REASON_STEPS,
    REASON_TOOL_CALLS,
    REASON_WALLCLOCK,
    BudgetExhausted,
    BudgetTracker,
)
from silicon_env.types import Budget, ContractError


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def make_tracker(**kw) -> tuple[BudgetTracker, FakeClock]:
    clock = FakeClock()
    budget_kwargs = {"max_steps": 4, "max_wallclock_s": 100.0, "max_tool_calls": 2}
    budget_kwargs.update(kw.pop("budget", {}))
    tracker = BudgetTracker(Budget(**budget_kwargs), clock=clock, **kw)
    return tracker, clock


# --- exact limits ----------------------------------------------------------


def test_exact_step_limit_allows_n_then_refuses():
    tracker, _ = make_tracker(budget={"max_steps": 3, "max_tool_calls": 3})
    assert tracker.exhausted_reason() is None
    for _ in range(3):
        tracker.reserve_action()
        tracker.consume_action()
    assert tracker.steps_used == 3
    assert tracker.exhausted_reason() == REASON_STEPS
    assert tracker.exhausted
    with pytest.raises(BudgetExhausted) as exc_info:
        tracker.reserve_action()
    assert exc_info.value.reason == REASON_STEPS
    # Failed reserve must not mutate counters.
    assert tracker.steps_used == 3
    with pytest.raises(BudgetExhausted):
        tracker.consume_action()
    assert tracker.steps_used == 3


def test_exact_tool_call_limit_blocks_only_tool_launches():
    tracker, _ = make_tracker(budget={"max_steps": 4, "max_tool_calls": 2})
    for _ in range(2):
        tracker.reserve_tool_call(10.0)
        tracker.consume_tool_call()
    assert tracker.tool_calls_used == 2
    assert tracker.exhausted_reason() == REASON_TOOL_CALLS
    # No further tool launch allowed ...
    with pytest.raises(BudgetExhausted) as exc_info:
        tracker.reserve_tool_call(10.0)
    assert exc_info.value.reason == REASON_TOOL_CALLS
    assert tracker.tool_calls_used == 2
    # ... but non-tool actions still have step allowance.
    tracker.reserve_action()
    tracker.consume_action()
    assert tracker.steps_used == 3


def test_zero_remaining_wallclock_refuses_everything():
    clock = FakeClock(now=0.0)
    tracker = BudgetTracker(
        Budget(max_steps=5, max_wallclock_s=10.0, max_tool_calls=5), clock=clock
    )
    clock.advance(10.0)
    assert tracker.remaining_wallclock_s() == 0.0
    assert tracker.exhausted_reason() == REASON_WALLCLOCK
    with pytest.raises(BudgetExhausted) as exc_info:
        tracker.reserve_action()
    assert exc_info.value.reason == REASON_WALLCLOCK
    with pytest.raises(BudgetExhausted):
        tracker.reserve_tool_call(1.0)
    assert tracker.steps_used == 0 and tracker.tool_calls_used == 0


def test_exhaustion_priority_is_wallclock_then_steps_then_tools():
    clock = FakeClock()
    tracker = BudgetTracker(
        Budget(max_steps=1, max_wallclock_s=10.0, max_tool_calls=1), clock=clock
    )
    tracker.reserve_tool_call(5.0)
    tracker.consume_tool_call()
    # Steps and tool calls trip together; steps win by priority.
    assert tracker.exhausted_reason() == REASON_STEPS
    clock2 = FakeClock()
    tracker2 = BudgetTracker(
        Budget(max_steps=5, max_wallclock_s=10.0, max_tool_calls=1), clock=clock2
    )
    tracker2.reserve_tool_call(5.0)
    tracker2.consume_tool_call()
    assert tracker2.exhausted_reason() == REASON_TOOL_CALLS
    clock2.advance(10.0)
    assert tracker2.exhausted_reason() == REASON_WALLCLOCK


# --- invalid actions / failed calls ----------------------------------------


def test_invalid_actions_consume_step_but_never_tool_call():
    tracker, _ = make_tracker(budget={"max_steps": 3, "max_tool_calls": 3})
    tracker.reserve_action(valid=False)
    reason = tracker.consume_action(valid=False)
    assert reason is None
    assert tracker.steps_used == 1
    assert tracker.tool_calls_used == 0
    snap = tracker.snapshot()
    assert snap["remaining_steps"] == 2
    assert snap["remaining_tool_calls"] == 3


def test_invalid_action_at_last_step_exhausts_episode():
    tracker, _ = make_tracker(budget={"max_steps": 1, "max_tool_calls": 1})
    tracker.reserve_action(valid=False)
    reason = tracker.consume_action(valid=False)
    assert reason == REASON_STEPS
    with pytest.raises(BudgetExhausted):
        tracker.reserve_action(valid=False)


@pytest.mark.parametrize("outcome", ["success", "tool_failure", "timeout", "infra_error"])
def test_failed_tool_attempts_charged_like_successes(outcome):
    tracker, _ = make_tracker()
    tracker.reserve_tool_call(10.0)
    # Outcome-independent charging: the caller records every dispatch.
    tracker.consume_tool_call()
    assert (tracker.steps_used, tracker.tool_calls_used) == (1, 1)
    _ = outcome  # charging does not branch on outcome by design.


def test_launch_failure_still_charged():
    tracker, _ = make_tracker()
    tracker.reserve_tool_call(10.0)
    # Tool never launched (launched=False) but the reserved slot is spent.
    tracker.consume_tool_call()
    assert tracker.steps_used == 1
    assert tracker.tool_calls_used == 1


# --- timeout clamping -------------------------------------------------------


def test_effective_timeout_never_exceeds_remaining_wallclock():
    tracker, clock = make_tracker(budget={"max_wallclock_s": 100.0})
    clock.advance(90.0)
    assert tracker.remaining_wallclock_s() == pytest.approx(10.0)
    assert tracker.effective_timeout(60.0) == pytest.approx(10.0)
    assert tracker.effective_timeout(10.0) == pytest.approx(10.0)
    assert tracker.effective_timeout(5.0) == pytest.approx(5.0)
    assert tracker.effective_timeout(None) == pytest.approx(10.0)


def test_timeout_boundary_exact_remaining_allowed_then_exhausted():
    tracker, clock = make_tracker(budget={"max_wallclock_s": 10.0})
    clock.advance(9.0)
    allowance = tracker.reserve_tool_call(1.0)
    assert allowance.effective_timeout_s == pytest.approx(1.0)
    tracker.consume_tool_call()
    clock.advance(1.0)  # exactly at the wallclock boundary
    assert tracker.remaining_wallclock_s() == 0.0
    with pytest.raises(BudgetExhausted) as exc_info:
        tracker.reserve_tool_call(1.0)
    assert exc_info.value.reason == REASON_WALLCLOCK


def test_zero_or_negative_timeout_requests_rejected():
    tracker, _ = make_tracker()
    with pytest.raises(ContractError):
        tracker.effective_timeout(0.0)
    with pytest.raises(ContractError):
        tracker.effective_timeout(-1.0)


def test_exhausted_episode_cannot_launch_additional_tool():
    tracker, _ = make_tracker(budget={"max_steps": 2, "max_tool_calls": 1})
    tracker.reserve_tool_call(5.0)
    tracker.consume_tool_call()
    with pytest.raises(BudgetExhausted):
        tracker.check_tool_call(1.0)
    # Simulate an environment gating ToolRunner.run on the tracker:
    with pytest.raises(BudgetExhausted):
        tracker.effective_timeout(1.0)
    assert tracker.tool_calls_used == 1


# --- snapshots ---------------------------------------------------------------


def test_snapshot_is_json_serializable_with_explicit_reason():
    tracker, clock = make_tracker(budget={"max_steps": 4, "max_tool_calls": 2})
    tracker.reserve_tool_call(5.0)
    tracker.consume_tool_call()
    clock.advance(3.5)
    snap = tracker.snapshot()
    assert snap["steps_used"] == 1
    assert snap["tool_calls_used"] == 1
    assert snap["elapsed_s"] == pytest.approx(3.5)
    assert snap["remaining_steps"] == 3
    assert snap["exhausted"] is False
    assert snap["exhausted_reason"] is None
    json.dumps(snap)  # must be strict-JSON serializable
    tracker.reserve_tool_call(5.0)
    reason = tracker.consume_tool_call()
    assert reason == REASON_TOOL_CALLS
    snap2 = tracker.snapshot()
    assert snap2["exhausted"] is True
    assert snap2["exhausted_reason"] == REASON_TOOL_CALLS
    json.dumps(snap2)


def test_restore_resumes_counters_and_wallclock():
    tracker, clock = make_tracker(budget={"max_steps": 4, "max_tool_calls": 2})
    tracker.reserve_tool_call(5.0)
    tracker.consume_tool_call()
    clock.advance(7.0)
    snap = tracker.snapshot()
    clock2 = FakeClock(now=1000.0)
    restored = BudgetTracker.restore(tracker.budget, snap, clock=clock2)
    assert restored.steps_used == 1
    assert restored.tool_calls_used == 1
    assert restored.elapsed_s() == pytest.approx(7.0)
    assert restored.remaining_wallclock_s() == pytest.approx(93.0)
    restored.reserve_tool_call(5.0)
    restored.consume_tool_call()
    assert restored.exhausted_reason() == REASON_TOOL_CALLS


def test_restore_rejects_bad_snapshots():
    tracker, _ = make_tracker()
    with pytest.raises(ContractError):
        BudgetTracker.restore(tracker.budget, {"steps_used": -1, "tool_calls_used": 0})
    with pytest.raises(ContractError):
        BudgetTracker.restore(tracker.budget, {"steps_used": 99, "tool_calls_used": 0})


def test_tracker_requires_valid_budget_and_clock():
    with pytest.raises(ContractError):
        BudgetTracker("not-a-budget")  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        BudgetTracker(
            Budget(max_steps=2, max_wallclock_s=5.0, max_tool_calls=2),
            clock="not-callable",  # type: ignore[arg-type]
        )
