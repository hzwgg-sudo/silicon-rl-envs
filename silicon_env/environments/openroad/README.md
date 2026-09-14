# OpenROAD GCD toolchain profile (M1-01)

Pinned, deterministic execution profile for the scored GCD/nangate45 flow.
Ticket M1-01. This directory holds the immutable pin; later M1 tickets
(task config, flow, metrics, grader, env) build on it.

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
- Binary versions (`openroad -version`, `yosys -V`) and reference-run
  RAM/time are **TBD-unverified**: only values from a real pinned-image
  run may fill them in. The preflight warns on these entries instead of
  passing silently.

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

## Linux route for actual EDA runs (requires a real Linux x86_64 host)

```bash
# 1. Fetch the pinned checkout (full history not required for scoring).
git clone https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git orfs
git -C orfs checkout 036d106273e66855cd5214d49518fd0f0df7de61

# 2. Pull the pinned image by digest (never by floating tag).
docker pull docker.io/openroad/orfs:26Q2@sha256:7832ae885e62933fcbfc486fbd9133f8c3bd1206d15c96e93bfad97432947b61

# 3. Gate, then run the reference GCD flow (single worker).
ORFS_CHECKOUT=/path/to/orfs python scripts/check_openroad.py \
    --orfs-checkout /path/to/orfs
make -C /path/to/orfs/flow DESIGN_CONFIG=./designs/nangate45/gcd/config.mk
```

Record the measured `openroad -version` / `yosys -V` output and the
peak RAM + wallclock into `toolchain.lock.json` (`tools.*.version`,
`resources.reference_run`) — that update is owned by a later ticket once
a real run exists.

## Reference-run resources: TBD (not promised)

| Metric | Value | Status |
| ------ | ----- | ------ |
| peak RAM | unknown | TBD — measure on Linux route |
| wallclock | unknown | TBD — measure on Linux route |
| machine | none yet | TBD |

8 GB Mac support is **not** claimed. The dev host (Mac arm64, 8 GB RAM,
container daemon stopped) cannot run this profile, so no pull, build, or
flow run was attempted here.

## Verification status (2026-09-14, Mac arm64/8GB, daemon stopped)

- `git ls-remote` + GitHub API: ORFS tag `26Q2` -> commit `036d1062…`,
  tool gitlinks, Docker Hub digest `sha256:7832ae88…` — all real,
  recorded above.
- `docker info`: daemon unreachable (`no such file or directory` for the
  socket) — image pull / version probe **blocked**, recorded in the
  lockfile `verification` section rather than fabricated.
- `pytest`, `ruff`, `git diff --check`: see the ticket report.

## Non-goals

No Ibex, no alternate PDKs, no native macOS EDA support, no vendored PDK
blobs (the repo bundles no LEF/LIB/GDS; assets resolve inside the pinned
checkout or image).
