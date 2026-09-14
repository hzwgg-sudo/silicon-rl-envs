#!/usr/bin/env python3
"""Regrade a saved run-task submission dir with the pure toy grader (M0-08).

Usage:
    python scripts/grade_task.py --submission-dir OUT [--output GRADE.json]

Exit codes: 0 pass, 2 grading failure, 3 infra/usage.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from silicon_env.cli import main_grade

if __name__ == "__main__":
    sys.exit(main_grade())
