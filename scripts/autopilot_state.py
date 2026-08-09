#!/usr/bin/env python3
"""Deterministic state, backlog, branch, and budget helper for auto-iterate-project.

Thin CLI entry point; the implementation lives in the ``autopilot`` package
(scripts/autopilot/), split by responsibility so it stays easy to extend and
maintain. This module re-exports the names historically imported from it so
external callers and the test suite keep working unchanged."""

from autopilot.cli import main
from autopilot.io import (
    EMPTY_TREE,
    parse_deadline,
    parse_time,
    _is_autopilot_path,
    _parse_numstat,
)
from autopilot.state import (
    compute_stop_reason,
    _candidate_score,
)

if __name__ == "__main__":
    main()
