# M0 independent review

Reviewed implementation through `2de9aa4` against GitHub issues #1–#9.
Corrections were committed as `d167fa3`. The follow-up Docker verification
is recorded below. No push, merge, or issue-state change was made.

## Findings corrected

- **M0-01 / M0-08:** README setup used an undeclared virtual environment and
  unquoted extras that fail in zsh. The example now follows the documented install.
- **M0-02 / M0-03:** Edit patterns now validate before reset discards the current
  workspace. Reject escaping workspace prefixes and recursive template/output
  placement. Reserve the ownership marker, create it exclusively, and reject
  template marker symlinks that could overwrite outside files.
- **M0-04:** Apply deadlines when the process leader has exited but children still
  hold pipes. Escalate cleanup for children that ignore TERM or close their output
  pipes, including cancellation. Bound each drain pass so output cannot starve
  deadline checks. Repair an inverted assertion in the original child-cleanup test.
- **M0-05 / M0-06:** Validate malformed tool timeouts before parsing them. Charge
  execution before dispatch so consuming the remaining wall time returns a
  structured timeout instead of raising during accounting. Direct submission
  consumes an action and cannot bypass an exhausted budget. Preserve grader
  infrastructure, timeout, and tool-failure statuses in submit-step results.
- **M0-07:** Isolate tool logs by episode. Verify the trace semantic hash, event
  count, and event artifact references. Record invalid actions that fail contract
  serialization. Redact nested secret fields and known secret values in exported
  text logs. Record container identity and limits in manifests. Reject appending
  events after finalization and artifact paths resolving outside the run directory.
- **M0-08:** Preserve infrastructure exit code 3, align usage errors with that
  documented code, count implicit submission, and copy referenced artifacts into
  exported runs.
- **M0-09:** Reuse capped streaming instead of buffering all container output in
  host memory. Reject network/root/unlimited settings and mounts containing
  protected directories. Normalize mount destinations and reject volume separators.
  Add no-new-privileges. Retain image PATH by default, distinguish tool stderr from
  Docker exit failures, support the environment's cwd interface, and report cleanup
  failures without falsely confirming removal.

## Verification

On this Apple Silicon macOS host, Python 3.12:

| Check | Result |
| --- | --- |
| Fresh `.venv`, editable install with dev extras | Passed |
| Package imports; dependency consistency | Passed |
| `.venv/bin/pytest -q` | **188 passed, 3 skipped** |
| `.venv/bin/ruff check .` | Passed |
| `git diff --check` | Passed |
| Installed CLI run + saved submission regrade | Passed twice |
| Exported manifest/artifact verification | Passed |
| Same-seed CLI replay semantic hashes | Identical |
| Generated run/work/venv ignores | Confirmed |
| Initial 30 regression cases against original `HEAD` snapshot | **28 failed, 2 passed** |

The final suite adds 32 regression cases. Process tests execute real local Python
subprocesses. Container command/output/interface tests use a fake Docker executable;
they verify orchestration behavior, not real container isolation.

## Docker follow-up: verified on Linux

The previously skipped gate now passes on a dedicated Colima VM running
Ubuntu 24.04.4, Linux 6.8.0-117-generic, ARM64, Python 3.12.3 and Docker 29.5.2.
The VM was limited to 2 CPUs and 2 GiB RAM and shared only a temporary test directory.

`SILICON_RUN_DOCKER_TESTS=1 python -m pytest -q --tb=short` completed with
**192 passed, zero failed, zero skipped**, in 11.03 seconds. This includes the full
core suite plus four real container integration tests. Effective cgroup limits
matched the runner configuration. Docker reported zero remaining containers.

The filesystem check now uses a world-writable input and asserts `EROFS`, proving
that the read-only mount blocks writes. It also verifies successful candidate
output writes. Network checks require a loopback-only interface set. Runtime
checks verify UID 65532, no effective capabilities, no-new-privileges, and actual
memory/swap/CPU/PID limits. Opting in now fails if Docker, its daemon, or the image
is unavailable instead of silently skipping the requested checks.

See [machine-readable evidence](m0-docker-verification.json) for the resolved
registry digest, runtime versions, configuration and results. The original
17-test container module also passed before these checks were strengthened.

The default image uses a version tag; the evidence records the exact digest
executed. Use that verified digest for byte-identical image reproducibility.
Raw execution logs remain local diagnostics; exported `.log` artifacts are redacted.
