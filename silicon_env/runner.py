"""Local trusted tool runner with deadlines and process-group cleanup (M0-04).

Runs pre-registered named tools as direct argv arrays (never a shell
string) with an explicit cwd, an env allowlist, monotonic deadlines, and
capped stdout/stderr capture files.

Scope notes (non-goals): local execution only -- no Docker, no remote
exec, no tool-specific output parsing, and no hard memory isolation on
macOS (address-space limits are not enforceable portably here).

This is the *trusted local* backend: it runs tools directly on the host
and must only execute already-vetted commands. Untrusted candidate code
belongs in the restricted Linux container backend
(``silicon_env.runners.container.ContainerRunner``), which enforces
pinned images, no network, a non-root UID, a read-only root, dropped
capabilities, and explicit resource limits -- see ``docs/execution.md``.

Standard library only, Python >= 3.10 compatible. POSIX process groups
via ``start_new_session`` + ``os.killpg`` (macOS-safe).
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from silicon_env.types import StepStatus

DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_KILL_GRACE_S = 1.0
DEFAULT_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")

_STDOUT_FILENAME = "stdout.log"
_STDERR_FILENAME = "stderr.log"
_READ_CHUNK = 65_536


class RunnerError(ValueError):
    """Raised for runner misuse (unknown tool, bad cwd/args/timeout, ...)."""


def _require_tool_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise RunnerError(f"tool name must be a non-empty string, got {name!r}")
    return name


def _require_argv(argv: object, *, what: str) -> tuple[str, ...]:
    if not isinstance(argv, (list, tuple)) or not argv:
        raise RunnerError(f"{what} must be a non-empty list of strings")
    for item in argv:
        if not isinstance(item, str) or not item:
            raise RunnerError(f"{what} must be a non-empty list of strings, got {argv!r}")
    return tuple(argv)


def _require_positive_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunnerError(f"{field_name} must be a number, got {type(value).__name__}")
    result = float(value)
    if not (result > 0) or result != result or result in (float("inf"), float("-inf")):
        raise RunnerError(f"{field_name} must be a positive finite number, got {value!r}")
    return result


def _require_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RunnerError(f"{field_name} must be a positive int, got {value!r}")
    return value


def _kill_process_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Signal the whole child process group; ignore already-exited races."""
    _kill_pgid(proc.pid, sig)


