"""autopilot — modular implementation of the auto-iterate-project state helper.

The package is organized by responsibility so it stays easy to extend and
maintain:

- ``io``      low-level time, JSON, git, lock, and token helpers
- ``config``  .autopilot/config.json schema, validation, persistence
- ``state``   .autopilot/state.json, backlog, stop conditions, reports
- ``agent``   runtime agent detection (opencode / claude-code / codex / generic)
- ``commands`` all CLI command handlers
- ``cli``     argument parsing and dispatch

The thin ``scripts/autopilot_state.py`` entry point re-exports the names the
test suite (and external callers) historically imported from that module.
"""

from . import io
from . import config
from . import state
from . import agent
from . import commands
from . import cli
