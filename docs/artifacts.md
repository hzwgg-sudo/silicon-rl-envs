# Artifacts: traces and manifests (M0-07)

Every episode records a replayable trace plus a final manifest under a
per-episode run directory. Tracing is on by default and never changes
stepping semantics.

## Layout

```text
<work_root>/runs/<run_id>/trace.jsonl
<work_root>/runs/<run_id>/manifest.json
<work_root>/runs/<run_id>/artifacts/step_<n>/stdout.log  # tool logs only
<work_root>/runs/<run_id>/artifacts/step_<n>/stderr.log
```

- `trace.jsonl`: append-only JSONL, one strict-JSON object per line, with
  monotonically ordered `seq` values (`reset` at 0, then one `step` per
  action, then one `submit` on grading).
- `manifest.json`: written atomically (tmp file + rename) on `submit()` or
  on `close()` without submit.
- Tool logs produced under `<work_root>/logs/step_<n>/` are **copied** into
  `artifacts/` so references stay valid after episode workspaces are
  cleaned up. All refs are run-relative posix paths, never absolute.

## Events

| type     | contents                                                        |
| -------- | --------------------------------------------------------------- |
| `reset`  | task id/version/hash, seed, toolchain refs, grader id, initial  |
|          | observation summary, budget snapshot, redacted env, immutable    |
|          | input hash, `timestamp_s` (diagnostic only)                     |
| `step`   | step index, redacted action params, status/reward/done,         |
|          | observation summary, relative `artifact_refs`, budget snapshot  |
| `submit` | grade status/score/passed, message, metrics, budget snapshot    |

Observation summaries keep `step_index`, `tool_name`, `exit_code`,
redacted `stdout_tail`/`stderr_tail`, and `timed_out` (plus `duration_s`
for diagnostics).

## Manifest

Binds the episode to its inputs and outputs: `run_id`, `task_id`,
`task_version`, `task_hash` (SHA-256 of the canonical task JSON), `seed`,
`toolchain_refs`, grader identity, `budgets` (limits), final
`budget_snapshot`, `status`/`passed`/`complete`, step/event counts,
`trace_file` (`"trace.jsonl"`), `artifacts` (`[{path, sha256, size_bytes}]`),
`semantic_hash`, and a redacted message.

Interrupted episodes (closed or superseded without `submit`) finalize as:

```json
{"status": "incomplete", "passed": false, "complete": false}
```

Incomplete runs are never successful by construction (`finalize` refuses
`passed=true` with `status="incomplete"`, and verification re-checks it).

## Semantic replay vs timing

`timestamp_s`, `duration_s`, `elapsed_s`, and `remaining_wallclock_s` are
recorded for diagnostics but excluded from the **semantic projection**
(`silicon_env.trace.semantic_projection_trace`), along with the `run_id`
recording identity. The manifest `semantic_hash` covers the projection
only, so two runs of the same seed + action sequence hash identically
despite different clocks and run ids. Timestamp equality is explicitly
not required.

## Redaction

- Mapping keys matching `API_KEY`/`TOKEN`/`SECRET`/`PASSWORD`/...
  (case-insensitive) have values replaced with `"***REDACTED***"`.
- Inline `key=value` / `key: value` secrets in free text become
  `key=<REDACTED>`.
- Known absolute host roots (cwd, `TMPDIR`, run dir, `$HOME`) become
  `<HOST_PATH>`. Store artifact refs relatively; never log absolute
  evaluator paths.

## Verification

```python
from silicon_env.trace import read_events, read_manifest, verify_run

events = read_events(run_dir / "trace.jsonl")   # strict JSON + ordered seqs
manifest = read_manifest(run_dir / "manifest.json")
verify_run(run_dir)  # parseable trace, relative refs resolve, hashes match
```

`verify_run` raises `TraceError` on non-parseable lines, out-of-order
seqs, absolute/`..` refs, dangling refs, hash mismatches, or an
`incomplete` run marked passed.

## Non-goals

No database, no telemetry upload, no waveform ingestion, and no exact
timestamp equality. Hashing is SHA-256 over bytes / canonical strict JSON.
