"""Replayable episode traces and run manifests (M0-07).

A versioned JSONL event stream (one record per ``reset``/``step``/``submit``)
plus a final ``manifest.json`` that binds the episode to its inputs, budgets,
toolchain identity, and artifact hashes.

Layout under a per-episode run directory::

    <work_root>/runs/<run_id>/trace.jsonl
    <work_root>/runs/<run_id>/manifest.json
    <work_root>/runs/<run_id>/artifacts/step_<n>/stdout.log  (tool logs, optional)

Design rules:

- ``trace.jsonl`` is append-only; every line is strict JSON (no NaN/Infinity).
- Artifact references stored in events/manifest are workspace-relative posix
  paths (never absolute host paths). Tool logs produced outside the run dir
  are *copied* in so refs never dangle after workspace cleanup.
- Timing fields (``timestamp_s``, ``duration_s``, ``elapsed_s``,
  ``remaining_wallclock_s``) are recorded for diagnostics but excluded from
  the *semantic projection*, which is stable across runs with different
  clocks. ``semantic_hash`` in the manifest binds the semantic content.
- Secret-looking mapping keys (``API_KEY``/``TOKEN``/``SECRET``/...) have
  their values replaced with ``***REDACTED***``; inline ``key=value`` secrets
  in free text become ``key=<REDACTED>``; known absolute host roots are
  replaced with ``<HOST_PATH>``.
- ``manifest.json`` is written atomically (tmp file + ``os.replace``).
- Episodes closed without ``submit`` finalize as ``status="incomplete"``
  with ``passed=False`` and ``complete=False`` -- never successful.

Stdlib-only, Python >= 3.10.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from silicon_env.types import ContractError, dumps_strict, loads_strict

TRACE_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
TRACE_FILENAME = "trace.jsonl"
MANIFEST_FILENAME = "manifest.json"
ARTIFACTS_DIRNAME = "artifacts"

REDACTED_VALUE = "***REDACTED***"
HOST_PATH_PLACEHOLDER = "<HOST_PATH>"

#: Mapping keys matching this pattern have their values redacted.
SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|token|secret|password|passwd|private[_-]?key|auth|bearer|session)",
    re.IGNORECASE,
)

#: Inline ``key: value`` / ``key=value`` secrets inside free text.
SECRET_INLINE_RE = re.compile(
    r"(?i)\b(api_key|apikey|token|secret|password|passwd|private_key|auth_token|bearer)"
    r"\s*[:=]\s*(\S+)"
)

#: Nondeterministic timing keys stripped by the semantic projection.
TIMING_KEYS = frozenset(
    {"timestamp_s", "duration_s", "elapsed_s", "remaining_wallclock_s"}
)

#: Identity keys stripped alongside timing: the run id names one recording,
#: not the episode semantics being replayed.
SEMANTIC_DROP_KEYS = TIMING_KEYS | frozenset({"run_id"})

WallClockFn = Callable[[], float]


class TraceError(ValueError):
    """Raised when a trace or manifest fails structural/hash verification."""


# -- hashing ---------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    """Hex SHA-256 of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Hex SHA-256 of a file's bytes (streamed)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(payload: Any) -> str:
    """Hex SHA-256 of the canonical strict-JSON encoding of ``payload``."""
    return sha256_bytes(dumps_strict(payload).encode("utf-8"))


def task_hash_for(task: Any) -> str:
    """Hash the canonical JSON of a :class:`TaskSpec` (validates first)."""
    if task is None or not hasattr(task, "to_dict"):
        raise TraceError("task must be a TaskSpec with to_dict()")
    task.validate()
    return sha256_json(task.to_dict())


