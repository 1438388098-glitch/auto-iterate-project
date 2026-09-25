"""Runtime agent detection and adaptation (opencode / claude-code / codex / generic)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import io

AGENT_OVERRIDE_ENV = "AUTOPILOT_AGENT"

# Env vars that uniquely identify a runtime. An empty value counts as unset
# (`VAR= cmd` in a shell exports the name with ""), keeping accidental exports
# from hijacking detection.
AGENT_ENV_SIGNALS = [
    ("opencode", ("OPENCODE",)),
    ("claude-code", ("CLAUDE_CODE",)),
    ("codex", ("CODEX",)),
]

# Global/cwd config markers that identify a runtime when env vars are absent.
# Checked in the target repo first, then the agent home directory.
AGENT_MARKER_FILES = [
    ("opencode", ("opencode.json",)),
    ("claude-code", ("CLAUDE.md",)),
    ("codex", ("AGENTS.md",)),
]

AGENT_PROFILES = {
    "opencode": {
        "label": "OpenCode",
        "default_shell": "powershell" if os.name == "nt" else "bash",
        "skill_dir": ".config/opencode/skills",
        "project_marker": "opencode.json",
        "agent_config": "opencode.json",
    },
    "claude-code": {
        "label": "Claude Code",
        "default_shell": "bash",
        "skill_dir": ".claude/skills",
        "project_marker": "CLAUDE.md",
        "agent_config": ".claude/settings.json",
    },
    "codex": {
        "label": "Codex",
        "default_shell": "bash",
        "skill_dir": ".codex/skills",
        "project_marker": "AGENTS.md",
        "agent_config": "AGENTS.md",
    },
    "generic": {
        "label": "Generic agent",
        "default_shell": "powershell" if os.name == "nt" else "bash",
        "skill_dir": ".agents/skills",
        "project_marker": "AGENTS.md",
        "agent_config": "AGENTS.md",
    },
}

KNOWN_AGENTS = ("opencode", "claude-code", "codex", "generic")


def _env_var_present(name):
    return name in os.environ and os.environ.get(name) != ""


def detect_agent(cwd=None, home=None):
    """Detect the runtime agent from env vars, then cwd markers, then home markers.
    Returns (agent, detected_by) where agent is one of KNOWN_AGENTS."""
    cwd = Path(cwd) if cwd else Path.cwd()
    home = Path(home) if home else io.home_dir()

    override = os.environ.get(AGENT_OVERRIDE_ENV, "")
    if override:
        if override not in KNOWN_AGENTS:
            print(
                "[WARN] {}='{}' is not one of {}; falling back to generic. "
                "Fix the value or unset the variable.".format(
                    AGENT_OVERRIDE_ENV, override, "|".join(sorted(KNOWN_AGENTS))
                ),
                file=sys.stderr,
            )
            override = "generic"
        return override, "override:{}".format(AGENT_OVERRIDE_ENV)

    for agent, vars_ in AGENT_ENV_SIGNALS:
        for name in vars_:
            if _env_var_present(name):
                return agent, "env:{}".format(name)

    # cwd project markers beat home markers (a project is usually configured for one agent)
    for agent, names in AGENT_MARKER_FILES:
        for name in names:
            if (cwd / name).exists():
                return agent, "cwd:{}".format(name)
            if (cwd / ".opencode" / name).exists() and agent == "opencode":
                return agent, "cwd:.opencode/{}".format(name)

    for agent, names in AGENT_MARKER_FILES:
        for name in names:
            marker = home / name
            if marker.exists() or (home / ("." + name)).exists():
                return agent, "home:{}".format(name)

    return "generic", "fallback"


def detect_python():
    """Return the first python launcher that actually runs (-V), trying the
    SKILL.md fallback order python -> python3 -> py. shutil.which alone is not
    enough: a stale shim (e.g. the Windows Store alias) resolves on PATH but
    cannot execute, and a dead python_cmd would poison every downstream
    command. Falls back to "python" when nothing can be probed."""
    for candidate in ("python", "python3", "py"):
        if not shutil.which(candidate):
            continue
        try:
            probe = subprocess.run(
                [candidate, "-V"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return "python"


def agent_profile(agent):
    return AGENT_PROFILES.get(agent, AGENT_PROFILES["generic"])


def skill_root():
    """Directory of the installed skill (the one holding SKILL.md). This file
    lives at <skill_root>/scripts/autopilot/agent.py, so the root is exactly
    three levels up — a home-relative guess pointed at the skills PARENT
    directory, and paths built from it did not exist."""
    return Path(__file__).resolve().parents[2]


def cmd_detect_agent(args):
    agent, detected_by = detect_agent(cwd=Path(args.repo).resolve(), home=args.home)
    profile = agent_profile(agent)
    python_cmd = detect_python()
    payload = {
        "agent": agent,
        "label": profile["label"],
        "detected_by": detected_by,
        "shell": profile["default_shell"],
        "python_cmd": python_cmd,
        # SKILL_DIR stays an explicit override; otherwise report where this
        # skill is actually installed (derived from the package location),
        # regardless of the --home detection override.
        "skill_dir": os.environ.get("SKILL_DIR") or str(skill_root()),
        "project_marker": profile["project_marker"],
        "agent_config": profile["agent_config"],
        "adaptation": {
            "use_python": python_cmd,
            "shell_syntax": profile["default_shell"],
            "command_style": "powershell" if profile["default_shell"] == "powershell" else "bash",
        },
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0
