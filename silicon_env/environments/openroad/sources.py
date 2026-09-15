"""Verify that the flow inputs are an unmodified checkout of the locked commit."""
from __future__ import annotations

import subprocess
from pathlib import Path


def verify_checkout(root: Path, commit: str) -> list[str]:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            timeout=30, check=True,
        )
        return result.stdout.strip()

    try:
        if git("rev-parse", "--show-toplevel") != str(root.resolve()):
            return ["ORFS checkout must be its own Git repository"]
        if git("rev-parse", "HEAD") != commit:
            return ["ORFS checkout revision does not match the locked commit"]
        # Include ignored inputs: an ignored settings.mk can override the flow.
        if git("status", "--porcelain", "--untracked-files=all", "--ignored", "--", "flow"):
            return ["ORFS flow inputs are modified or contain untracked/ignored files"]
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"cannot verify pinned ORFS checkout: {exc}"]
    return []
