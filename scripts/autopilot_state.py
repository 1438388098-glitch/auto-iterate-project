#!/usr/bin/env python3
"""Deterministic state, backlog, branch, and budget helper for auto-iterate-project.

Thin CLI entry point; the implementation lives in the ``autopilot`` package
(scripts/autopilot/), split by responsibility so it stays easy to extend and
maintain. This module re-exports the names historically imported from it so
external callers and the test suite keep working unchanged."""

import sys
from pathlib import Path

# Explicit bootstrap so the entry keeps working under `python -P` /
# PYTHONSAFEPATH, or when only this file is copied without the package layout.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from autopilot.cli import main
from autopilot.io import (
    parse_deadline,
    parse_time,
    _is_autopilot_path,
    _parse_numstat,
)
from autopilot.state import (
    compute_stop_reason,
    _candidate_score,
    all_goals_met,
    candidate_adjusted_score,
    candidate_deps_status,
    compute_type_stats,
    rank_candidates,
)

if __name__ == "__main__":
    main()