# -- redaction --------------------------------------------------------------


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Redact secret-looking keys; inline secrets inside string values too."""
    out: dict[str, Any] = {}
    for key, value in dict(data).items():
        if isinstance(key, str) and SECRET_KEY_RE.search(key):
            out[key] = REDACTED_VALUE
        elif isinstance(value, str):
            out[key] = redact_text(value)
        elif isinstance(value, Mapping):
            out[key] = redact_mapping(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [redact_mapping({"value": v})["value"] for v in value]
        else:
            out[key] = value
    return out


def redact_text(text: Any, *, extra_roots: tuple[str, ...] = ()) -> str:
    """Redact inline secrets and known absolute host roots in free text."""
    coerced = text if isinstance(text, str) else str(text)
    redacted = SECRET_INLINE_RE.sub(r"\1=<REDACTED>", coerced)
    for key, value in os.environ.items():
        if value and SECRET_KEY_RE.search(key):
            if len(value) >= 8:
                redacted = redacted.replace(value, REDACTED_VALUE)
            else:
                redacted = re.sub(r"(?<!\w)" + re.escape(value) + r"(?!\w)",
                                  REDACTED_VALUE, redacted)
    for root in sorted(set(extra_roots + _redact_roots()), key=len, reverse=True):
        if root and isinstance(root, str) and root in redacted:
            redacted = redacted.replace(root, HOST_PATH_PLACEHOLDER)
    home = os.path.expanduser("~")
    if home and home != "/" and home in redacted:
        redacted = redacted.replace(home, HOST_PATH_PLACEHOLDER)
    return redacted


def _redact_roots() -> tuple[str, ...]:
    """Absolute host prefixes worth scrubbing (cwd; TMPDIR when absolute)."""
    roots: list[str] = []
    try:
        cwd = os.path.abspath(os.getcwd())
    except OSError:
        cwd = ""
    if cwd:
        roots.append(cwd)
    for var in ("TMPDIR", "TEMP", "TMP", "SILICON_EVAL_SECRETS_DIR"):
        candidate = os.environ.get(var, "")
        if candidate.startswith("/") and candidate not in roots:
            roots.append(candidate)
    return tuple(roots)


# -- semantic projection ----------------------------------------------------


def semantic_projection_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``event`` minus timing/identity fields (recursive for timing)."""

    def _strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: _strip(v) for k, v in value.items() if k not in TIMING_KEYS}
        if isinstance(value, list):
            return [_strip(item) for item in value]
        return value

    projected = _strip(dict(event))
    projected.pop("run_id", None)
    return projected


