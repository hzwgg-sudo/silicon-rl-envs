"""M0-03 workspace tests: isolation, failed-copy cleanup, traversal,
symlink escapes, oversized reads, and protected-file edits."""

import os
from pathlib import Path

import pytest

from silicon_env.workspace import (
    EpisodeWorkspace,
    WorkspaceError,
    WorkspaceManager,
)


def make_template(path: Path) -> Path:
    (path / "design").mkdir(parents=True)
    (path / "src").mkdir(parents=True)
    (path / "design" / "top.sdc").write_text("clock 1.0\n", encoding="utf-8")
    (path / "design" / "constraints.sdc").write_text("clock 2.0\n", encoding="utf-8")
    (path / "src" / "top.v").write_text("module top; endmodule\n", encoding="utf-8")
    (path / "README.txt").write_text("do not edit\n", encoding="utf-8")
    return path


def make_manager(tmp_path: Path, **kwargs) -> WorkspaceManager:
    template = make_template(tmp_path / "template")
    kwargs.setdefault("allowed_edit_paths", ("design/*.sdc", "src/*.v"))
    return WorkspaceManager(root_dir=tmp_path / "episodes", template_dir=template, **kwargs)


# --- reset isolation -----------------------------------------------------


def test_two_resets_are_independent_with_identical_immutable_hashes(tmp_path):
    mgr = make_manager(tmp_path)
    ws1 = mgr.reset()
    ws2 = mgr.reset()
    try:
        assert isinstance(ws1, EpisodeWorkspace)
        assert ws1.root != ws2.root
        assert ws1.root.is_dir() and ws2.root.is_dir()
        # Same template -> identical immutable input hashes.
        assert ws1.immutable_hash == ws2.immutable_hash
        assert ws1.verify_immutable_hash()
        assert ws2.verify_immutable_hash()

        ws1.write_text("design/top.sdc", "edited by ws1\n")
        assert ws1.read_text("design/top.sdc") == "edited by ws1\n"
        # The sibling workspace is unaffected.
        assert ws2.read_text("design/top.sdc") == "clock 1.0\n"

        # Editable outputs do not perturb the immutable hash ...
        assert ws1.verify_immutable_hash()
        assert mgr.hash_immutable_inputs(ws1.root) == mgr.hash_immutable_inputs(ws2.root)
        # ... but tampering with a protected input is detected.
        (ws1.root / "README.txt").write_text("tampered\n", encoding="utf-8")
        assert not ws1.verify_immutable_hash()
        # ... and differ per workspace on disk.
        assert (ws1.root / "design" / "top.sdc").read_text() != (
            ws2.root / "design" / "top.sdc"
        ).read_text()
    finally:
        mgr.cleanup_all()


def test_reset_copies_full_template(tmp_path):
    mgr = make_manager(tmp_path)
    ws = mgr.reset()
    try:
        assert ws.read_text("README.txt") == "do not edit\n"
        assert ws.read_text("src/top.v") == "module top; endmodule\n"
        assert sorted(ws.list_dir("design")) == ["constraints.sdc", "top.sdc"]
    finally:
        mgr.cleanup_all()


def test_failed_copy_cleans_up_partial_dir(tmp_path, monkeypatch):
    mgr = make_manager(tmp_path)
    before = set((tmp_path / "episodes").iterdir()) if (tmp_path / "episodes").exists() else set()

    import shutil

    real_copytree = shutil.copytree

    def boom(*args, **kwargs):
        # Simulate a partial copy, then fail.
        dest = Path(args[1])
        (dest / "partial.txt").write_text("partial", encoding="utf-8")
        raise OSError("disk exploded")

    monkeypatch.setattr(shutil, "copytree", boom)
    with pytest.raises(WorkspaceError, match="failed to copy template"):
        mgr.reset()
    # No stray episode directories remain.
    after = set((tmp_path / "episodes").iterdir())
    assert after == before
    monkeypatch.setattr(shutil, "copytree", real_copytree)


# --- path policy ----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/hosts",
        "/tmp/x",
        "../outside.txt",
        "a/../../b.txt",
        "design/../../../etc/passwd",
        "..",
        "design/..",
    ],
)
def test_traversal_and_absolute_paths_rejected(tmp_path, bad):
    mgr = make_manager(tmp_path)
    ws = mgr.reset()
    try:
        with pytest.raises(WorkspaceError):
            ws.read_text(bad)
        with pytest.raises(WorkspaceError):
            ws.write_text(bad, "x")
        with pytest.raises(WorkspaceError):
            ws.list_dir(bad)
        with pytest.raises(WorkspaceError):
            mgr.is_editable(bad)
    finally:
        mgr.cleanup_all()


def test_absolute_path_object_rejected(tmp_path):
    mgr = make_manager(tmp_path)
    ws = mgr.reset()
    try:
        with pytest.raises(WorkspaceError):
            ws.read_text(Path("/etc/hosts"))
    finally:
        mgr.cleanup_all()