def _kill_pgid(pgid: int, sig: int) -> None:
    """Signal a process group unconditionally (for orphaned grandchildren)."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return


@dataclass(frozen=True)
class RunResult:
    """Structured outcome of one tool invocation (always returned, never None)."""

    tool_name: str
    argv: tuple[str, ...]
    cwd: Path
    log_dir: Path
    stdout_path: Path
    stderr_path: Path
    exit_code: int | None
    status: StepStatus
    timed_out: bool
    launched: bool
    duration_s: float
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "argv": list(self.argv),
            "cwd": str(self.cwd),
            "log_dir": str(self.log_dir),
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
            "exit_code": self.exit_code,
            "status": self.status.value,
            "timed_out": self.timed_out,
            "launched": self.launched,
            "duration_s": self.duration_s,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "error": self.error,
        }


class ToolRunner:
    """Registry + executor for trusted local tool commands.

    :param tools: optional initial mapping of tool name to base argv
        (e.g. ``{"python": [sys.executable]}``). Per-run ``args`` are
        appended; the executable itself can never come from the agent.
    :param env_allowlist: env var names allowed into the child. The child
        environment is built from these allowlisted entries of
        ``os.environ`` plus per-run ``env`` overrides (whose keys must
        also be allowlisted).
    :param default_timeout_s: applied when ``run`` omits ``timeout_s``.
    :param default_max_output_bytes: per-stream capture cap applied when
        ``run`` omits ``max_output_bytes``.
    :param kill_grace_s: delay between SIGTERM and SIGKILL of the child
        process group on timeout.
    """

    def __init__(
        self,
        *,
        tools: Mapping[str, Sequence[str]] | None = None,
        env_allowlist: Sequence[str] | None = None,
        default_timeout_s: float = DEFAULT_TIMEOUT_S,
        default_max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        kill_grace_s: float = DEFAULT_KILL_GRACE_S,
    ) -> None:
        self._tools: dict[str, tuple[str, ...]] = {}
        if tools is not None:
            if not isinstance(tools, Mapping):
                raise RunnerError("tools must be a mapping of name to argv")
            for name, argv in tools.items():
                self.register(name, argv)
        if env_allowlist is None:
            self._env_allowlist = tuple(DEFAULT_ENV_ALLOWLIST)
        else:
            if not isinstance(env_allowlist, (list, tuple)):
                raise RunnerError("env_allowlist must be a list of strings")
            cleaned: list[str] = []
            for item in env_allowlist:
                if not isinstance(item, str) or not item.strip():
                    raise RunnerError(
                        f"env_allowlist entries must be non-empty strings, got {item!r}"
                    )
                cleaned.append(item)
            self._env_allowlist = tuple(cleaned)
        self._default_timeout_s = _require_positive_float(
            default_timeout_s, field_name="default_timeout_s"
        )
        self._default_max_output = _require_positive_int(
            default_max_output_bytes, field_name="default_max_output_bytes"
        )
        self._kill_grace_s = _require_positive_float(kill_grace_s, field_name="kill_grace_s")

    @property
    def tools(self) -> dict[str, tuple[str, ...]]:
        return dict(self._tools)

    @property
    def env_allowlist(self) -> tuple[str, ...]:
        return self._env_allowlist

    def register(self, name: str, argv: Sequence[str]) -> None:
        """Register (or replace) a named tool's base argv array."""
        clean_name = _require_tool_name(name)
        self._tools[clean_name] = _require_argv(argv, what=f"tools[{clean_name!r}]")

    def run(
        self,
        tool_name: str,
        args: Sequence[str] = (),
        *,
        cwd: str | os.PathLike[str],
        log_dir: str | os.PathLike[str],
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        max_output_bytes: int | None = None,
    ) -> RunResult:
        """Run a registered tool; always returns a structured :class:`RunResult`.

        ``args`` are appended to the registered base argv (no shell).
        ``cwd`` must be an existing directory. Only allowlisted env vars
        reach the child. Stdout/stderr stream to ``log_dir/stdout.log``
        and ``log_dir/stderr.log`` capped at ``max_output_bytes`` each.

        Launch failures (missing executable, bad cwd at exec time, ...)
        return ``status=INFRA_ERROR`` with ``launched=False`` instead of
        raising; only misuse (unknown tool, bad arg types, ...) raises
        :class:`RunnerError`.
        """
        clean_name = _require_tool_name(tool_name)
        if clean_name not in self._tools:
            raise RunnerError(f"unknown tool {clean_name!r}; registered: {sorted(self._tools)}")
        if args is None:
            extra: tuple[str, ...] = ()
        else:
            if not isinstance(args, (list, tuple)):
                raise RunnerError(f"args must be a list of strings, got {type(args).__name__}")
            for item in args:
                if not isinstance(item, str):
                    raise RunnerError(f"args must be a list of strings, got {args!r}")
            extra = tuple(args)
        argv = self._tools[clean_name] + extra

        if isinstance(cwd, os.PathLike):
            cwd = os.fspath(cwd)
        if not isinstance(cwd, str) or not cwd.strip():
            raise RunnerError("cwd must be a non-empty path")
        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            raise RunnerError(f"cwd must be an existing directory: {cwd!r}")

        if isinstance(log_dir, os.PathLike):
            log_dir = os.fspath(log_dir)
        if not isinstance(log_dir, str) or not log_dir.strip():
            raise RunnerError("log_dir must be a non-empty path")
        log_path = Path(log_dir)
        try:
            log_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunnerError(f"cannot create log_dir {log_dir!r}: {exc}") from exc

        limit = (
            self._default_max_output
            if max_output_bytes is None
            else _require_positive_int(max_output_bytes, field_name="max_output_bytes")
        )
        deadline_s = (
            self._default_timeout_s
            if timeout_s is None
            else _require_positive_float(timeout_s, field_name="timeout_s")
        )

        child_env = self._build_child_env(env)

        stdout_path = log_path / _STDOUT_FILENAME
        stderr_path = log_path / _STDERR_FILENAME

        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=str(cwd_path),
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
            # Distinct from a nonzero exit: the tool never launched.
            _write_empty_logs(stdout_path, stderr_path)
            duration = time.monotonic() - start
            return RunResult(
                tool_name=clean_name,
                argv=argv,
                cwd=cwd_path,
                log_dir=log_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                exit_code=None,
                status=StepStatus.INFRA_ERROR,
                timed_out=False,
                launched=False,
                duration_s=duration,
                error=f"failed to launch {argv[0]!r}: {exc}",
            )

        try:
            return self._pump_and_wait(
                proc,
                clean_name=clean_name,
                argv=argv,
                cwd_path=cwd_path,
                log_path=log_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                start=start,
                deadline_s=deadline_s,
                limit=limit,
            )
        except BaseException:
            # The leader may exit before descendants; always escalate the group.
            _kill_pgid(proc.pid, signal.SIGTERM)
            time.sleep(self._kill_grace_s)
            _kill_pgid(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=self._kill_grace_s)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            # Reap pipes so file descriptors never leak to the caller.
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

    # -- internals ------------------------------------------------------

    def _build_child_env(self, env: Mapping[str, str] | None) -> dict[str, str]:
        child: dict[str, str] = {
            key: os.environ[key] for key in self._env_allowlist if key in os.environ
        }
        if env is None:
            return child
        if not isinstance(env, Mapping):
            raise RunnerError("env must be a mapping of str to str")
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise RunnerError("env must map str names to str values")
            if key not in self._env_allowlist:
                raise RunnerError(
                    f"env var {key!r} is not in the allowlist {list(self._env_allowlist)}"
                )
            child[key] = value
        return child

    def _pump_and_wait(
        self,
        proc: subprocess.Popen[bytes],
        *,
        clean_name: str,
        argv: tuple[str, ...],
        cwd_path: Path,
        log_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        start: float,
        deadline_s: float,
        limit: int,
    ) -> RunResult:
        deadline = start + deadline_s
        assert proc.stdout is not None and proc.stderr is not None
        for stream in (proc.stdout, proc.stderr):
            try:
                os.set_blocking(stream.fileno(), False)
            except (OSError, ValueError):
                pass

        out_fh = open(stdout_path, "wb")
        err_fh = open(stderr_path, "wb")
        out_written = 0
        err_written = 0
        out_truncated = False
        err_truncated = False
        timed_out = False
        term_sent_at: float | None = None

        def _append(fh, data: bytes, written: int) -> tuple[int, bool]:
            if not data:
                return written, False
            room = limit - written
            if room <= 0:
                return written, True
            fh.write(data[:room])
            return written + min(len(data), room), len(data) > room

        def _drain_available() -> None:
            nonlocal out_written, err_written, out_truncated, err_truncated
            nonlocal out_eof, err_eof
            for stream, fh, kind in ((proc.stdout, out_fh, "out"), (proc.stderr, err_fh, "err")):
                assert stream is not None
                if (kind == "out" and out_eof) or (kind == "err" and err_eof):
                    continue
                for _ in range(16):
                    try:
                        chunk = stream.read(_READ_CHUNK)
                    except (BlockingIOError, OSError):
                        break
                    if chunk is None:
                        break
                    if chunk == b"":
                        if kind == "out":
                            out_eof = True
                        else:
                            err_eof = True
                        break
                    if kind == "out":
                        out_written, hit = _append(fh, chunk, out_written)
                        out_truncated = out_truncated or hit
                    else:
                        err_written, hit = _append(fh, chunk, err_written)
                        err_truncated = err_truncated or hit
                    if len(chunk) < _READ_CHUNK:
                        break

        out_eof = False
        err_eof = False
        try:
            while True:
                _drain_available()
                now = time.monotonic()
                exited = proc.poll() is not None
                if now >= deadline and not timed_out and not (exited and out_eof and err_eof):
                    timed_out = True
                    _kill_process_group(proc, signal.SIGTERM)
                    term_sent_at = now
                if timed_out and not exited:
                    assert term_sent_at is not None
                    if now - term_sent_at >= self._kill_grace_s:
                        _kill_pgid(proc.pid, signal.SIGKILL)
                if exited and out_eof and err_eof:
                    if timed_out and term_sent_at is not None:
                        # Descendants may close their pipes and ignore TERM.
                        time.sleep(max(0.0, self._kill_grace_s - (now - term_sent_at)))
                    _kill_pgid(proc.pid, signal.SIGKILL)
                    break
                if exited and timed_out and not (out_eof and err_eof):
                    # Parent reaped but a grandchild still holds the pipes:
                    # escalate to SIGKILL so EOF arrives promptly.
                    assert term_sent_at is not None
                    if now - term_sent_at >= self._kill_grace_s:
                        _kill_pgid(proc.pid, signal.SIGKILL)
                # Avoid a hot spin while waiting for output or the deadline.
                watch = []
                if not out_eof:
                    watch.append(proc.stdout)
                if not err_eof:
                    watch.append(proc.stderr)
                if not watch:
                    if exited:
                        break
                    time.sleep(0.01)
                    continue
                try:
                    select.select(watch, [], [], 0.05)
                except (OSError, ValueError):
                    time.sleep(0.01)
                # Safety backstop: never spin past deadline+grace+margin
                # without the process exiting (e.g. unkillable child).
                if time.monotonic() - start > deadline_s + 2 * self._kill_grace_s + 10:
                    _kill_process_group(proc, signal.SIGKILL)
                    break
        finally:
            out_fh.close()
            err_fh.close()

        # Ensure the (possibly killed) child is reaped.
        if proc.poll() is None:
            _kill_process_group(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=self._kill_grace_s)
            except Exception:
                pass
        exit_code = proc.poll()
        if exit_code is None:
            try:
                exit_code = proc.wait(timeout=self._kill_grace_s)
            except Exception:
                exit_code = None

        duration = time.monotonic() - start
        if timed_out:
            status = StepStatus.TIMEOUT
        elif exit_code == 0:
            status = StepStatus.SUCCESS
        elif exit_code is None:
            status = StepStatus.INFRA_ERROR
        else:
            status = StepStatus.TOOL_FAILURE
        return RunResult(
            tool_name=clean_name,
            argv=argv,
            cwd=cwd_path,
            log_dir=log_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            exit_code=exit_code,
            status=status,
            timed_out=timed_out,
            launched=True,
            duration_s=duration,
            stdout_truncated=out_truncated,
            stderr_truncated=err_truncated,
            stdout_bytes=out_written,
            stderr_bytes=err_written,
            error="timed out" if timed_out else "",
        )


def _write_empty_logs(stdout_path: Path, stderr_path: Path) -> None:
    for path in (stdout_path, stderr_path):
        try:
            with open(path, "wb"):
                pass
        except OSError:
            pass


__all__ = [
    "DEFAULT_ENV_ALLOWLIST",
    "DEFAULT_KILL_GRACE_S",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_S",
    "RunResult",
    "RunnerError",
    "ToolRunner",
]