def semantic_projection_trace(events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project a whole event list to its timing-free semantic form."""
    return [semantic_projection_event(event) for event in events]


def semantic_hash_for(events: list[Mapping[str, Any]]) -> str:
    """Stable hash of the semantic projection (timing excluded)."""
    return sha256_json(semantic_projection_trace(events))


# -- low-level readers ------------------------------------------------------


def read_events(trace_path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Parse a JSONL trace file; every line must be a strict-JSON object."""
    path = Path(trace_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TraceError(f"cannot read trace {path}: {exc}") from exc
    events: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = loads_strict(line)
        except ContractError as exc:
            raise TraceError(f"trace line {lineno} is not strict JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise TraceError(f"trace line {lineno} must decode to an object")
        events.append(payload)
    seqs = [e.get("seq") for e in events]
    if seqs != list(range(len(events))):
        raise TraceError(f"trace seqs must be ordered 0..n-1, got {seqs!r}")
    return events


def read_manifest(manifest_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Parse + minimally validate a manifest file."""
    path = Path(manifest_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TraceError(f"cannot read manifest {path}: {exc}") from exc
    try:
        payload = loads_strict(text)
    except ContractError as exc:
        raise TraceError(f"manifest is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TraceError("manifest must decode to an object")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise TraceError(
            f"unsupported manifest schema_version {payload.get('schema_version')!r}"
        )
    for field_name in (
        "run_id",
        "task_id",
        "task_version",
        "task_hash",
        "seed",
        "status",
        "trace_file",
    ):
        if field_name not in payload:
            raise TraceError(f"manifest is missing required field {field_name!r}")
    if payload["status"] == "incomplete" and payload.get("passed") is True:
        raise TraceError("incomplete runs must never be marked passed")
    return payload


def verify_run(run_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify trace parseability + artifact refs + hashes for a run dir.

    Checks: manifest exists and validates; trace parses with ordered seqs;
    every artifact ref is relative (no absolute paths, no ``..``) and
    resolves to an existing file under ``run_dir`` whose SHA-256 matches;
    incomplete runs are never marked passed. Returns the manifest dict.
    """
    root = Path(run_dir)
    manifest = read_manifest(root / MANIFEST_FILENAME)
    trace_rel = manifest["trace_file"]
    if not isinstance(trace_rel, str) or os.path.isabs(trace_rel) or ".." in trace_rel.split("/"):
        raise TraceError(f"manifest trace_file ref must be relative, got {trace_rel!r}")
    trace_path = root / trace_rel
    if not trace_path.is_file() or trace_path.is_symlink():
        raise TraceError(f"dangling trace ref: {trace_rel!r}")
    events = read_events(trace_path)
    if semantic_hash_for(events) != manifest.get("semantic_hash"):
        raise TraceError("trace semantic hash mismatch")
    if manifest.get("events") != len(events):
        raise TraceError("trace event count mismatch")
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise TraceError("manifest artifacts must be a list")
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise TraceError("manifest artifact entries must be objects")
        rel = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(rel, str) or not rel or os.path.isabs(rel):
            raise TraceError(f"artifact ref must be a relative path, got {rel!r}")
        if ".." in Path(rel).parts:
            raise TraceError(f"artifact ref must not escape the run dir: {rel!r}")
        target = root / rel
        if (not target.is_file() or target.is_symlink()
                or root.resolve() not in target.resolve().parents):
            raise TraceError(f"dangling artifact ref: {rel!r}")
        actual = sha256_file(target)
        if actual != expected:
            raise TraceError(
                f"artifact hash mismatch for {rel!r}: expected {expected}, got {actual}"
            )
    declared = {entry["path"] for entry in artifacts}
    for event in events:
        for ref in event.get("artifact_refs", []):
            if ref not in declared:
                raise TraceError(f"dangling artifact ref in trace: {ref!r}")
    return manifest


# -- recorder ---------------------------------------------------------------


class TraceRecorder:
    """Append-only JSONL recorder plus atomic manifest finalization.

    :param run_dir: per-episode directory (created if missing).
    :param task: validated :class:`TaskSpec` for task/seed/toolchain hashes.
    :param seed: effective episode seed (may override ``task.seed``).
    :param run_id: stable id; defaults to the run dir name.
    :param clock: wallclock seconds source for ``timestamp_s`` (diagnostic
        only; excluded from the semantic projection).
    """

    def __init__(
        self,
        run_dir: str | os.PathLike[str],
        *,
        task: Any,
        seed: int | None = None,
        run_id: str | None = None,
        runner_provenance: Mapping[str, Any] | None = None,
        clock: WallClockFn | None = None,
    ) -> None:
        if task is None or not hasattr(task, "to_dict"):
            raise TraceError("task must be a TaskSpec")
        task.validate()
        root = Path(run_dir)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TraceError(f"cannot create run_dir {root}: {exc}") from exc
        if not root.is_dir():
            raise TraceError(f"run_dir must be a directory: {root}")
        artifacts = root / ARTIFACTS_DIRNAME
        try:
            artifacts.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TraceError(f"cannot create artifacts dir {artifacts}: {exc}") from exc
        self._run_dir = root
        self._task = task
        self._runner_provenance = redact_mapping(dict(runner_provenance or {}))
        self._seed = task.seed if seed is None else seed
        if isinstance(self._seed, bool) or not isinstance(self._seed, int):
            raise TraceError("seed must be an int")
        self._run_id = run_id or root.name or uuid.uuid4().hex[:12]
        self._clock: WallClockFn = clock if clock is not None else time.time
        if not callable(self._clock):
            raise TraceError("clock must be callable")
        self._task_hash = task_hash_for(task)
        self._trace_path = root / TRACE_FILENAME
        self._manifest_path = root / MANIFEST_FILENAME
        self._seq = 0
        self._steps = 0
        self._finalized = False
        self._artifacts: list[dict[str, Any]] = []
        self._redact_roots = _redact_roots() + (str(root),)
        try:
            self._trace_path.touch(exist_ok=True)
        except OSError as exc:
            raise TraceError(f"cannot create trace file {self._trace_path}: {exc}") from exc

    # -- views ----------------------------------------------------------

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def task_hash(self) -> str:
        return self._task_hash

    @property
    def trace_path(self) -> Path:
        return self._trace_path

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    @property
    def finalized(self) -> bool:
        return self._finalized

    # -- events ----------------------------------------------------------

    def record_reset(
        self,
        observation: Any,
        budget_snapshot: Mapping[str, Any] | None = None,
        *,
        immutable_hash: str | None = None,
    ) -> dict[str, Any]:
        """Record the reset event (seq 0) with the initial observation."""
        obs = observation.to_dict() if hasattr(observation, "to_dict") else dict(observation)
        event = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "seq": self._seq,
            "type": "reset",
            "run_id": self._run_id,
            "task_id": self._task.task_id,
            "task_version": self._task.task_version,
            "task_hash": self._task_hash,
            "seed": self._seed,
            "toolchain_refs": dict(self._task.toolchain_refs),
            "runner_provenance": self._runner_provenance,
            "grader_id": self._task.grader.grader_id,
            "grader_version": self._task.grader.grader_version,
            "observation": self._summarize_observation(obs),
            "budget": self._strip_timing(dict(budget_snapshot or {})),
            "env": redact_mapping(dict(os.environ)),
            "immutable_hash": immutable_hash or "",
            "timestamp_s": float(self._clock()),
        }
        return self._append(event)

    def record_step(
        self,
        action: Any,
        result: Any,
        budget_snapshot: Mapping[str, Any] | None = None,
        *,
        artifact_files: tuple[str | os.PathLike[str], ...] = (),
        step_label: str | None = None,
    ) -> dict[str, Any]:
        """Record one step event; copies ``artifact_files`` into the run dir."""
        try:
            action_dict = action.to_dict() if hasattr(action, "to_dict") else dict(action)
        except ContractError:
            # Invalid actions are part of the audit trail too.
            action_dict = {
                "action_type": str(getattr(action, "action_type", "invalid")),
                "params": {"invalid_repr": repr(getattr(action, "params", None))},
            }
        result_dict = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        label = step_label or f"step_{result_dict.get('step_index', self._steps + 1)}"
        refs: list[str] = []
        for src in artifact_files:
            try:
                dest_rel = f"{ARTIFACTS_DIRNAME}/{label}/{Path(src).name}"
                refs.append(self.ingest_artifact(src, dest_rel=dest_rel))
            except (OSError, TraceError):
                continue
        obs = result_dict.get("observation", {})
        event = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "seq": self._seq,
            "type": "step",
            "run_id": self._run_id,
            "step_index": result_dict.get("step_index", self._steps + 1),
            "action": {
                "action_type": action_dict.get("action_type", ""),
                "params": redact_mapping(dict(action_dict.get("params", {}))),
            },
            "status": result_dict.get("status", ""),
            "reward": result_dict.get("reward", 0.0),
            "done": bool(result_dict.get("done", False)),
            "message": redact_text(result_dict.get("message", ""), extra_roots=self._redact_roots),
            "observation": self._summarize_observation(obs),
            "artifact_refs": refs,
            "budget": self._strip_timing(dict(budget_snapshot or {})),
            "timestamp_s": float(self._clock()),
        }
        self._steps += 1
        return self._append(event)

    def record_submit(
        self,
        grade: Any,
        budget_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record the submit event with the grading outcome."""
        grade_dict = grade.to_dict() if hasattr(grade, "to_dict") else dict(grade)
        event = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "seq": self._seq,
            "type": "submit",
            "run_id": self._run_id,
            "status": grade_dict.get("status", ""),
            "score": grade_dict.get("score", 0.0),
            "passed": bool(grade_dict.get("passed", False)),
            "message": redact_text(grade_dict.get("message", ""), extra_roots=self._redact_roots),
            "metrics": grade_dict.get("metrics", []),
            "budget": self._strip_timing(dict(budget_snapshot or {})),
            "timestamp_s": float(self._clock()),
        }
        return self._append(event)

    # -- artifacts --------------------------------------------------------

    def ingest_artifact(
        self, src: str | os.PathLike[str], *, dest_rel: str
    ) -> str:
        """Copy ``src`` into the run dir at ``dest_rel``; return the rel ref."""
        if not isinstance(dest_rel, str) or not dest_rel or os.path.isabs(dest_rel):
            raise TraceError(f"artifact dest must be a relative path, got {dest_rel!r}")
        if ".." in Path(dest_rel).parts:
            raise TraceError(f"artifact dest must not escape the run dir: {dest_rel!r}")
        src_path = Path(src)
        if src_path.is_symlink():
            raise TraceError("symlink artifacts are not allowed")
        try:
            size = src_path.stat().st_size
        except OSError as exc:
            raise TraceError(f"cannot stat artifact {src_path}: {exc}") from exc
        dest = self._run_dir / dest_rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink() or self._run_dir.resolve() not in dest.resolve().parents:
                raise TraceError("artifact destination escapes run directory")
            if src_path.suffix == ".log":
                dest.write_text(
                    redact_text(src_path.read_text(encoding="utf-8", errors="replace"),
                                extra_roots=self._redact_roots), encoding="utf-8",
                )
            else:
                shutil.copyfile(src_path, dest)
        except OSError as exc:
            raise TraceError(f"cannot ingest artifact {src_path}: {exc}") from exc
        size = dest.stat().st_size
        entry = {"path": dest_rel, "sha256": sha256_file(dest), "size_bytes": size}
        self._artifacts.append(entry)
        return dest_rel

    # -- finalization ------------------------------------------------------

    def finalize(
        self,
        *,
        status: str,
        passed: bool,
        budget_snapshot: Mapping[str, Any] | None = None,
        message: str = "",
        complete: bool | None = None,
    ) -> dict[str, Any]:
        """Write ``manifest.json`` atomically; repeated calls return as-is."""
        if self._finalized:
            return read_manifest(self._manifest_path)
        if status == "incomplete" and passed:
            raise TraceError("incomplete runs must never be marked passed")
        events = read_events(self._trace_path)
        snapshot = dict(budget_snapshot or {})
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": self._run_id,
            "task_id": self._task.task_id,
            "task_version": self._task.task_version,
            "task_hash": self._task_hash,
            "seed": self._seed,
            "toolchain_refs": dict(self._task.toolchain_refs),
            "runner_provenance": self._runner_provenance,
            "grader_id": self._task.grader.grader_id,
            "grader_version": self._task.grader.grader_version,
            "budgets": self._task.budgets.to_dict(),
            "budget_snapshot": snapshot,
            "status": status,
            "passed": bool(passed),
            "complete": (status != "incomplete") if complete is None else bool(complete),
            "steps": self._steps,
            "events": len(events),
            "trace_file": TRACE_FILENAME,
            "artifacts": list(self._artifacts),
            "semantic_hash": semantic_hash_for(events),
            "message": redact_text(message, extra_roots=self._redact_roots),
        }
        if manifest["status"] == "incomplete":
            manifest["passed"] = False
            manifest["complete"] = False
        tmp = self._manifest_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(dumps_strict(manifest) + "\n", encoding="utf-8")
            os.replace(tmp, self._manifest_path)
        except OSError as exc:
            raise TraceError(f"cannot finalize manifest: {exc}") from exc
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        self._finalized = True
        return manifest

    def finalize_incomplete(
        self, reason: str = "interrupted", budget_snapshot: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Finalize an interrupted episode: incomplete, never successful."""
        return self.finalize(
            status="incomplete",
            passed=False,
            budget_snapshot=budget_snapshot,
            message=f"incomplete: {reason}",
            complete=False,
        )

    # -- internals ----------------------------------------------------------

    def _append(self, event: dict[str, Any]) -> dict[str, Any]:
        if self._finalized:
            raise TraceError("cannot append to a finalized trace")
        json.dumps(event, allow_nan=False)  # strict-JSON guard before writing
        line = dumps_strict(event)
        try:
            with open(self._trace_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
        except OSError as exc:
            raise TraceError(f"cannot append to trace: {exc}") from exc
        self._seq += 1
        return event

    def _summarize_observation(self, obs: Mapping[str, Any]) -> dict[str, Any]:
        summary = {
            "step_index": obs.get("step_index", 0),
            "tool_name": obs.get("tool_name", ""),
            "exit_code": obs.get("exit_code", 0),
            "stdout_tail": redact_text(
                obs.get("stdout_tail", ""), extra_roots=self._redact_roots
            ),
            "stderr_tail": redact_text(
                obs.get("stderr_tail", ""), extra_roots=self._redact_roots
            ),
            "timed_out": bool(obs.get("timed_out", False)),
        }
        if "duration_s" in obs:
            try:
                summary["duration_s"] = float(obs["duration_s"])
            except (TypeError, ValueError):
                summary["duration_s"] = 0.0
        return summary

    @staticmethod
    def _strip_timing(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Drop wallclock timing from budget snapshots stored in events.

        Keeps counters/limits (semantic replay data); the full snapshot with
        timing still lands in the manifest, where the semantic projection
        strips it for comparison.
        """
        return {k: v for k, v in snapshot.items() if k not in TIMING_KEYS}


__all__ = [
    "ARTIFACTS_DIRNAME",
    "HOST_PATH_PLACEHOLDER",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "REDACTED_VALUE",
    "SECRET_INLINE_RE",
    "SECRET_KEY_RE",
    "SEMANTIC_DROP_KEYS",
    "TIMING_KEYS",
    "TRACE_FILENAME",
    "TRACE_SCHEMA_VERSION",
    "TraceError",
    "TraceRecorder",
    "read_events",
    "read_manifest",
    "redact_mapping",
    "redact_text",
    "semantic_hash_for",
    "semantic_projection_event",
    "semantic_projection_trace",
    "sha256_bytes",
    "sha256_file",
    "sha256_json",
    "task_hash_for",
    "verify_run",
]
