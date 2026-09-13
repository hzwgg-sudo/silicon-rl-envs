#!/usr/bin/env python3
"""Run a toy task with an explicit actions file (M0-08).

Usage:
    python scripts/run_task.py --task TASK.json --actions ACTIONS.json \
        --output-dir OUT [--seed N]

Exit codes: 0 pass, 2 invalid submission / grading failure, 3 infra/usage.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from silicon_env.cli import main_run

if __name__ == "__main__":
    sys.exit(main_run())
