"""Isolated episode workspaces (M0-03).

Copy a declared template directory into a fresh, uniquely-named episode
directory and enforce editable/readable path policies on every access.

Scope notes (non-goals): this is a test-isolation helper, not a security
boundary for arbitrary host code. There is no container backend, no caching,
and no shared writable workspaces. Symlinks are never followed: any access
that traverses a symlink -- inside or outside the workspace -- is rejected.

Standard library only, Python >= 3.10 compatible.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Union

PathLike = Union[str, os.PathLike]

MARKER_FILENAME = ".silicon_episode_owner"
DEFAULT_MAX_READ_BYTES = 1_000_000
DEFAULT_MAX_WRITE_BYTES = 1_000_000
DEFAULT_MAX_LIST_ENTRIES = 5_000
DEFAULT_PREFIX = "ep_"


class WorkspaceError(ValueError):
    """Raised when a workspace operation violates policy or fails."""


def _glob_to_regex(pattern: str) -> str:
    """Translate a posix glob pattern to a regex string (anchored by caller).

    ``*`` matches any run of non-separator characters, ``?`` matches one
    non-separator character, and ``**`` matches across separators. A ``**/``
    segment also matches zero directories, so ``a/**/b`` matches ``a/b``.
    ``[...]`` character classes are passed through verbatim.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 2] == "**":
                if pattern[i : i + 3] == "**/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append("\\[")
                i += 1
            else:
                out.append(pattern[i : close + 1])
                i = close + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def _compile_allowlist(patterns: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            raise WorkspaceError(
                f"allowed_edit_paths entries must be non-empty strings, got {pattern!r}"
            )
        if pattern.startswith("/") or ".." in pattern.split("/"):
            raise WorkspaceError(
                f"allowed_edit_paths entry {pattern!r} must be a workspace-relative "
                "pattern (no absolute paths or '..' segments)"
            )
        normalized = pattern.strip()
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized:
            raise WorkspaceError(
                f"allowed_edit_paths entries must be non-empty strings, got {pattern!r}"
            )
        compiled.append(re.compile(_glob_to_regex(normalized)))
    return tuple(compiled)


def _check_rel_syntax(rel: PathLike, *, allow_root: bool = False) -> str:
    """Validate the raw relative-path syntax; return the posix form."""
    if isinstance(rel, os.PathLike):
        rel = os.fspath(rel)
    if not isinstance(rel, str):
        raise WorkspaceError(f"path must be a string, got {type(rel).__name__}")
    if "\x00" in rel:
        raise WorkspaceError("path must not contain NUL bytes")
    if os.path.isabs(rel) or rel.startswith("/") or rel.startswith("\\"):
        raise WorkspaceError(f"absolute paths are not allowed: {rel!r}")
    # Reject Windows drive-absolute paths too (e.g. "C:\\x", "C:/x").
    if len(rel) >= 2 and rel[1] == ":" and rel[0].isalpha():
        raise WorkspaceError(f"absolute paths are not allowed: {rel!r}")
    normalized = rel.replace("\\", "/") if os.sep != "/" else rel
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in normalized.split("/")):
        raise WorkspaceError(f"path traversal is not allowed: {rel!r}")
    if not parts:
        if allow_root:
            return "."
        raise WorkspaceError(f"empty path is not allowed: {rel!r}")
    return "/".join(parts)


def _real(p: Path) -> Path:
    return Path(os.path.realpath(p))


class WorkspaceManager:
    """Owns template copies and enforces path policies for episode workspaces.

    :param root_dir: parent directory under which unique episode directories
        are created (created if missing).
    :param template_dir: declared template whose *contents* are copied into
        each fresh episode directory. Copied with symlinks preserved but
        never followed: any later access through a symlink is rejected.
    :param allowed_edit_paths: workspace-relative glob patterns for files the
        agent may write. Everything else is a protected (read-only) input.
    :param max_read_bytes: default bound for ``read_bytes``/``read_text``.
    :param max_write_bytes: default bound for ``write_bytes``/``write_text``.
    :param max_list_entries: bound for ``list_dir`` entry counts.
    :param prefix: filename prefix for generated episode directories.
    """

    def __init__(
        self,
        *,
        root_dir: PathLike,
        template_dir: PathLike,
        allowed_edit_paths: Iterable[str] = (),
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
        max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES,
        max_list_entries: int = DEFAULT_MAX_LIST_ENTRIES,
        prefix: str = DEFAULT_PREFIX,
    ) -> None:
        if isinstance(root_dir, os.PathLike):
            root_dir = os.fspath(root_dir)
        if isinstance(template_dir, os.PathLike):
            template_dir = os.fspath(template_dir)
        if not isinstance(root_dir, str) or not root_dir.strip():
            raise WorkspaceError("root_dir must be a non-empty path")
        if not isinstance(template_dir, str) or not template_dir.strip():
            raise WorkspaceError("template_dir must be a non-empty path")
        for name, value in (
            ("max_read_bytes", max_read_bytes),
            ("max_write_bytes", max_write_bytes),
            ("max_list_entries", max_list_entries),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise WorkspaceError(f"{name} must be a positive int, got {value!r}")
        if not isinstance(prefix, str) or not prefix:
            raise WorkspaceError("prefix must be a non-empty string")

        template = Path(template_dir)
        if not template.is_dir() or template.is_symlink():
            raise WorkspaceError(f"template_dir must be an existing directory: {template}")

        root = Path(root_dir)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(f"cannot create root_dir {root}: {exc}") from exc
        if not root.is_dir() or root.is_symlink():
            raise WorkspaceError(f"root_dir must be a directory: {root}")

        self._root = root.resolve()
        self._template = template.resolve()
        self._allowlist_src = tuple(allowed_edit_paths)
        self._allowlist = _compile_allowlist(self._allowlist_src)
        self._max_read_bytes = max_read_bytes
        self._max_write_bytes = max_write_bytes
        self._max_list_entries = max_list_entries
        self._prefix = prefix
        self._token = uuid.uuid4().hex
        self._owned: set[Path] = set()

    @property
    def root_dir(self) -> Path:
        return self._root

    @property
    def template_dir(self) -> Path:
        return self._template

    @property
    def allowed_edit_paths(self) -> tuple[str, ...]:
        return self._allowlist_src

    # -- policy ---------------------------------------------------------

    def is_editable(self, rel: PathLike) -> bool:
        """Return True when ``rel`` matches the editable-file allowlist."""
        posix = _check_rel_syntax(rel)
        return any(rx.fullmatch(posix) for rx in self._allowlist)

    # -- path resolution -------------------------------------------------

    def _resolve(self, workspace_root: Path, rel: PathLike, *, allow_root: bool = False) -> Path:
        """Resolve ``rel`` inside ``workspace_root`` with canonical validation.

        Rejects absolute paths, ``..`` traversal, symlink escapes, and any
        path whose final target or intermediate component is a symlink.
        """
        posix = _check_rel_syntax(rel, allow_root=allow_root)
        root_real = _real(workspace_root)
        if posix == ".":
            candidate = root_real
        else:
            candidate = root_real.joinpath(*posix.split("/"))
        # Refuse to traverse symlinks at all (symlink escapes included).
        self._ensure_no_symlinks(root_real, candidate, rel)
        # Canonical containment backstop (resolves any residual symlinks,
        # e.g. a symlinked workspace root component).
        target_real = _real(candidate)
        try:
            target_real.relative_to(root_real)
        except ValueError:
            raise WorkspaceError(f"path escapes workspace root: {rel!r}") from None
        return target_real if posix == "." else root_real.joinpath(*posix.split("/"))

    @staticmethod
    def _ensure_no_symlinks(root_real: Path, candidate: Path, rel: PathLike) -> None:
        current = root_real
        parts: tuple[str, ...] = (
            ()
            if str(candidate) == str(root_real)
            else tuple(candidate.relative_to(root_real).parts)
        )
        for part in parts:
            current = current / part
            if os.path.islink(current):
                raise WorkspaceError(f"symlink access is not allowed: {rel!r}")

    # -- lifecycle -------------------------------------------------------

    def reset(self) -> EpisodeWorkspace:
        """Create a fresh episode directory from the template.

        On copy failure the partial directory is removed and a
        ``WorkspaceError`` is raised, leaving no stray episode dirs behind.
        """
        dest = self._root / f"{self._prefix}{uuid.uuid4().hex[:16]}"
        try:
            dest.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            raise WorkspaceError(f"cannot create episode directory {dest}: {exc}") from exc
        try:
            shutil.copytree(self._template, dest, symlinks=True, dirs_exist_ok=True)
        except Exception as exc:
            shutil.rmtree(dest, ignore_errors=True)
            self._owned.discard(dest.resolve() if dest.exists() else dest)
            raise WorkspaceError(f"failed to copy template into {dest}: {exc}") from exc
        try:
            (dest / MARKER_FILENAME).write_text(self._token, encoding="utf-8")
        except OSError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise WorkspaceError(f"failed to mark episode directory {dest}: {exc}") from exc
        resolved = dest.resolve()
        self._owned.add(resolved)
        digest = self.hash_immutable_inputs(resolved)
        return EpisodeWorkspace(root=resolved, immutable_hash=digest, manager=self)

    def hash_immutable_inputs(self, workspace: Union[EpisodeWorkspace, PathLike]) -> str:
        """Hash every protected (non-editable) regular file deterministically.

        Walks the workspace in sorted order; editable files, the ownership
        marker, and directory structure names do not contribute -- only the
        relative path and bytes of each protected file (or symlink target
        string for symlinks, which are never followed).
        """
        root = workspace.root if isinstance(workspace, EpisodeWorkspace) else Path(workspace)  # type: ignore[arg-type]
        root_real = _real(root)
        if not root_real.is_dir():
            raise WorkspaceError(f"workspace is not a directory: {root}")
        digest = hashlib.sha256()
        for dirpath, dirnames, filenames in os.walk(root_real, topdown=True, followlinks=False):
            dirnames.sort()
            # Never descend into symlinked directories.
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root_real).replace(os.sep, "/")
                if rel == MARKER_FILENAME:
                    continue
                if self._matches_allowlist(rel):
                    continue  # editable outputs do not affect the input hash
                digest.update(rel.encode("utf-8"))
                digest.update(b"\x00")
                if os.path.islink(full):
                    digest.update(b"link:\x00")
                    digest.update(os.readlink(full).encode("utf-8", errors="surrogateescape"))
                else:
                    with open(full, "rb") as fh:
                        for chunk in iter(lambda: fh.read(65536), b""):
                            digest.update(chunk)
                digest.update(b"\x00")
        return digest.hexdigest()

    def _matches_allowlist(self, rel_posix: str) -> bool:
        return any(rx.fullmatch(rel_posix) for rx in self._allowlist)

    # -- bounded helpers ---------------------------------------------------

    def read_bytes(
        self,
        workspace: Union[EpisodeWorkspace, PathLike],
        rel: PathLike,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        root = self._coerce_root(workspace)
        limit = self._coerce_limit(max_bytes, self._max_read_bytes, "max_bytes")
        target = self._resolve(root, rel)
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise WorkspaceError(f"cannot read {rel!r}: {exc}") from exc
        if not target.is_file():
            raise WorkspaceError(f"not a regular file: {rel!r}")
        if size > limit:
            raise WorkspaceError(
                f"file {rel!r} is {size} bytes, exceeding the {limit}-byte read limit"
            )
        try:
            with open(target, "rb") as fh:
                data = fh.read(limit + 1)
        except OSError as exc:
            raise WorkspaceError(f"cannot read {rel!r}: {exc}") from exc
        if len(data) > limit:
            raise WorkspaceError(f"file {rel!r} exceeds the {limit}-byte read limit")
        return data

    def read_text(
        self,
        workspace: Union[EpisodeWorkspace, PathLike],
        rel: PathLike,
        *,
        max_bytes: int | None = None,
        encoding: str = "utf-8",
    ) -> str:
        data = self.read_bytes(workspace, rel, max_bytes=max_bytes)
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            raise WorkspaceError(f"cannot decode {rel!r} as {encoding}: {exc}") from exc

    def write_bytes(
        self,
        workspace: Union[EpisodeWorkspace, PathLike],
        rel: PathLike,
        data: bytes | bytearray,
        *,
        max_bytes: int | None = None,
    ) -> None:
        root = self._coerce_root(workspace)
        if not isinstance(data, (bytes, bytearray)):
            raise WorkspaceError(f"data must be bytes, got {type(data).__name__}")
        limit = self._coerce_limit(max_bytes, self._max_write_bytes, "max_bytes")
        if len(data) > limit:
            raise WorkspaceError(
                f"write of {len(data)} bytes to {rel!r} exceeds the {limit}-byte limit"
            )
        posix = _check_rel_syntax(rel)
        if not self._matches_allowlist(posix):
            raise WorkspaceError(
                f"write to protected input is not allowed: {rel!r} "
                f"(allowed_edit_paths={list(self._allowlist_src)})"
            )
        target = self._resolve(root, rel)
        if os.path.lexists(target) and not target.is_file():
            # Covers pre-existing symlinks (rejected above) and directories.
            raise WorkspaceError(f"cannot overwrite non-file: {rel!r}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(f"cannot create parent dirs for {rel!r}: {exc}") from exc
        # Re-validate parents after creation (a concurrent/malicious symlink
        # must not redirect the write outside the workspace).
        self._resolve(root, posix.rsplit("/", 1)[0] if "/" in posix else ".", allow_root=True)
        if os.path.islink(target):
            raise WorkspaceError(f"symlink access is not allowed: {rel!r}")
        if _real(target.parent) != target.parent.resolve():
            raise WorkspaceError(f"path escapes workspace root: {rel!r}")
        try:
            with open(target, "wb") as fh:
                fh.write(bytes(data))
        except OSError as exc:
            raise WorkspaceError(f"cannot write {rel!r}: {exc}") from exc

    def write_text(
        self,
        workspace: Union[EpisodeWorkspace, PathLike],
        rel: PathLike,
        text: str,
        *,
        max_bytes: int | None = None,
        encoding: str = "utf-8",
    ) -> None:
        if not isinstance(text, str):
            raise WorkspaceError(f"text must be a string, got {type(text).__name__}")
        try:
            data = text.encode(encoding)
        except (UnicodeEncodeError, LookupError) as exc:
            raise WorkspaceError(f"cannot encode text as {encoding}: {exc}") from exc
        self.write_bytes(workspace, rel, data, max_bytes=max_bytes)

    def list_dir(
        self,
        workspace: Union[EpisodeWorkspace, PathLike],
        rel: PathLike = ".",
        *,
        max_entries: int | None = None,
    ) -> list[str]:
        root = self._coerce_root(workspace)
        limit = self._coerce_limit(max_entries, self._max_list_entries, "max_entries")
        target = self._resolve(root, rel, allow_root=True)
        if not target.is_dir():
            raise WorkspaceError(f"not a directory: {rel!r}")
        try:
            entries = sorted(os.listdir(target))
        except OSError as exc:
            raise WorkspaceError(f"cannot list {rel!r}: {exc}") from exc
        if len(entries) > limit:
            raise WorkspaceError(
                f"directory {rel!r} has {len(entries)} entries, exceeding the "
                f"{limit}-entry list limit"
            )
        return entries

    # -- cleanup -----------------------------------------------------------

    def cleanup(self, workspace: Union[EpisodeWorkspace, PathLike]) -> None:
        """Remove an episode directory owned by this manager.

        Refuses to touch anything that is not a directory inside ``root_dir``
        carrying this manager's ownership marker, so stray or foreign paths
        are never deleted.
        """
        raw = workspace.root if isinstance(workspace, EpisodeWorkspace) else Path(workspace)  # type: ignore[arg-type]
        candidate_real = _real(Path(raw))
        root_real = _real(self._root)
        if candidate_real == root_real:
            raise WorkspaceError("refusing to remove the workspace root itself")
        try:
            candidate_real.relative_to(root_real)
        except ValueError:
            raise WorkspaceError(f"refusing to remove path outside workspace root: {raw!r}")
        if not candidate_real.is_dir() or candidate_real.is_symlink():
            raise WorkspaceError(f"not an owned episode directory: {raw!r}")
        marker = candidate_real / MARKER_FILENAME
        try:
            token = (
                marker.read_text(encoding="utf-8")
                if marker.is_file() and not marker.is_symlink()
                else ""
            )
        except OSError:
            token = ""
        if token != self._token or candidate_real not in self._owned:
            raise WorkspaceError(f"refusing to remove directory not owned by this manager: {raw!r}")
        shutil.rmtree(candidate_real, ignore_errors=False)
        self._owned.discard(candidate_real)

    def cleanup_all(self) -> None:
        """Remove every live episode directory owned by this manager."""
        for owned in sorted(self._owned, key=str):
            marker = owned / MARKER_FILENAME
            if owned.is_dir() and marker.is_file():
                try:
                    if marker.read_text(encoding="utf-8") == self._token:
                        shutil.rmtree(owned, ignore_errors=False)
                except OSError as exc:
                    raise WorkspaceError(f"failed to remove {owned}: {exc}") from exc
        self._owned.clear()

    # -- internals -----------------------------------------------------------

    def _coerce_root(self, workspace: Union[EpisodeWorkspace, PathLike]) -> Path:
        if isinstance(workspace, EpisodeWorkspace):
            return workspace.root
        if isinstance(workspace, os.PathLike):
            return Path(os.fspath(workspace))
        if isinstance(workspace, str):
            return Path(workspace)
        raise WorkspaceError(
            f"workspace must be an EpisodeWorkspace or path, got {type(workspace).__name__}"
        )

    @staticmethod
    def _coerce_limit(value: int | None, default: int, name: str) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise WorkspaceError(f"{name} must be a positive int, got {value!r}")
        return value


@dataclass
class EpisodeWorkspace:
    """A single fresh episode directory plus its immutable-input hash."""

    root: Path
    immutable_hash: str
    manager: WorkspaceManager = field(repr=False, compare=False)

    def read_bytes(self, rel: PathLike, *, max_bytes: int | None = None) -> bytes:
        return self.manager.read_bytes(self, rel, max_bytes=max_bytes)

    def read_text(self, rel: PathLike, *, max_bytes: int | None = None) -> str:
        return self.manager.read_text(self, rel, max_bytes=max_bytes)

    def write_bytes(
        self, rel: PathLike, data: bytes | bytearray, *, max_bytes: int | None = None
    ) -> None:
        self.manager.write_bytes(self, rel, data, max_bytes=max_bytes)

    def write_text(self, rel: PathLike, text: str, *, max_bytes: int | None = None) -> None:
        self.manager.write_text(self, rel, text, max_bytes=max_bytes)

    def list_dir(self, rel: PathLike = ".", *, max_entries: int | None = None) -> list[str]:
        return self.manager.list_dir(self, rel, max_entries=max_entries)

    def is_editable(self, rel: PathLike) -> bool:
        return self.manager.is_editable(rel)

    def verify_immutable_hash(self) -> bool:
        """Re-hash protected inputs and compare against the reset-time hash."""
        return self.manager.hash_immutable_inputs(self) == self.immutable_hash

    def cleanup(self) -> None:
        self.manager.cleanup(self)


__all__ = [
    "DEFAULT_MAX_LIST_ENTRIES",
    "DEFAULT_MAX_READ_BYTES",
    "DEFAULT_MAX_WRITE_BYTES",
    "MARKER_FILENAME",
    "EpisodeWorkspace",
    "WorkspaceError",
    "WorkspaceManager",
]
