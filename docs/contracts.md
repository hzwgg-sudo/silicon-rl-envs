# Contracts (M0-02)

Versioned, strict, JSON-serializable contracts for tasks, actions,
observations, and grading results. Runtime is stdlib-only
(`dataclasses` + `json` + `enum`); validation is pure and side-effect
free -- an invalid schema raises `ContractError` before any workspace
directory or tool process could be created.

Modules:

- `silicon_env.types`: `ContractError`, `StepStatus`, `GradeStatus`,
  `Metric`, `Provenance`, `Budget`, `GraderConfig`, strict JSON helpers.
- `silicon_env.task`: `TaskSpec`, `Action`, `Observation`.
- `silicon_env.grader`: `StepResult`, `GradeResult`.

Every contract carries `schema_version` (currently `1`). Unknown versions,
missing required fields, unknown fields, non-finite numbers (`NaN`,
`Infinity`), and conflicting options are all rejected with
`ContractError`.

## Status vocabulary

Step outcomes (`StepStatus`): `success`, `invalid_submission`,
`tool_failure`, `timeout`, `infra_error`.

Episode outcomes (`GradeStatus`): `pass`, `fail`, `invalid_submission`,
`tool_failure`, `timeout`, `infra_error`. `passed` is `true` exactly when
status is `pass`.

Rewards/scores must be finite floats. Every metric carries an explicit
non-empty `unit` (e.g. `"um^2"`, `"ns"`, `"W"`). Every result preserves
`status`, metric `units`, and `provenance` through JSON round-trips.

## Validation rules (summary)

- `task_id` is a lowercase slug (`[a-z0-9][a-z0-9-_]*`);
  `task_version` is `MAJOR.MINOR.PATCH`.
- `seed` satisfies `0 <= seed < 2**63`.
- `budgets` values are positive and finite; `max_tool_calls` must not
  exceed `max_steps` (at most one tool call per step).
- Budget enforcement (`silicon_env.budget.BudgetTracker`, M0-05): every
  action -- valid or invalid -- consumes one step; invalid actions never
  consume a tool call. Every dispatched tool attempt (success or
  failure, including launch failure) consumes one step plus one tool
  call, reserved *before* dispatch. Allowances are checked before
  dispatch; an exhausted episode must not launch additional tools and
  per-call deadlines never exceed the remaining wall time. Exhaustion
  priority: `max_wallclock_s`, then `max_steps`, then `max_tool_calls`.
- `grader.timeout_s` must fit inside `budgets.max_wallclock_s`.
- `read_only` tasks must not allow `write_file`/`run_tool` and must not
  declare `allowed_edit_paths`. `submit` is still allowed (it does not
  mutate the workspace) and remains required.
- `Action.params` and `GradeResult.details` must be strict JSON values
  with finite floats only.
- `StepResult` terminal statuses (`timeout`, `infra_error`) require
  `done=true`; `observation.step_index` must match `step_index`.
- `GradeResult.passed` must agree with `status`; `provenance.task_id` /
  `task_version` must match the result when provenance is present.

## JSON examples

### TaskSpec

```json
{
  "allowed_actions": ["read_file", "write_file", "run_tool", "submit"],
  "allowed_edit_paths": ["design/*.sdc", "src/*.v"],
  "budgets": {"max_steps": 10, "max_tool_calls": 8, "max_wallclock_s": 600.0},
  "grader": {
    "grader_id": "rule-grader",
    "grader_version": "0.1.0",
    "required_metrics": ["area_um2"],
    "timeout_s": 60.0
  },
  "read_only": false,
  "schema_version": 1,
  "seed": 42,
  "source_ref": "tasks/gcd@v0.1.0",
  "task_id": "gcd-flow",
  "task_version": "0.1.0",
  "toolchain_refs": {"openroad": "v2.0-1234", "yosys": "0.33"}
}
```

### Action

```json
{
  "action_type": "write_file",
  "params": {"content": "x", "path": "design/foo.sdc"},
  "schema_version": 1,
  "step_index": 2
}
```

### Observation

```json
{
  "duration_s": 1.5,
  "exit_code": 0,
  "schema_version": 1,
  "stderr_tail": "",
  "stdout_tail": "ok",
  "step_index": 3,
  "timed_out": false,
  "tool_name": "openroad"
}
```

### StepResult

```json
{
  "done": true,
  "message": "done",
  "metrics": [{"name": "area_um2", "unit": "um^2", "value": 123.0}],
  "observation": {
    "duration_s": 1.5,
    "exit_code": 0,
    "schema_version": 1,
    "stderr_tail": "",
    "stdout_tail": "ok",
    "step_index": 3,
    "timed_out": false,
    "tool_name": "openroad"
  },
  "provenance": {
    "grader_id": "rule-grader",
    "grader_version": "0.1.0",
    "seed": 42,
    "task_id": "gcd-flow",
    "task_version": "0.1.0",
    "toolchain_refs": {"openroad": "v2.0-1234"}
  },
  "reward": 1.0,
  "schema_version": 1,
  "status": "success",
  "step_index": 3
}
```

### GradeResult

```json
{
  "details": {"attempts": 2},
  "message": "graded",
  "metrics": [{"name": "area_um2", "unit": "um^2", "value": 123.0}],
  "passed": true,
  "provenance": {
    "grader_id": "rule-grader",
    "grader_version": "0.1.0",
    "seed": 42,
    "task_id": "gcd-flow",
    "task_version": "0.1.0",
    "toolchain_refs": {"openroad": "v2.0-1234"}
  },
  "schema_version": 1,
  "score": 0.75,
  "status": "pass",
  "task_id": "gcd-flow",
  "task_version": "0.1.0"
}
```

## Usage

```python
from silicon_env.task import TaskSpec
from silicon_env.types import ContractError

try:
    task = TaskSpec.from_json(raw_text)  # raises ContractError on bad schema
except ContractError as exc:
    print("invalid task:", exc)
```

Direct `submit()` consumes one action, just like `step(submit)`. If the action
or wall-time budget is exhausted, it returns a timeout grade without running
the grader. Tool attempts are charged immediately before execution so a
timeout remains a structured result even when it uses the remaining wall time.
