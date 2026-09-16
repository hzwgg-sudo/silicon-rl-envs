"""Pinned OpenROAD GCD toolchain profile (M1-01).

Minimal shared constants for the deterministic GCD/nangate45 flow that
later M1 tickets reuse. Stdlib-only, Python >= 3.10.
"""

from __future__ import annotations

from pathlib import Path

#: Absolute path of the immutable toolchain lockfile.
OPENROAD_LOCKFILE = Path(__file__).with_name("toolchain.lock.json")

#: Scored design + platform pair for M1.
OPENROAD_DESIGN = "gcd"
OPENROAD_PLATFORM = "nangate45"

#: Tools the preflight gate checks before any flow may start.
EXPECTED_TOOLS = ("openroad", "yosys", "make")

#: Single-worker execution defaults (see lockfile ``resources``).
SINGLE_WORKER_CPUS = 1
SINGLE_WORKER_JOBS = 1

__all__ = [
    "EXPECTED_TOOLS",
    "OPENROAD_DESIGN",
    "OPENROAD_LOCKFILE",
    "OPENROAD_PLATFORM",
    "SINGLE_WORKER_CPUS",
    "SINGLE_WORKER_JOBS",
]
