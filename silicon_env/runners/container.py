"""Restricted Docker-compatible Linux container runner (M0-09).

A Docker-compatible Linux backend behind the existing runner protocol:
it takes a registered tool name + argv array and returns
:class:`silicon_env.runner.RunResult` with the same
``StepStatus`` vocabulary (``SUCCESS`` / ``TOOL_FAILURE`` / ``TIMEOUT`` /
``INFRA_ERROR``).

Security posture (defense in depth, not VM-grade isolation):

- Pinned images only: the configured image must come from an explicit
  allowlist (``allowed_images``) and must carry a tag or digest pin.
  ``latest`` without a digest is rejected.
- No network (``--network none``), non-root UID (``--user 65532:65532``),
  read-only root filesystem (``--read-only``), all capabilities dropped
  (``--cap-drop ALL``).
- Process / memory / CPU limits (``--pids-limit``, ``--memory`` /
  ``--memory-swap``, ``--cpus``).
- Exactly one explicit writable scratch area (``/scratch`` tmpfs);
  ``/tmp`` is a second throwaway tmpfs required by most toolchains.
  Everything else in the container filesystem is read-only except
  mounts explicitly declared ``readonly=False``.
- Only declared mounts reach the container. Host home directories,
  the Docker socket, ``/var/run``, ``/``, ``/etc``, and the evaluator
  secrets directory (``$SILICON_EVAL_SECRETS_DIR`` when set) can never
  be mounted -- misuse raises :class:`ContainerRunnerError`.
- Images are never pulled or built implicitly (``--pull never``);
  a missing image is an infrastructure error, never a silent fetch.
- Containers always run with ``--rm`` and are force-removed
  (``docker rm -f``) on timeout or cancellation so no live container
  is left behind.

Stdlib only, Python >= 3.10. Command construction
(:func:`build_docker_argv`) is pure and needs no Docker daemon, so it
is unit-testable anywhere (including macOS without Docker).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from silicon_env.runner import (
    DEFAULT_KILL_GRACE_S,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_S,
    RunnerError,
    RunResult,
    ToolRunner,
    _require_argv,
    _require_positive_float,
    _require_positive_int,
    _require_tool_name,
    _write_empty_logs,
)
from silicon_env.types import StepStatus

# Pinned image allowlist. Tags pin major.minor.patch + distro; callers
# that need byte-identical reproducibility should use a digest-pinned
# ref (``...@sha256:<hex>``) instead -- both forms are accepted, and
# the effective ref is reported in :meth:`ContainerRunner.provenance`.
# NOTE: only real, pullable refs belong here. Do not add illustrative
# or unverified digests; digest-pinned refs are validated by the unit
# tests with caller-supplied allowlists.
PINNED_IMAGES: dict[str, str] = {
    "python-slim": "python:3.12.3-slim-bookworm",
}
DEFAULT_IMAGE = PINNED_IMAGES["python-slim"]

DEFAULT_USER = "65532:65532"  # non-root, nobody-like UID:GID
DEFAULT_NETWORK = "none"
DEFAULT_MEMORY = "512m"
DEFAULT_MEMORY_SWAP = "512m"
DEFAULT_CPUS = "1.0"
DEFAULT_PIDS_LIMIT = 128
DEFAULT_SCRATCH_SIZE = "64m"
DEFAULT_WORKDIR = "/work"
DEFAULT_SCRATCH_DIR = "/scratch"

_STDOUT_FILENAME = "stdout.log"
_STDERR_FILENAME = "stderr.log"

# Docker exit codes that mean the container never ran the tool.
_DOCKER_DAEMON_EXIT_CODES = frozenset({125, 126, 127})
# Stderr markers (lowercased, substring match) for infra failures.
_INFRA_MARKERS = (
    "unable to find image",
    "no such image",
    "not found",
    "exec format error",
    "is not supported on",
    "cannot connect to the docker daemon",
    "docker daemon",
    "permission denied",  # e.g. cannot reach the daemon socket
)


class ContainerRunnerError(RunnerError):
    """Misuse of the container runner (alias of RunnerError)."""


def docker_available(docker_bin: str = "docker") -> bool:
    """Return True when a ``docker``-compatible binary resolves on PATH."""
    if not isinstance(docker_bin, str) or not docker_bin.strip():
        return False
    if os.path.dirname(docker_bin):
        path = Path(docker_bin)
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(docker_bin) is not None


def _require_image_ref(image: object, *, allowed_images: Sequence[str]) -> str:
    if not isinstance(image, str) or not image.strip():
        raise ContainerRunnerError(f"image must be a non-empty string, got {image!r}")
    ref = image.strip()
    if ref not in list(allowed_images):
        raise ContainerRunnerError(
            f"image {ref!r} is not in the pinned allowlist {list(allowed_images)}"
        )
    bare = ref.split("@", 1)[0]
    has_tag = ":" in bare.split("/")[-1]
    has_digest = "@sha256:" in ref
    if not (has_tag or has_digest):
        raise ContainerRunnerError(
            f"image {ref!r} must be pinned with a tag (e.g. ':3.12.3-slim') "
            "or a digest ('@sha256:...')"
        )
    if not has_digest and bare.endswith(":latest"):
        raise ContainerRunnerError(
            f"image {ref!r} uses the floating ':latest' tag without a digest pin"
        )
    return ref


def _require_container_path(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value.strip() == "":
        raise ContainerRunnerError(f"{field_name} must be an absolute container path")
    parts = value.split("/")
    if ".." in parts:
        raise ContainerRunnerError(f"{field_name} must not contain '..': {value!r}")
    if ":" in value or "\x00" in value:
        raise ContainerRunnerError(f"{field_name} contains a forbidden separator")
    return os.path.normpath(value)


@dataclass(frozen=True)
class ContainerMount:
    """One host -> container bind mount declaration."""

    host_path: str | os.PathLike[str]
    container_path: str
    readonly: bool = True


def _host_str(host: str | os.PathLike[str]) -> str:
    if isinstance(host, os.PathLike):
        host = os.fspath(host)
    if not isinstance(host, str) or not host.strip():
        raise ContainerRunnerError("mount host_path must be a non-empty path")
    return host


def _forbidden_host_roots() -> list[Path]:
    roots: list[Path] = [Path("/"), Path("/etc"), Path("/var/run")]
    try:
        home = Path.home()
        roots.append(home)
    except Exception:
        pass
    socket_path = Path("/var/run/docker.sock")
    if socket_path not in roots:
        roots.append(socket_path)
    secrets_dir = os.environ.get("SILICON_EVAL_SECRETS_DIR", "").strip()
    if secrets_dir:
        roots.append(Path(secrets_dir).expanduser())
    tmpdir = os.environ.get("TMPDIR", "").strip()
    _ = tmpdir  # TMPDIR itself is *not* forbidden; listed to avoid confusion.
    return roots


def _reject_if_forbidden(host_raw: str, candidate: Path) -> None:
    """Reject mounts at or under a forbidden host root.

    ``/`` itself is never mountable, but every absolute path is
    trivially "under" ``/`` -- so only an exact match counts there.
    """
    for root in _forbidden_host_roots():
        candidates: list[Path] = [Path(os.path.normpath(str(root)))]
        try:
            resolved_root = root.resolve() if root.exists() else None
        except OSError:
            resolved_root = None
        if resolved_root is not None and resolved_root not in candidates:
            candidates.append(resolved_root)
        for root_cmp in candidates:
            if root_cmp == Path("/"):
                inside = candidate == root_cmp
            else:
                inside = (
                    candidate == root_cmp or root_cmp in candidate.parents
                    or candidate in root_cmp.parents
                )
            if inside:
                raise ContainerRunnerError(
                    f"mount host_path {host_raw!r} is inside forbidden root "
                    f"{str(root)!r}; only declared task inputs and the candidate "
                    "output dir may be mounted"
                )


def _check_mount(mount: ContainerMount) -> tuple[str, str, bool]:
    """Validate one mount; return (host_abs, container_path, readonly)."""
    if not isinstance(mount, ContainerMount):
        raise ContainerRunnerError(
            f"mounts must be ContainerMount entries, got {type(mount).__name__}"
        )
    host_raw = _host_str(mount.host_path)
    expanded = os.path.expanduser(os.path.expandvars(host_raw))
    host_path = Path(expanded)
    if not host_path.is_absolute():
        raise ContainerRunnerError(f"mount host_path must be absolute, got {host_raw!r}")
    _reject_if_forbidden(host_raw, Path(os.path.normpath(expanded)))
    if not host_path.exists():
        raise ContainerRunnerError(f"mount host_path does not exist: {host_raw!r}")
    if ":" in str(host_path) or "\x00" in str(host_path):
        raise ContainerRunnerError("mount host_path contains a forbidden separator")
    resolved = host_path.resolve()
    _reject_if_forbidden(host_raw, resolved)
    container_path = _require_container_path(
        mount.container_path, field_name="mount container_path"
    )
    if not isinstance(mount.readonly, bool):
        raise ContainerRunnerError("mount readonly must be a bool")
    return (str(resolved), container_path, mount.readonly)


def build_docker_argv(
    *,
    docker_bin: str = "docker",
    container_name: str,
    image: str,
    tool_argv: Sequence[str],
    mounts: Sequence[ContainerMount] = (),
    env: Mapping[str, str] | None = None,
    workdir: str = DEFAULT_WORKDIR,
    user: str = DEFAULT_USER,
    network: str = DEFAULT_NETWORK,
    memory: str = DEFAULT_MEMORY,
    memory_swap: str = DEFAULT_MEMORY_SWAP,
    cpus: str = DEFAULT_CPUS,
    pids_limit: int = DEFAULT_PIDS_LIMIT,
    scratch_size: str = DEFAULT_SCRATCH_SIZE,
    scratch_dir: str = DEFAULT_SCRATCH_DIR,
) -> list[str]:
    """Build the deterministic ``docker run`` argv (pure; no Docker needed).

    Raises :class:`ContainerRunnerError` on bad tool argv, forbidden
    mounts, or a non-absolute workdir. Never invokes a shell.
    """
    if not isinstance(docker_bin, str) or not docker_bin.strip():
        raise ContainerRunnerError("docker_bin must be a non-empty string")
    if not isinstance(container_name, str) or not container_name.strip():
        raise ContainerRunnerError("container_name must be a non-empty string")
    if any(c.isspace() or c in "/\\_" for c in container_name):
        raise ContainerRunnerError(f"container_name must be a simple token, got {container_name!r}")
    if not isinstance(image, str) or not image.strip():
        raise ContainerRunnerError("image must be a non-empty string")
    _require_image_ref(image, allowed_images=[image])
    argv_inside = _require_argv(tool_argv, what="tool argv")
    workdir = _require_container_path(workdir, field_name="workdir")
    scratch_dir = _require_container_path(scratch_dir, field_name="scratch_dir")
    if not isinstance(user, str) or not user.strip():
        raise ContainerRunnerError("user must be a non-empty string")
    if not isinstance(network, str) or not network.strip():
        raise ContainerRunnerError("network must be a non-empty string")
    for label, val in (("memory", memory), ("memory_swap", memory_swap), ("cpus", cpus)):
        if not isinstance(val, str) or not val.strip():
            raise ContainerRunnerError(f"{label} must be a non-empty string")
    if isinstance(pids_limit, bool) or not isinstance(pids_limit, int) or pids_limit <= 0:
        raise ContainerRunnerError("pids_limit must be a positive int")
    if not isinstance(scratch_size, str) or not scratch_size.strip():
        raise ContainerRunnerError("scratch_size must be a non-empty string")

    if network != "none":
        raise ContainerRunnerError("restricted runner requires network='none'")
    if not re.fullmatch(r"[1-9][0-9]*(?::[1-9][0-9]*)?", user):
        raise ContainerRunnerError("user must be a numeric non-root UID and optional non-root GID")
    for label, value in (("memory", memory), ("memory_swap", memory_swap),
                         ("scratch_size", scratch_size)):
        if not re.fullmatch(r"[1-9][0-9]*[bkmgBKMG]?", value):
            raise ContainerRunnerError(f"{label} must be a finite positive size")
    try:
        _require_positive_float(float(cpus), field_name="cpus")
    except (ValueError, RunnerError) as exc:
        raise ContainerRunnerError("cpus must be finite and positive") from exc

    checked_mounts: list[tuple[str, str, bool]] = [_check_mount(m) for m in mounts]
    seen: set[str] = set()
    for _, container_path, _ in checked_mounts:
        if container_path in seen:
            raise ContainerRunnerError(f"duplicate container_path mount: {container_path!r}")
        seen.add(container_path)

    child_env: dict[str, str] = {}
    if env is not None:
        if not isinstance(env, Mapping):
            raise ContainerRunnerError("env must be a mapping of str to str")
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ContainerRunnerError("env must map str names to str values")
            child_env[key] = value

    cmd: list[str] = [
        docker_bin,
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        network,
        "--pull",
        "never",
        "--user",
        user,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(pids_limit),
        "--memory",
        memory,
        "--memory-swap",
        memory_swap,
        "--cpus",
        cpus,
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,size={scratch_size}",
        "--tmpfs",
        f"{scratch_dir}:rw,nosuid,nodev,exec,size={scratch_size}",
        "--workdir",
        workdir,
    ]
    for host_abs, container_path, readonly in checked_mounts:
        mode = "ro" if readonly else "rw"
        cmd += ["--volume", f"{host_abs}:{container_path}:{mode}"]
    for key in sorted(child_env):
        cmd += ["--env", f"{key}={child_env[key]}"]
    cmd += ["--init", image]
    cmd += list(argv_inside)
    return cmd


class ContainerRunner:
    """Registry + executor for tools run inside restricted containers.

    Mirrors the :class:`silicon_env.runner.ToolRunner` protocol (named
    tools, env allowlist, per-run timeout) but executes via
    ``docker run`` with the hardening in :func:`build_docker_argv`.
    :meth:`run` always returns a :class:`RunResult`; only misuse
    (unknown tool, forbidden mount, bad timeout, ...) raises
    :class:`ContainerRunnerError`.
    """

    def __init__(
        self,
        *,
        tools: Mapping[str, Sequence[str]] | None = None,
        image: str = DEFAULT_IMAGE,
        allowed_images: Sequence[str] | None = None,
        docker_bin: str = "docker",
        env_allowlist: Sequence[str] | None = None,
        default_timeout_s: float = DEFAULT_TIMEOUT_S,
        default_max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        kill_grace_s: float = DEFAULT_KILL_GRACE_S,
        memory: str = DEFAULT_MEMORY,
        memory_swap: str = DEFAULT_MEMORY_SWAP,
        cpus: str = DEFAULT_CPUS,
        pids_limit: int = DEFAULT_PIDS_LIMIT,
        user: str = DEFAULT_USER,
        network: str = DEFAULT_NETWORK,
        scratch_size: str = DEFAULT_SCRATCH_SIZE,
        scratch_dir: str = DEFAULT_SCRATCH_DIR,
    ) -> None:
        self._tools: dict[str, tuple[str, ...]] = {}
        if tools is not None:
            if not isinstance(tools, Mapping):
                raise ContainerRunnerError("tools must be a mapping of name to argv")
            for name, argv in tools.items():
                self.register(name, argv)
        if allowed_images is None:
            self._allowed_images = tuple(PINNED_IMAGES.values())
        else:
            if not isinstance(allowed_images, (list, tuple)) or not allowed_images:
                raise ContainerRunnerError("allowed_images must be a non-empty list")
            for item in allowed_images:
                if not isinstance(item, str) or not item.strip():
                    raise ContainerRunnerError(
                        f"allowed_images entries must be non-empty strings, got {item!r}"
                    )
            self._allowed_images = tuple(allowed_images)
        self._image = _require_image_ref(image, allowed_images=self._allowed_images)
        if not isinstance(docker_bin, str) or not docker_bin.strip():
            raise ContainerRunnerError("docker_bin must be a non-empty string")
        self._docker_bin = docker_bin
        if env_allowlist is None:
            self._env_allowlist: tuple[str, ...] = ("LANG", "LC_ALL", "TZ")
        else:
            if not isinstance(env_allowlist, (list, tuple)):
                raise ContainerRunnerError("env_allowlist must be a list of strings")
            cleaned: list[str] = []
            for item in env_allowlist:
                if not isinstance(item, str) or not item.strip():
                    raise ContainerRunnerError(
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
        for label, val in (
            ("memory", memory),
            ("memory_swap", memory_swap),
            ("cpus", cpus),
            ("user", user),
            ("network", network),
            ("scratch_size", scratch_size),
        ):
            if not isinstance(val, str) or not val.strip():
                raise ContainerRunnerError(f"{label} must be a non-empty string")
        if isinstance(pids_limit, bool) or not isinstance(pids_limit, int) or pids_limit <= 0:
            raise ContainerRunnerError("pids_limit must be a positive int")
        _require_container_path(scratch_dir, field_name="scratch_dir")
        self._memory = memory
        self._memory_swap = memory_swap
        self._cpus = cpus
        self._pids_limit = pids_limit
        self._user = user
        self._network = network
        self._scratch_size = scratch_size
        self._scratch_dir = scratch_dir

    @property
    def tools(self) -> dict[str, tuple[str, ...]]:
        return dict(self._tools)

    @property
    def image(self) -> str:
        return self._image

    @property
    def env_allowlist(self) -> tuple[str, ...]:
        return self._env_allowlist

    def register(self, name: str, argv: Sequence[str]) -> None:
        """Register (or replace) a named tool's in-container base argv."""
        clean_name = _require_tool_name(name)
        try:
            self._tools[clean_name] = _require_argv(argv, what=f"tools[{clean_name!r}]")
        except RunnerError as exc:
            raise ContainerRunnerError(str(exc)) from exc

    def provenance(self) -> dict:
        """Effective backend identity + limits for trace manifests."""
        return {
            "runner": "container",
            "backend": "docker",
            "docker_bin": self._docker_bin,
            "image": self._image,
            "image_allowlist": list(self._allowed_images),
            "limits": {
                "memory": self._memory,
                "memory_swap": self._memory_swap,
                "cpus": self._cpus,
                "pids_limit": self._pids_limit,
            },
            "security": {
                "network": self._network,
                "user": self._user,
                "read_only_root": True,
                "cap_drop": ["ALL"],
                "pull": "never",
                "writable_scratch": self._scratch_dir,
            },
        }

    def build_argv(
        self,
        tool_name: str,
        args: Sequence[str] = (),
        *,
        mounts: Sequence[ContainerMount] = (),
        env: Mapping[str, str] | None = None,
        workdir: str = DEFAULT_WORKDIR,
        container_name: str = "silicon-probe",
    ) -> list[str]:
        """Pure command construction (no Docker needed; unit-testable)."""
        clean_name = _require_tool_name(tool_name)
        if clean_name not in self._tools:
            raise ContainerRunnerError(
                f"unknown tool {clean_name!r}; registered: {sorted(self._tools)}"
            )
        extra = self._coerce_args(args)
        child_env = self._build_child_env(env)
        return build_docker_argv(
            docker_bin=self._docker_bin,
            container_name=container_name,
            image=self._image,
            tool_argv=self._tools[clean_name] + extra,
            mounts=mounts,
            env=child_env,
            workdir=workdir,
            user=self._user,
            network=self._network,
            memory=self._memory,
            memory_swap=self._memory_swap,
            cpus=self._cpus,
            pids_limit=self._pids_limit,
            scratch_size=self._scratch_size,
            scratch_dir=self._scratch_dir,
        )

    def run(
        self,
        tool_name: str,
        args: Sequence[str] = (),
        *,
        mounts: Sequence[ContainerMount] = (),
        cwd: str | os.PathLike[str] | None = None,
        log_dir: str | os.PathLike[str],
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        max_output_bytes: int | None = None,
        workdir: str = DEFAULT_WORKDIR,
    ) -> RunResult:
        """Run a registered tool in a restricted container.

        Always returns a :class:`RunResult`. Missing Docker, missing
        images, and daemon/architecture failures map to
        ``status=INFRA_ERROR`` (never success); nonzero tool exits map
        to ``TOOL_FAILURE``; deadline overruns map to ``TIMEOUT`` and
        force-remove the container.
        """
        clean_name = _require_tool_name(tool_name)
        if clean_name not in self._tools:
            raise ContainerRunnerError(
                f"unknown tool {clean_name!r}; registered: {sorted(self._tools)}"
            )
        extra = self._coerce_args(args)
        argv_inside = self._tools[clean_name] + extra
        if cwd is not None:
            # The common runner protocol declares cwd as the candidate workspace.
            # Additional immutable inputs remain explicit read-only mounts.
            mounts = (*mounts, ContainerMount(Path(cwd).absolute(), workdir, readonly=False))

        if isinstance(log_dir, os.PathLike):
            log_dir = os.fspath(log_dir)
        if not isinstance(log_dir, str) or not log_dir.strip():
            raise ContainerRunnerError("log_dir must be a non-empty path")
        log_path = Path(log_dir)
        try:
            log_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ContainerRunnerError(f"cannot create log_dir {log_dir!r}: {exc}") from exc
        stdout_path = log_path / _STDOUT_FILENAME
        stderr_path = log_path / _STDERR_FILENAME

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
        checked_workdir = _require_container_path(workdir, field_name="workdir")
        # Validate mounts eagerly so misuse raises before touching Docker.
        for mount in mounts:
            _check_mount(mount)

        start = time.monotonic()
        container_name = f"silicon-{os.getpid()}-{uuid.uuid4().hex[:12]}"

        def _infra(error: str) -> RunResult:
            _write_empty_logs(stdout_path, stderr_path)
            return RunResult(
                tool_name=clean_name,
                argv=argv_inside,
                cwd=Path(checked_workdir),
                log_dir=log_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                exit_code=None,
                status=StepStatus.INFRA_ERROR,
                timed_out=False,
                launched=False,
                duration_s=time.monotonic() - start,
                error=error,
            )

        if not docker_available(self._docker_bin):
            return _infra(
                f"docker binary {self._docker_bin!r} not found; "
                "install a Docker-compatible runtime to use the container backend"
            )

        cmd = build_docker_argv(
            docker_bin=self._docker_bin,
            container_name=container_name,
            image=self._image,
            tool_argv=argv_inside,
            mounts=mounts,
            env=child_env,
            workdir=checked_workdir,
            user=self._user,
            network=self._network,
            memory=self._memory,
            memory_swap=self._memory_swap,
            cpus=self._cpus,
            pids_limit=self._pids_limit,
            scratch_size=self._scratch_size,
            scratch_dir=self._scratch_dir,
        )

        # Reuse bounded streaming and process cleanup from the local backend.
        # Only allowlisted values above enter the container; the Docker client
        # itself retains its host environment to locate its daemon/config.
        client = ToolRunner(
            tools={clean_name: cmd}, env_allowlist=tuple(os.environ),
            kill_grace_s=self._kill_grace_s,
        )
        try:
            result = client.run(
                clean_name, cwd=log_path.resolve(), log_dir=log_path.resolve(),
                timeout_s=deadline_s, max_output_bytes=limit,
            )
        except BaseException:
            self._remove_container(container_name)
            raise
        if result.timed_out:
            removed = self._remove_container(container_name)
            error = "timed out; container removed" if removed else (
                "timed out; container cleanup could not be confirmed"
            )
            return replace(result, argv=argv_inside, cwd=Path(checked_workdir), error=error)
        stderr_text = result.stderr_path.read_text(encoding="utf-8", errors="replace")
        if not result.launched or _looks_like_infra(result.exit_code, stderr_text):
            return replace(
                result, argv=argv_inside, cwd=Path(checked_workdir),
                exit_code=None, status=StepStatus.INFRA_ERROR, launched=False,
                error=result.error or f"container infrastructure failure: {stderr_text[:500]}",
            )
        return replace(
            result, argv=argv_inside, cwd=Path(checked_workdir),
            error="container killed (possibly OOM)" if result.exit_code == 137 else "",
        )

    # -- internals --------------------------------------------------------

    def _coerce_args(self, args: Sequence[str]) -> tuple[str, ...]:
        if args is None:
            return ()
        if not isinstance(args, (list, tuple)):
            raise ContainerRunnerError(f"args must be a list of strings, got {type(args).__name__}")
        for item in args:
            if not isinstance(item, str):
                raise ContainerRunnerError(f"args must be a list of strings, got {args!r}")
        return tuple(args)

    def _build_child_env(self, env: Mapping[str, str] | None) -> dict[str, str]:
        child: dict[str, str] = {
            key: os.environ[key] for key in self._env_allowlist if key in os.environ
        }
        if env is None:
            return child
        if not isinstance(env, Mapping):
            raise ContainerRunnerError("env must be a mapping of str to str")
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ContainerRunnerError("env must map str names to str values")
            if key not in self._env_allowlist:
                raise ContainerRunnerError(
                    f"env var {key!r} is not in the allowlist {list(self._env_allowlist)}"
                )
            child[key] = value
        return child

    def _remove_container(self, container_name: str) -> bool:
        try:
            result = subprocess.run(
                [self._docker_bin, "rm", "-f", container_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self._kill_grace_s,
            )
            return result.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _capped_len(data: bytes, limit: int) -> tuple[int, bool]:
        if len(data) > limit:
            return (limit, True)
        return (len(data), False)

    @staticmethod
    def _write_capped(path: Path, data: bytes, limit: int) -> None:
        try:
            with open(path, "wb") as fh:
                fh.write(data[:limit])
        except OSError:
            pass


def _looks_like_infra(exit_code: int | None, stderr_text: str) -> bool:
    # Tool stderr is untrusted diagnostic text, not a Docker status signal.
    return exit_code in _DOCKER_DAEMON_EXIT_CODES



__all__ = [
    "DEFAULT_CPUS",
    "DEFAULT_IMAGE",
    "DEFAULT_MEMORY",
    "DEFAULT_MEMORY_SWAP",
    "DEFAULT_PIDS_LIMIT",
    "DEFAULT_SCRATCH_SIZE",
    "DEFAULT_SCRATCH_DIR",
    "DEFAULT_USER",
    "DEFAULT_WORKDIR",
    "PINNED_IMAGES",
    "ContainerMount",
    "ContainerRunner",
    "ContainerRunnerError",
    "build_docker_argv",
    "docker_available",
]
