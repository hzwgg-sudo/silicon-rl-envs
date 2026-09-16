# OpenROAD GCD toolchain profile (M1-01)

For executable container setup and scoring instructions, use
[the GCD quickstart](../../../docs/gcd-quickstart.md). Every flow now uses
its own WORK_HOME output tree and the production evaluator reads the pinned
final report paths automatically. See the quickstart for current qualification evidence.


Pinned, deterministic execution profile for the scored GCD/nangate45 flow.
This directory includes the immutable pin, task configuration, flow adapter,
metrics reader, grader and environment.

## Files

| File | Purpose |
| ---- | ------- |
| `toolchain.lock.json` | Immutable pin: ORFS commit, tool source commits, digest-pinned image, required assets, resource floor |
| `preflight.py` | Preflight gate (importable, stdlib-only) |
| `__init__.py` | Shared constants (`OPENROAD_LOCKFILE`, design/platform, single-worker defaults) |
| `README.md` | This document |

`scripts/check_openroad.py` is the CLI wrapper; `docker/openroad/Dockerfile`
records the reproducible container profile.

## The pin

- ORFS `26Q2` at commit `036d106273e66855cd5214d49518fd0f0df7de61`
  (2026-04-07 quarterly release; design/config paths re-verified at the
  commit on 2026-09-14).
- Tool sources at their ORFS gitlink commits: OpenROAD `0e2d771c…`,
  yosys `d3e297fc…` (upstream v0.63 era), yosys-slang `64b44616a…`.
- Runtime image `docker.io/openroad/orfs:26Q2` by digest
  `sha256:7832ae88…47b61` (linux/amd64).
- No floating refs anywhere in scored inputs: the lockfile validator
  rejects floating tags in `orfs.*`, `sources.*`, and `image.*`.
- Binary versions were probed inside the pinned image on Linux and are
  recorded in the lockfile; preflight checks subsequent probes against them.

## Single-worker defaults

One worker, one job (`cpus: 1, jobs: 1`). The resource floor for any
reference run is `cpus >= 2, RAM >= 4 GiB`, configurable via
`--min-ram-gb` / `--min-cpus`. The old `silicon-m0` container profile
(2 CPU/2GiB) is **not** evidence for OpenROAD and is not reused as proof.

## Native lightweight Mac tests

Default tests need no EDA tools, Docker, network, or API keys:

```bash
.venv/bin/pytest -q tests/test_openroad_toolchain.py
.venv/bin/ruff check .
python scripts/check_openroad.py --help
```

Unit tests inject fake tool probes, fake arches, and synthetic ORFS
checkouts under `tmp_path`.

## Linux execution and qualification

Follow [the GCD quickstart](../../../docs/gcd-quickstart.md) for preflight,
three fresh baseline runs, an episode, independent regrading and the release
gate. All flow invocations use the restricted container adapter; pulling the
image alone does not install tools on the host.

Task v0.2.0 uses the approved immutable 0.60 ns constraint. The upstream
0.46 ns stock design failed timing on the pinned tools; its measurements
remain in [the historical record](../../../docs/m1-stock-qualification.json).
The zero-negative-slack grader is unchanged.

`tasks/gcd/baseline.json` must pass `baseline.validate_baseline` before
scoring. The fingerprint covers tool/source pins, stock parameters, fixed
design facts and the packaged SDC content hash. Changed inputs invalidate
old baselines. Generate a replacement using:

```bash
python scripts/generate_openroad_baseline.py \
    --orfs-checkout /path/to/orfs \
    --output /tmp/gcd-baseline.json --work-parent /tmp/gcd-baseline-runs \
    --seed 7 --timeout-s 7200
```

The generator rejects invalid, timing-infeasible or drifting runs. Resource
records distinguish elapsed flow time and GNU-time maximum child RSS from
whole-container memory. The enforced profile is 1 CPU and 4 GiB memory;
native Apple-silicon execution is unsupported.

## Scripted GCD baselines (M1-09)

Two deterministic scripted agents in `silicon_env/agents/` establish
task usability and the do-nothing reference for the GCD task:

- `baseline.py::run_noop_agent` — resets, optionally performs one
  inspect `read_file` of the stock candidate, then submits without
  edits. Declared cap `NOOP_MAX_ACTIONS = 2` (inspect + submit), zero
  tool calls. Trusted score `0.5` when the baseline matches stock.
- `openroad_search.py::run_search_agent` — evaluates a fixed-order
  grid of at most four legal config pairs (stock `(0.30, 55.0)` plus
  three variants spanning the supported bounds: mins, maxes, mid
  density), one `write_file` + one `openroad-flow` run per candidate,
  then writes back the winner and submits. Declared caps
  `SEARCH_MAX_CANDIDATES = 4`, `SEARCH_MAX_TOOL_CALLS = 4`.
  Winner selection is stable and deterministic: smallest observed area,
  then lowest `PLACE_DENSITY`, then lowest `CORE_UTILIZATION`, then
  earliest grid index. Same seed + same tool metrics always yields the
  same action sequence and selection (no randomness). All-invalid grids
  fall back to stock and report no improvement honestly; budget
  exhaustion stops probing before exceeding the budget and submits the
  best so far (or terminates honestly when the episode already ended).

Both agents drive only the public observation/action interface and
save through the existing environment trace/artifacts (no custom paths
outside `work_root`). Tests in `tests/test_openroad_baselines.py`
inject fake flow backends and need no EDA/Docker/network/keys.

```bash
.venv/bin/pytest -q tests/test_openroad_baselines.py
```

Real pinned runs of both agents are opt-in and skipped by default
(`SILICON_RUN_GCD_E2E=1` plus `ORFS_CHECKOUT` at the pinned
commit on Linux); improvement over stock is recorded, not required.

## Non-goals

No Ibex, no alternate PDKs, no native macOS EDA support, no vendored PDK
blobs (the repo bundles no LEF/LIB/GDS; assets resolve inside the pinned
checkout or image).
