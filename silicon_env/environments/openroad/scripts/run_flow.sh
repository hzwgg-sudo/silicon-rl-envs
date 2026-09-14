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
#   - This wrapper retains pinned router defaults; FLOW_SEED, if
#     set, is recorded in the log header but NOT forwarded (see adapter
#     provenance `seed_passthrough_supported=false`).
#   - Outputs go under fresh WORK_HOME (default: a new temporary directory).
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
    --native-tools --orfs-checkout "${ORFS_CHECKOUT}"
fi

if [ "$#" -ne 0 ]; then
  echo "run_flow: extra make arguments are not supported" >&2
  exit 2
fi
if [ "$FLOW_VARIANT" != "default" ]; then
  echo "run_flow: FLOW_VARIANT must be default" >&2
  exit 2
fi
python3 - "$PLACE_DENSITY" "$CORE_UTILIZATION" <<'PYCODE'
import sys
from silicon_env.environments.openroad.config import validate_candidate_config
validate_candidate_config({"PLACE_DENSITY": float(sys.argv[1]),
                           "CORE_UTILIZATION": float(sys.argv[2])})
PYCODE
WORK_HOME="${WORK_HOME:-$(mktemp -d)}"
mkdir -p "$WORK_HOME"
if [ -n "$(ls -A "$WORK_HOME")" ]; then
  echo "run_flow: WORK_HOME must be empty" >&2
  exit 2
fi
WORK_HOME="$(cd "$WORK_HOME" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS=1
export TZ=UTC

echo "run_flow: endpoint=final design=gcd platform=nangate45"
echo "run_flow: variant=${FLOW_VARIANT} place_density=${PLACE_DENSITY} \\"
echo "  core_utilization=${CORE_UTILIZATION} seed=${FLOW_SEED} (record-only)"
echo "run_flow: checkout=${ORFS_CHECKOUT} outputs=${WORK_HOME}"

exec make -C "${ORFS_CHECKOUT}/flow" \
  DESIGN_CONFIG=./designs/nangate45/gcd/config.mk \
  "FLOW_VARIANT=${FLOW_VARIANT}" \
  "PLACE_DENSITY=${PLACE_DENSITY}" \
  "CORE_UTILIZATION=${CORE_UTILIZATION}" \
  "SDC_FILE=${SCRIPT_DIR}/../tasks/gcd/constraint.sdc" \
  "WORK_HOME=${WORK_HOME}" \
  "POST_FINAL_REPORT_TCL=${SCRIPT_DIR}/final_evidence.tcl" \
  NUM_CORES=1 -j1 final
