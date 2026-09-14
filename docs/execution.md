# Execution backends: trusted local vs restricted container (M0-09)

Two backends implement the same runner protocol (named tool + argv array
in, `RunResult` with `SUCCESS` / `TOOL_FAILURE` / `TIMEOUT` /
`INFRA_ERROR` out). Pick per trust level of the code being executed.

## Trusted local mode (`silicon_env.runner.ToolRunner`)

Runs pre-registered named tools directly on the host: explicit `cwd`,
env allowlist, monotonic deadlines, process-group cleanup, capped
`stdout.log` / `stderr.log` capture.

Use it **only** for already-vetted commands (graders, harness helpers,
deterministic checks). It provides **no** filesystem, network, or
memory isolation beyond the env allowlist and output caps, and must
never execute untrusted candidate code.

## Restricted container mode (`silicon_env.runners.container`)

A Docker-compatible Linux backend for untrusted candidate code. One
backend only (no scheduler, remote, VM-grade isolation, or image
building).

```python
from silicon_env.runners.container import ContainerMount, ContainerRunner

runner = ContainerRunner(tools={"python": ["python3"]})
result = runner.run(
    "python",
    ["-c", "print('hi')"],
    mounts=[
        ContainerMount("/path/to/task-inputs", "/task/inputs", readonly=True),
        ContainerMount("/path/to/candidate-out", "/task/outputs", readonly=False),
    ],
    log_dir="/path/to/logs",
    timeout_s=60,
)
```

### Hardening (every `docker run`)

| Property | Flag |
| -------- | ---- |
| Pinned image, never auto-pulled/built | `--pull never` + allowlist (`PINNED_IMAGES`; tag or `@sha256:` digest; `:latest` rejected) |
| No network | `--network none` |
| Non-root | `--user 65532:65532` |
| Read-only root | `--read-only` (+ `--init`) |
| No capabilities or privilege escalation | `--cap-drop ALL --security-opt no-new-privileges` |
| Process limit | `--pids-limit 128` |
| Memory limit | `--memory 512m` / `--memory-swap 512m` |
| CPU limit | `--cpus 1.0` |
| Explicit writable scratch | `--tmpfs /scratch:rw,nosuid,nodev,exec,size=64m` (+ throwaway `/tmp` tmpfs) |
| Always cleaned up | `--rm` + `docker rm -f` on timeout/cancel |

Defaults are tunable at construction (`memory=`, `cpus=`,
`pids_limit=`, `user=`, ...); `provenance()` reports the effective
values. Network access, root UIDs, and unlimited resource settings are rejected.
The image's own `PATH` is retained unless explicitly allowlisted by the caller.

### Mount allowlist

Only declared `ContainerMount(host_path, container_path, readonly)`
entries are mounted (`:ro` inputs, `:rw` candidate output). The runner
**refuses** to mount host `/`, `/etc`, `/var/run` (incl. the Docker
socket), the host home directory, and `$SILICON_EVAL_SECRETS_DIR` when
set -- misuse raises `ContainerRunnerError` before Docker is touched.
Container paths must be absolute and free of `..`. Ancestors of protected
host paths are also rejected. The common `run(..., cwd=...)` interface mounts
the declared candidate workspace read-write at `/work`; additional immutable
inputs use explicit read-only mounts. Stage workspaces outside the home tree
(for example under `/tmp`) to satisfy the mount policy.

### Result mapping

- exit 0 -> `SUCCESS`; nonzero tool exit -> `TOOL_FAILURE`
  (exit 137 notes a SIGKILL / possible OOM in `error`).
- deadline overrun -> `TIMEOUT` (`timed_out=True`); the container is
  force-removed. If cleanup fails, the result explicitly reports that removal
  could not be confirmed.
- missing `docker` binary, missing image, daemon errors, and
  architecture mismatches (`exec format error`, docker 125/126/127)
  -> `INFRA_ERROR` with `launched=False` -- never success.

### Provenance

`runner.provenance()` returns backend identity, the pinned image ref,
the image allowlist, effective limits, and the security posture as a
JSON-serializable dict. Environments automatically record it under
`runner_provenance` in reset events and final manifests. Version tags can move;
use a verified digest reference when byte-identical image identity is required.

Both backends stream stdout/stderr to bounded files rather than buffering all
output in host memory. Docker diagnostic text alone never changes a tool result
into an infrastructure failure; Docker exit codes determine that distinction.

### Opt-in integration checks (Linux + Docker only)

```bash
# Pull explicitly; the runner never fetches images implicitly.
IMAGE=$(python -c 'from silicon_env.runners.container import DEFAULT_IMAGE; print(DEFAULT_IMAGE)')
docker pull "$IMAGE"
SILICON_RUN_DOCKER_TESTS=1 pytest tests/test_container_runner.py
```

Covers: host sentinel outside mounts unreadable, a world-writable input blocked
by the read-only filesystem, writable candidate output, loopback-only networking,
blocked egress, timeout cleanup, non-root identity, dropped privileges, and
effective cgroup resource limits. The limit checks require Linux cgroup v2.
Default tests skip integration. Setting `SILICON_RUN_DOCKER_TESTS=1` requires a
working Linux Docker daemon and the pre-pulled image; missing prerequisites fail
the requested check.

Verified on Linux ARM64 in Colima: the full suite passed with **192 tests and no
skips**. See [recorded runtime and image evidence](m0-docker-verification.json).

To reproduce using the dedicated Mac test VM created during review:

```bash
colima start silicon-m0
colima -p silicon-m0 ssh
# Run from the staged source tree inside the VM:
cd /private/tmp/silicon-m0-docker/source
SILICON_RUN_DOCKER_TESTS=1 /tmp/m0-venv/bin/python -m pytest -q
# Exit the VM shell, then release its memory:
exit
colima stop silicon-m0
```

The staged checkout and virtual environment are temporary test resources and
may need recreating after cleanup. The VM is stopped after verification.

## Non-goals

No VM-grade isolation, no scheduler, no remote execution, no image
building, and no guarantee that Docker fits an 8 GB laptop -- the
container backend is opt-in; the default test suite stays
Docker/network-free.
