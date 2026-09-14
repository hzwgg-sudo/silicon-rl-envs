#!/usr/bin/env python3
"""Generate the repeatable GCD reference baseline (M1-05).

Runs the stock GCD candidate three times in fresh scratch workspaces
under the identical pinned profile, verifies all runs valid and within
the declared tight tolerances, then writes the baseline record
atomically. Exits nonzero on any validity failure or unexplained
metric drift, and never writes fabricated values (on failure nothing
is written).

Linux route (real EDA runs; blocked on the Mac dev host)::

    python scripts/generate_openroad_baseline.py \\
        --orfs-checkout /path/to/orfs \\
        --output silicon_env/environments/openroad/tasks/gcd/baseline.json \\
        --seed 0 --timeout-s 7200

Default unit tests inject a fake ``run_once`` callable and never touch
EDA tools, Docker, network, or API keys::

    record = generate_baseline(output_path=path, run_once=fake_run_once)

Exit codes: 0 baseline written, 1 validity failure / metric drift /
run error, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from silicon_env.environments.openroad import baseline as gcd_baseline  # noqa: E402
from silicon_env.environments.openroad import config as gcd  # noqa: E402

DEFAULT_OUTPUT = str(
    Path(__file__).resolve().parent.parent
    / "silicon_env"
    / "environments"
    / "openroad"
    / "tasks"
    / "gcd"
    / "baseline.json"
)

RunOnceFn = Callable[[Path, int], Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--orfs-checkout",
        default=None,
        help="path to the pinned ORFS checkout (else $ORFS_CHECKOUT)",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="destination baseline.json path (default: packaged tasks/gcd/baseline.json)",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="base seed; runs use seed/seed+1/seed+2"
    )
    parser.add_argument("--timeout-s", type=float, default=7200.0, help="per-run flow deadline")
    parser.add_argument("--work-parent", help="retain raw run evidence under this directory")
    parser.add_argument(
        "--run-id-prefix",
        default="stock-gcd",
        help="prefix for the three provenance run ids",
    )
    parser.add_argument(
        "--area-rel", type=float, default=gcd_baseline.DEFAULT_TOLERANCES["area_rel"]
    )
    parser.add_argument(
        "--wns-abs-ns", type=float, default=gcd_baseline.DEFAULT_TOLERANCES["wns_abs_ns"]
    )
    parser.add_argument(
        "--tns-abs-ns", type=float, default=gcd_baseline.DEFAULT_TOLERANCES["tns_abs_ns"]
    )
    return parser


def measure_peak_rss_gb() -> float | None:
    """Best-effort peak RSS in GiB (self + children); None when unavailable."""
    try:
        import resource  # Unix only; absent on some platforms
    except ImportError:
        return None
    try:
        peak_self = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        try:
            peak_kids = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        except (OSError, ValueError):
            peak_kids = 0
        peak = max(peak_self, peak_kids)
        if peak <= 0:
            return None
        # Linux ru_maxrss is KiB, macOS is bytes: convert to GiB.
        if sys.platform == "darwin":
            return peak / (1024**3)
        return peak / (1024**2)
    except (OSError, ValueError):
        return None


def write_record_atomic(record: Mapping[str, Any], output_path: str | os.PathLike[str]) -> Path:
    """Write the baseline record atomically (tmp file + os.replace)."""
    target = Path(output_path)
    if target.parent and str(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent) or "."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target



def make_flow_run_once(
    *,
    orfs_checkout: str | os.PathLike[str],
    timeout_s: float,
    runner_factory: Callable[[], Any] | None = None,
) -> RunOnceFn:
    """Build the real ``run_once`` (flow + metrics) for the Linux route."""
    from silicon_env.environments.openroad import flow as gcd_flow
    from silicon_env.environments.openroad.reports import parse_generated_reports

    def _default_runner() -> Any:
        from silicon_env.environments.openroad.runtime import GcdContainerRunner

        return GcdContainerRunner()

    factory = runner_factory or _default_runner

    def run_once(scratch_dir: Path, seed: int) -> Any:
        runner = factory()
        result = gcd_flow.run_gcd_flow(
            gcd.stock_candidate_config(),
            orfs_checkout=orfs_checkout,
            scratch_dir=scratch_dir,
            runner=runner,
            seed=seed,
            timeout_s=timeout_s,
        )
        (scratch_dir / "flow-result.json").write_text(
            json.dumps(result.to_dict(), indent=2) + "\n")
        return parse_generated_reports(result)

    return run_once


def generate_baseline(
    *,
    output_path: str | os.PathLike[str],
    run_once: RunOnceFn,
    seeds: tuple[int, int, int] | list[int] = (0, 1, 2),
    run_id_prefix: str = "stock-gcd",
    tolerances: Mapping[str, Any] | None = None,
    tool_versions: Mapping[str, Any] | None = None,
    work_parent: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Run the stock candidate 3x via ``run_once`` and write the baseline.

    Each invocation gets a fresh empty scratch dir; ``run_once`` maps
    ``(scratch_dir, seed)`` to a :class:`GcdMetrics`. All three metrics
    must be valid and within tolerances (checked by
    :func:`build_baseline_record`), otherwise nothing is written and a
    :class:`BaselineError` propagates. Measured wallclock/peak-RSS are
    recorded as provenance resources.
    """
    seed_list = list(seeds)
    if len(seed_list) != gcd_baseline.REQUIRED_RUN_COUNT:
        raise gcd_baseline.BaselineError(
            f"need exactly {gcd_baseline.REQUIRED_RUN_COUNT} seeds, got {len(seed_list)}"
        )
    run_ids = [f"{run_id_prefix}-{i}" for i in range(gcd_baseline.REQUIRED_RUN_COUNT)]
    tols = dict(tolerances) if tolerances is not None else dict(gcd_baseline.DEFAULT_TOLERANCES)

    collected: list[Any] = []
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="gcd-baseline-") as parent:
        base = Path(work_parent) if work_parent is not None else Path(parent)
        base.mkdir(parents=True, exist_ok=True)
        for index, seed in enumerate(seed_list):
            scratch = base / f"run-{index}"
            if scratch.exists():
                raise gcd_baseline.BaselineError(f"scratch dir already exists: {scratch}")
            scratch.mkdir(parents=True)
            try:
                metrics = run_once(scratch, seed)
            except gcd_baseline.BaselineError:
                raise
            except Exception as exc:
                raise gcd_baseline.BaselineError(
                    f"run {index} (seed {seed}) raised {type(exc).__name__}: {exc}"
                ) from exc
            collected.append(metrics)
    wallclock_s = max(0.0, time.monotonic() - start)
    peak_rss_gb = None  # host Docker-client RSS is not the EDA process peak
    resources = {
        "wallclock_s": wallclock_s,
        "peak_rss_gb": peak_rss_gb,
        "status": "measured" if peak_rss_gb is not None else "measured-partial",
        "notes": (
            "total generator wallclock for the three stock runs; "
            "EDA peak RSS is unmeasured: host Docker-client RSS is not container memory"
        ),
    }
    record = gcd_baseline.build_baseline_record(
        collected,
        tolerances=tols,
        run_ids=run_ids,
        seeds=seed_list,
        tool_versions=tool_versions,
        resources=resources,
    )
    write_record_atomic(record, output_path)
    return record


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    checkout_raw = (args.orfs_checkout or os.environ.get("ORFS_CHECKOUT", "")).strip()
    if not checkout_raw:
        print("error: --orfs-checkout is required (or set $ORFS_CHECKOUT)", file=sys.stderr)
        return 2
    if not isinstance(args.seed, int) or args.seed < 0:
        print("error: --seed must be a non-negative integer", file=sys.stderr)
        return 2
    try:
        timeout_s = float(args.timeout_s)
    except (TypeError, ValueError):
        print("error: --timeout-s must be a number", file=sys.stderr)
        return 2
    if not (timeout_s > 0) or not math.isfinite(timeout_s):
        print("error: --timeout-s must be a positive finite number", file=sys.stderr)
        return 2
    tolerances = {
        "area_rel": args.area_rel,
        "wns_abs_ns": args.wns_abs_ns,
        "tns_abs_ns": args.tns_abs_ns,
    }
    seeds = [args.seed + i for i in range(gcd_baseline.REQUIRED_RUN_COUNT)]
    try:
        from silicon_env.environments.openroad.preflight import (
            load_toolchain_lock,
            run_preflight,
        )
        from silicon_env.environments.openroad.runtime import probe_pinned_tool

        check = run_preflight(load_toolchain_lock(), orfs_checkout=checkout_raw,
                              probe_tool=probe_pinned_tool)
        if not check.ok:
            raise gcd_baseline.BaselineError(check.message())
        versions = {name: probe_pinned_tool(name)[1] for name in ("openroad", "yosys", "make")}
        run_once = make_flow_run_once(orfs_checkout=checkout_raw, timeout_s=timeout_s)
        record = generate_baseline(
            output_path=args.output,
            run_once=run_once,
            seeds=seeds,
            tolerances=tolerances,
            tool_versions=versions,
            work_parent=args.work_parent,
        )
    except gcd_baseline.BaselineError as exc:
        print(f"baseline generation failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # flow/contract misuse surfaces as ValueError
        print(f"baseline generation failed: {exc}", file=sys.stderr)
        return 1
    sample = gcd_baseline.summarize(record)
    print(f"baseline written: {args.output}")
    print(f"  status={sample['status']} task={sample['task_id']}@{sample['task_version']}")
    print(f"  orfs={sample['orfs_commit'][:12]}... candidate={sample['candidate_hash'][:12]}...")
    print(f"  area={sample['metrics']['area_um2']!r} um^2 runs={sample['run_ids']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
