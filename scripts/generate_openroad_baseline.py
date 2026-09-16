#!/usr/bin/env python3
"""Generate a repeatable task reference baseline (M1-05 GCD, M2-02 second task).

Runs the stock candidate three times in fresh scratch workspaces
under the identical pinned profile, verifies all runs valid and within
the declared tight tolerances, then writes the baseline record
atomically. Exits nonzero on any validity failure or unexplained
metric drift, and never writes fabricated values (on failure nothing
is written).

Linux route (real EDA runs; blocked on the Mac dev host)::

    python scripts/generate_openroad_baseline.py \\
        --orfs-checkout /path/to/orfs \\
        --output silicon_env/environments/openroad/tasks/gcd/baseline.json \\
        --seed 7 --timeout-s 7200

    python scripts/generate_openroad_baseline.py --task ibex \\
        --orfs-checkout /path/to/orfs \\
        --output silicon_env/environments/openroad/tasks/ibex/baseline.json \\
        --seed 7 --timeout-s 7200

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
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from silicon_env.environments.openroad import baseline as task_baseline  # noqa: E402
from silicon_env.environments.openroad import config as gcd  # noqa: E402
from silicon_env.environments.openroad import ibex_config as ibex  # noqa: E402

gcd_baseline = task_baseline  # historical alias used below

DEFAULT_OUTPUT = str(
    Path(__file__).resolve().parent.parent
    / "silicon_env"
    / "environments"
    / "openroad"
    / "tasks"
    / "gcd"
    / "baseline.json"
)

DEFAULT_IBEX_OUTPUT = str(
    Path(__file__).resolve().parent.parent
    / "silicon_env"
    / "environments"
    / "openroad"
    / "tasks"
    / "ibex"
    / "baseline.json"
)

#: Task name -> task-config module. Both tasks share the pinned ORFS
#: commit, image, platform, endpoint, and report schema; they differ in
#: design facts, candidate stocks, and output paths.
TASK_CONFIGS: dict[str, Any] = {"gcd": gcd, "ibex": ibex}

#: Default provenance run-id prefix per task (separate namespaces so
#: one task's runs are never mistaken for the other's).
TASK_RUN_ID_PREFIXES: dict[str, str] = {"gcd": "stock-gcd", "ibex": "stock-ibex"}

#: In-image tool root at the pinned ORFS image (absolute container
#: paths, probed on the Linux route; also see runtime.TOOL_ROOT).
_TOOL_ROOT = "/OpenROAD-flow-scripts/tools/install"

RunOnceFn = Callable[[Path, int], Any]


def resolve_task(name_or_module: Any) -> tuple[str, Any]:
    """Resolve a ``--task`` value to ``(name, config module)``.

    Accepts ``"gcd"`` / ``"ibex"`` (default ``"gcd"`` for ``None``) or a
    task-config module exposing the shared baseline surface. Anything
    else raises :class:`BaselineError` (fail closed).
    """
    if name_or_module is None:
        return "gcd", gcd
    if isinstance(name_or_module, str):
        try:
            return name_or_module, TASK_CONFIGS[name_or_module]
        except KeyError:
            raise task_baseline.BaselineError(
                f"unknown task {name_or_module!r} "
                f"(expected one of {sorted(TASK_CONFIGS)})"
            ) from None
    for name, module in TASK_CONFIGS.items():
        if name_or_module is module:
            return name, module
    # Duck-typed module: must expose the shared surface.
    try:
        task_baseline._resolve_task(name_or_module)
    except task_baseline.BaselineError:
        raise
    label = getattr(name_or_module, "TASK_ID", getattr(name_or_module, "__name__", "?"))
    return str(label), name_or_module


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task",
        default="gcd",
        choices=sorted(TASK_CONFIGS),
        help="which stock task to qualify (default: gcd)",
    )
    parser.add_argument(
        "--orfs-checkout",
        default=None,
        help="path to the pinned ORFS checkout (else $ORFS_CHECKOUT)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="destination baseline.json path (default: packaged tasks/<task>/baseline.json)",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="base seed; runs use seed/seed+1/seed+2"
    )
    parser.add_argument("--timeout-s", type=float, default=7200.0, help="per-run flow deadline")
    parser.add_argument("--work-parent", help="retain raw run evidence under this directory")
    parser.add_argument(
        "--run-id-prefix",
        default=None,
        help="prefix for the three provenance run ids (default: stock-<task>)",
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
    resource_samples = []

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
        resource_path = Path(result.provenance.get("output_dir", scratch_dir)) / "resources.txt"
        peak = None
        if resource_path.is_file():
            match = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)",
                              resource_path.read_text())
            if match and int(match.group(1)) > 0:
                peak = int(match.group(1)) / (1024**2)
        resource_samples.append({"peak_rss_gb": peak,
                                 "wallclock_s": result.provenance.get("duration_s")})
        return parse_generated_reports(result)

    run_once.resource_samples = resource_samples
    return run_once


def check_ibex_checkout(orfs_checkout: str | os.PathLike[str]) -> Path:
    """Verify the pinned checkout carries the flow Makefile plus every
    Ibex ``required_assets`` entry. Returns the resolved checkout root;
    raises :class:`BaselineError` fail-closed when anything is absent."""
    root = Path(orfs_checkout).resolve()
    if not root.is_dir():
        raise gcd_baseline.BaselineError(f"orfs_checkout is not a dir: {orfs_checkout!r}")
    missing = [rel for rel in ("flow/Makefile", *ibex.REQUIRED_ASSETS)
               if not (root / rel).is_file()]
    if missing:
        raise gcd_baseline.BaselineError(
            f"pinned checkout {root} is missing Ibex required assets: {missing}"
        )
    return root


def make_ibex_run_once(
    *,
    orfs_checkout: str | os.PathLike[str],
    timeout_s: float,
    docker_exe: str = "docker",
) -> RunOnceFn:
    """Build the real Ibex ``run_once`` (stock make + metrics) for Linux.

    Mirrors the M2-01 stock probe's copies-only container model (proved
    in run 35054041797) plus the GCD runner's trusted-SDC discipline:
    a disposable writable copy of ``orfs/flow`` is mounted at ``/flow``,
    fresh ``/outputs`` receives ``WORK_HOME`` artifacts and the GNU-time
    ``resources.txt``, and the pinned v0.2.0 task SDC is mounted
    read-only at ``/trusted/constraint.sdc`` and passed as ``SDC_FILE``
    (the upstream 2.20 ns SDC must NOT be used: it is ~16 ps too tight).
    Stock knobs travel explicitly on the make command line; ``OR_SEED``
    carries the run seed to the detailed router.

    Only invoked on the Linux route (needs ``docker`` + the pinned
    image); unit tests inject fakes instead. Any make failure, timeout,
    or missing final evidence raises :class:`BaselineError` (nothing is
    fabricated; the generator then writes nothing).
    """
    from silicon_env.environments.openroad import flow as flow_mod
    from silicon_env.environments.openroad.reports import parse_generated_reports
    from silicon_env.types import StepStatus

    deadline = float(timeout_s)
    if not (deadline > 0) or not math.isfinite(deadline):
        raise gcd_baseline.BaselineError(f"timeout_s must be positive finite, got {timeout_s!r}")
    checkout = check_ibex_checkout(orfs_checkout)
    stock = ibex.stock_candidate_config()
    resource_samples: list[dict[str, Any]] = []

    def run_once(scratch_dir: Path, seed: int) -> Any:
        seed_value = int(seed)
        if seed_value < 0:
            raise gcd_baseline.BaselineError(f"seed must be non-negative, got {seed!r}")
        scratch = Path(scratch_dir)
        flow_copy = scratch / "flow-copy"
        output_dir = scratch / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(checkout / "flow", flow_copy, symlinks=False)
        except OSError as exc:
            raise gcd_baseline.BaselineError(f"cannot stage flow copy: {exc}") from exc
        trusted_sdc = scratch / "constraint.sdc"
        trusted_sdc.write_bytes(ibex.FIXED_SDC_PATH.read_bytes())
        start_ns = time.time_ns()
        start_epoch = time.time()
        console_log = scratch / "flow-console.log"
        argv = [
            docker_exe, "run", "--rm",
            "--pull", "never",
            "--network", "none",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--cpus", "4",
            "--memory", "16g",
            "--memory-swap", "16g",
            "--pids-limit", "512",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
            "--tmpfs", "/scratch:rw,nosuid,nodev,exec,size=1g",
            "-e", "FLOW_VARIANT=default",
            "-e", "NUM_CORES=1",
            "-e", f"OR_SEED={seed_value % (2 ** 31)}",
            "-e", "OMP_NUM_THREADS=1",
            "-e", "TZ=UTC",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "HOME=/tmp",
            "-v", f"{flow_copy}:/flow:rw",
            "-v", f"{output_dir}:/outputs:rw",
            "-v", f"{trusted_sdc}:/trusted/constraint.sdc:ro",
            "-w", "/flow",
            ibex.IMAGE_PINNED_REF,
            "/usr/bin/time", "-v", "-o", "/outputs/resources.txt",
            "make",
            "DESIGN_CONFIG=./designs/nangate45/ibex/config.mk",
            f"PLACE_DENSITY={stock['PLACE_DENSITY']!r}",
            f"CORE_UTILIZATION={stock['CORE_UTILIZATION']!r}",
            "WORK_HOME=/outputs",
            "SDC_FILE=/trusted/constraint.sdc",
            f"OPENROAD_EXE={_TOOL_ROOT}/OpenROAD/bin/openroad",
            f"YOSYS_EXE={_TOOL_ROOT}/yosys/bin/yosys",
            "-j1", "final",
        ]
        try:
            with open(console_log, "w", encoding="utf-8") as log:
                completed = subprocess.run(
                    argv, stdout=log, stderr=subprocess.STDOUT,
                    timeout=deadline, check=False,
                )
        except FileNotFoundError as exc:
            raise gcd_baseline.BaselineError(
                f"docker executable not found ({docker_exe!r}): {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise gcd_baseline.BaselineError(
                f"ibex stock run (seed {seed_value}) exceeded {deadline}s: fail closed"
            ) from exc
        duration_s = time.time() - start_epoch
        if completed.returncode != 0:
            raise gcd_baseline.BaselineError(
                f"ibex stock run (seed {seed_value}) exited {completed.returncode} "
                f"(see {console_log}): fail closed"
            )
        prefix = f"results/{ibex.FIXED_PLATFORM}/{ibex.FIXED_DESIGN}/default"
        finals = [f"{prefix}/6_final.{ext}" for ext in ("gds", "def", "v")]
        fresh_finals = []
        for rel in finals:
            target = output_dir / rel
            try:
                if (target.is_file() and not target.is_symlink()
                        and target.stat().st_mtime_ns >= start_ns):
                    fresh_finals.append(rel)
            except OSError:
                pass
        stage = "final" if len(fresh_finals) == len(finals) else "not-started"
        result = flow_mod.FlowResult(
            status=StepStatus.SUCCESS,
            stage_reached=stage,
            artifacts={rel: str(output_dir / rel) for rel in fresh_finals},
            provenance={
                "endpoint": ibex.ENDPOINT,
                "orfs_commit": ibex.ORFS_COMMIT,
                "image_pinned_ref": ibex.IMAGE_PINNED_REF,
                "seed_requested": seed_value,
                "router_seed": seed_value % (2 ** 31),
                "candidate": dict(stock),
                "clock_period_ns": ibex.FIXED_CLOCK_PERIOD_NS,
                "output_dir": str(output_dir),
                "start_ns": start_ns,
                "duration_s": duration_s,
                "variant": "default",
            },
        )
        (scratch / "flow-result.json").write_text(
            json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        peak: float | None = None
        resource_path = output_dir / "resources.txt"
        if resource_path.is_file():
            try:
                match = re.search(
                    r"Maximum resident set size \(kbytes\):\s*(\d+)",
                    resource_path.read_text(encoding="utf-8"),
                )
            except OSError:
                match = None
            if match and int(match.group(1)) > 0:
                peak = int(match.group(1)) / (1024 ** 2)
        resource_samples.append({"peak_rss_gb": peak, "wallclock_s": duration_s})
        return parse_generated_reports(
            result, design=ibex.FIXED_DESIGN, platform=ibex.FIXED_PLATFORM
        )

    run_once.resource_samples = resource_samples  # type: ignore[attr-defined]
    return run_once


def generate_baseline(
    *,
    output_path: str | os.PathLike[str],
    run_once: RunOnceFn,
    seeds: tuple[int, int, int] | list[int] = (0, 1, 2),
    run_id_prefix: str | None = None,
    tolerances: Mapping[str, Any] | None = None,
    tool_versions: Mapping[str, Any] | None = None,
    work_parent: str | os.PathLike[str] | None = None,
    task: Any = None,
) -> dict[str, Any]:
    """Run the stock candidate 3x via ``run_once`` and write the baseline.

    Each invocation gets a fresh empty scratch dir; ``run_once`` maps
    ``(scratch_dir, seed)`` to metrics. All three metrics must be valid
    and within tolerances (checked by
    :func:`build_baseline_record`), otherwise nothing is written and a
    :class:`BaselineError` propagates. Measured wallclock/peak-RSS are
    recorded as provenance resources. ``task`` selects the task-config
    module the record is keyed to (``"gcd"``/``"ibex"`` name or module;
    default GCD), including the default run-id prefix.
    """
    task_name, cfg = resolve_task(task if task is not None else "gcd")
    prefix = run_id_prefix if run_id_prefix is not None else TASK_RUN_ID_PREFIXES.get(
        task_name, f"stock-{task_name}")
    seed_list = list(seeds)
    if len(seed_list) != gcd_baseline.REQUIRED_RUN_COUNT:
        raise gcd_baseline.BaselineError(
            f"need exactly {gcd_baseline.REQUIRED_RUN_COUNT} seeds, got {len(seed_list)}"
        )
    run_ids = [f"{prefix}-{i}" for i in range(gcd_baseline.REQUIRED_RUN_COUNT)]
    tols = dict(tolerances) if tolerances is not None else dict(gcd_baseline.DEFAULT_TOLERANCES)

    collected: list[Any] = []
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"{prefix}-baseline-") as parent:
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
    samples = getattr(run_once, "resource_samples", [])
    peaks = [sample["peak_rss_gb"] for sample in samples if sample["peak_rss_gb"] is not None]
    peak_rss_gb = max(peaks) if len(peaks) == 3 else None
    resources = {
        "wallclock_s": wallclock_s,
        "runs": samples,
        "peak_rss_gb": peak_rss_gb,
        "status": "measured" if peak_rss_gb is not None else "measured-partial",
        "notes": (
            "total generator wallclock for the three stock runs; "
            "peak RSS is GNU time max child RSS inside each container, not total cgroup memory"
        ),
    }
    record = gcd_baseline.build_baseline_record(
        collected,
        tolerances=tols,
        run_ids=run_ids,
        seeds=seed_list,
        tool_versions=tool_versions,
        resources=resources,
        task=cfg,
    )
    write_record_atomic(record, output_path)
    return record


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        task_name, cfg = resolve_task(args.task)
    except gcd_baseline.BaselineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    output = args.output or (
        DEFAULT_IBEX_OUTPUT if task_name == "ibex" else DEFAULT_OUTPUT
    )
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
        if task_name == "ibex":
            check_ibex_checkout(checkout_raw)
        versions = {name: probe_pinned_tool(name)[1] for name in ("openroad", "yosys", "make")}
        if task_name == "ibex":
            run_once = make_ibex_run_once(orfs_checkout=checkout_raw, timeout_s=timeout_s)
        else:
            run_once = make_flow_run_once(orfs_checkout=checkout_raw, timeout_s=timeout_s)
        record = generate_baseline(
            output_path=output,
            run_once=run_once,
            seeds=seeds,
            tolerances=tolerances,
            tool_versions=versions,
            work_parent=args.work_parent,
            task=cfg,
        )
    except gcd_baseline.BaselineError as exc:
        print(f"baseline generation failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # flow/contract misuse surfaces as ValueError
        print(f"baseline generation failed: {exc}", file=sys.stderr)
        return 1
    sample = gcd_baseline.summarize(record)
    print(f"baseline written: {output}")
    print(f"  status={sample['status']} task={sample['task_id']}@{sample['task_version']}")
    print(f"  orfs={sample['orfs_commit'][:12]}... candidate={sample['candidate_hash'][:12]}...")
    print(f"  area={sample['metrics']['area_um2']!r} um^2 runs={sample['run_ids']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