def test_symlink_escape_rejected(tmp_path):
    mgr = make_manager(tmp_path, allowed_edit_paths=("**",))
    ws = mgr.reset()
    try:
        outside = tmp_path / "secret.txt"
        outside.write_text("s3cr3t", encoding="utf-8")
        os.symlink(outside, ws.root / "link.txt")
        os.symlink(tmp_path, ws.root / "linkdir")
        with pytest.raises(WorkspaceError, match="symlink"):
            ws.read_text("link.txt")
        with pytest.raises(WorkspaceError, match="symlink"):
            ws.write_text("link.txt", "x")
        with pytest.raises(WorkspaceError, match="symlink"):
            ws.read_text("linkdir/secret.txt")
        with pytest.raises(WorkspaceError, match="symlink"):
            ws.list_dir("linkdir")
    finally:
        mgr.cleanup_all()


def test_oversized_read_rejected(tmp_path):
    mgr = make_manager(tmp_path, max_read_bytes=16)
    ws = mgr.reset()
    try:
        with pytest.raises(WorkspaceError, match="exceeding|exceeds"):
            ws.read_text("src/top.v")
        with pytest.raises(WorkspaceError, match="exceeding|exceeds"):
            ws.read_bytes("src/top.v")
        # Small files still read fine.
        assert ws.read_text("README.txt", max_bytes=1024) == "do not edit\n"
    finally:
        mgr.cleanup_all()


def test_protected_file_edits_rejected_but_editable_allowed(tmp_path):
    mgr = make_manager(tmp_path)
    ws = mgr.reset()
    try:
        with pytest.raises(WorkspaceError, match="protected"):
            ws.write_text("README.txt", "hacked")
        with pytest.raises(WorkspaceError, match="protected"):
            ws.write_bytes("design", b"x")
        with pytest.raises(WorkspaceError, match="protected"):
            ws.write_text("other/new.txt", "x")
        # Allowlisted paths are writable, including new files in those dirs.
        ws.write_text("design/top.sdc", "edited\n")
        assert ws.read_text("design/top.sdc") == "edited\n"
        ws.write_text("src/new.v", "module new; endmodule\n")
        assert ws.read_text("src/new.v") == "module new; endmodule\n"
        # Glob '*' does not cross directories: nested files stay protected.
        with pytest.raises(WorkspaceError, match="protected"):
            ws.write_text("design/sub/nested.sdc", "x")
    finally:
        mgr.cleanup_all()


def test_star_star_allows_nested_paths(tmp_path):
    mgr = make_manager(tmp_path, allowed_edit_paths=("design/**",))
    ws = mgr.reset()
    try:
        ws.write_text("design/sub/nested.sdc", "x")
        assert ws.read_text("design/sub/nested.sdc") == "x"
    finally:
        mgr.cleanup_all()


def test_oversized_write_rejected(tmp_path):
    mgr = make_manager(tmp_path, max_write_bytes=4)
    ws = mgr.reset()
    try:
        with pytest.raises(WorkspaceError, match="exceeds"):
            ws.write_text("design/top.sdc", "way too long")
        ws.write_text("design/top.sdc", "ok\n")
        assert ws.read_text("design/top.sdc") == "ok\n"
    finally:
        mgr.cleanup_all()


def test_list_bound_enforced(tmp_path):
    mgr = make_manager(tmp_path)
    ws = mgr.reset()
    try:
        with pytest.raises(WorkspaceError, match="exceeding"):
            ws.list_dir(".", max_entries=1)
        assert ws.list_dir(".", max_entries=100) != []
    finally:
        mgr.cleanup_all()


# --- cleanup ownership ----------------------------------------------------


def test_cleanup_removes_owned_dir_only(tmp_path):
    mgr = make_manager(tmp_path)
    ws = mgr.reset()
    root = ws.root
    assert root.is_dir()
    ws.cleanup()
    assert not root.exists()
    # Foreign directories are never removed.
    foreign = tmp_path / "not-an-episode"
    foreign.mkdir()
    (foreign / "data.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="refusing"):
        mgr.cleanup(foreign)
    assert (foreign / "data.txt").read_text() == "keep"
    # The workspace root itself is never removed.
    with pytest.raises(WorkspaceError, match="refusing"):
        mgr.cleanup(mgr.root_dir)
    # Paths outside the root are never removed.
    with pytest.raises(WorkspaceError, match="refusing"):
        mgr.cleanup(tmp_path / "template")


def test_cleanup_all_removes_everything_owned(tmp_path):
    mgr = make_manager(tmp_path)
    ws1 = mgr.reset()
    ws2 = mgr.reset()
    mgr.cleanup_all()
    assert not ws1.root.exists()
    assert not ws2.root.exists()
    assert list(mgr.root_dir.iterdir()) == []


def test_read_only_task_has_no_editable_paths(tmp_path):
    mgr = make_manager(tmp_path, allowed_edit_paths=())
    ws = mgr.reset()
    try:
        assert not ws.is_editable("design/top.sdc")
        with pytest.raises(WorkspaceError, match="protected"):
            ws.write_text("design/top.sdc", "x")
        # Reads still work.
        assert ws.read_text("design/top.sdc") == "clock 1.0\n"
    finally:
        mgr.cleanup_all()
