#!/usr/bin/env python3
"""Preflight gate for the pinned OpenROAD GCD toolchain (M1-01).

Verifies expected tools, locked versions, required ORFS assets,
architecture, and resource floor before any flow starts.

Usage:
    python scripts/check_openroad.py --orfs-checkout /path/to/orfs
    python scripts/check_openroad.py --orfs-checkout /path/to/orfs \\
        --min-ram-gb 8 --min-cpus 4

Exit codes: 0 ok, 1 preflight failures, 2 usage / unreadable lockfile.
Default probes run no containers and need no network; probing tool
``--version`` flags on the local PATH is the only host contact.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from silicon_env.environments.openroad.preflight import (  # noqa: E402
    PreflightError,
    default_lockfile_path,
    load_toolchain_lock,
    run_preflight,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lockfile",
        default=str(default_lockfile_path()),
        help="path to toolchain.lock.json (default: packaged lockfile)",
    )
    parser.add_argument(
        "--orfs-checkout",
        default=None,
        help="path to the pinned ORFS checkout (else $ORFS_CHECKOUT)",
    )
    parser.add_argument("--min-ram-gb", type=float, default=None)
    parser.add_argument("--min-cpus", type=int, default=None)
    parser.add_argument(
        "--arch",
        default=None,
        help="override detected machine arch (for testing only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        lock = load_toolchain_lock(args.lockfile)
    except PreflightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        report = run_preflight(
            lock,
            orfs_checkout=args.orfs_checkout,
            machine=args.arch,
            min_ram_gb=args.min_ram_gb,
            min_cpus=args.min_cpus,
        )
    except PreflightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(report.message())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
