#!/usr/bin/env bash
# Fixed-endpoint GCD OpenROAD flow entrypoint (M1-03, container/Linux use).
#
# Thin wrapper: gates the preflight check, then execs the pinned `make`
# target in the checkout's `flow/` directory through detailed routing to
# the `final` endpoint. Candidate overrides travel on the command line so
# the pinned checkout needs no extra files; the Python adapter
# (`silicon_env/environments/openroad/flow.py`) is the primary caller and
# this script exists for manual/container runs and documentation.
#
# Environment:
#   ORFS_CHECKOUT      path to the pinned ORFS checkout (required)
#   PLACE_DENSITY      default 0.30 (stock; must stay in [0.20, 0.80])
#   CORE_UTILIZATION   default 55   (stock; must stay in [20, 90])
#   FLOW_VARIANT       default "default" (must match toolchain.lock.json)
#   REPO_ROOT          repo root for scripts/check_openroad.py
#                      (default: derived from this script's location)
#   SILICON_SKIP_PREFLIGHT=1  skip the preflight gate (not recommended)
#
# Notes:
#   - Single thread: `make -j1` plus OMP_NUM_THREADS=1, TZ=UTC.
#   - ORFS make offers no seed passthrough for the GCD flow; FLOW_SEED, if
#     set, is recorded in the log header but NOT forwarded (see adapter
#     provenance `seed_passthrough_supported=false`).
#   - Flow outputs land under $ORFS_CHECKOUT/flow/{results,logs,reports}
#     (standard ORFS behavior). Use a throwaway checkout copy or a
#     container overlay to keep a pristine pin untouched.
set -euo pipefail

ORFS_CHECKOUT="${ORFS_CHECKOUT:?set ORFS_CHECKOUT to the pinned ORFS checkout}"
PLACE_DENSITY="${PLACE_DENSITY:-0.30}"
CORE_UTILIZATION="${CORE_UTILIZATION:-55}"
FLOW_VARIANT="${FLOW_VARIANT:-default}"
FLOW_SEED="${FLOW_SEED:-unset}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../../.." && pwd)}"

if [ "${SILICON_SKIP_PREFLIGHT:-0}" != "1" ]; then
  python3 "${REPO_ROOT}/scripts/check_openroad.py" \
    --orfs-checkout "${ORFS_CHECKOUT}"
fi

export OMP_NUM_THREADS=1
export TZ=UTC

echo "run_flow: endpoint=final design=gcd platform=nangate45"
echo "run_flow: variant=${FLOW_VARIANT} place_density=${PLACE_DENSITY} \\"
echo "  core_utilization=${CORE_UTILIZATION} seed=${FLOW_SEED} (record-only)"
echo "run_flow: checkout=${ORFS_CHECKOUT}"

exec make -C "${ORFS_CHECKOUT}/flow" \
  DESIGN_CONFIG=./designs/nangate45/gcd/config.mk \
  "FLOW_VARIANT=${FLOW_VARIANT}" \
  "PLACE_DENSITY=${PLACE_DENSITY}" \
  "CORE_UTILIZATION=${CORE_UTILIZATION}" \
  -j1 \
  "$@"
