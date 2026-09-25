"""autopilot — modular implementation of the auto-iterate-project state helper.

The package is organized by responsibility so it stays easy to extend and
maintain:

- ``io``       low-level time, JSON, git, lock, and token helpers
- ``config``   .autopilot/config.json schema, validation, persistence
- ``state``    .autopilot/state.json, backlog, stop conditions, reports
- ``agent``    runtime agent detection (opencode / claude-code / codex / generic)
- ``secrets``  staged-diff secret scanning (pattern table + masking)
- ``guard``    allow_paths/deny_paths commit path matching
- ``verify``   verification command discovery from repo entry points
- ``miner``    deterministic repo mining into backlog candidates
- ``commands`` all CLI command handlers
- ``cli``      argument parsing and dispatch

The thin ``scripts/autopilot_state.py`` entry point re-exports the names the
test suite (and external callers) historically imported from that module.
"""

from . import io
from . import config
from . import state
from . import agent
from . import secrets
from . import guard
from . import verify
from . import miner
from . import commands
from . import cli

# Single version authority: SKILL.md, agents/openai.yaml, references/overview.md,
# and the CHANGELOG's newest section must all carry this value (asserted by the
# suite's version-consistency test so drift turns CI red instead of relying on memory).
__version__ = "1.5.0"

__all__ = ["io", "config", "state", "agent", "secrets", "guard", "verify", "miner", "commands", "cli", "__version__"]
