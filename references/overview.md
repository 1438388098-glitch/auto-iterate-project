# Auto Iterate Project

Version 1.5.0 — see [../CHANGELOG.md](../CHANGELOG.md) for release history.

Automatically iterate any git project inside an agent session: analyze the repository, pick the next high-value improvement, implement small changes, verify, commit, and loop until a goal is met or configurable round/time/token limits are reached.

Works with opencode, Claude Code, Codex, and other agent runtimes (auto-detected). Git is the only external dependency.

## Features

- **Self-owned loop**: deterministic state, backlog, branch, and budget management via `scripts/autopilot_state.py`; no host-specific tooling required.
- **Efficiency-first defaults**: one round works on `candidates_per_round` (default 4) candidates, full verification runs every `verify_every_rounds` (default 3) rounds, and commits are flushed once every `commit_every_rounds` (default 5) rounds instead of per candidate.
- **Smart backlog ranking**: candidates ranked by expected value per round (`ranking_mode: expected`) with type saturation, origin quotas, value floor, and effort cost. `classic` restores the legacy value/effort ratio.
- **Repo analysis cache**: `.autopilot/analysis.json` auto-invalidates when HEAD or config changes.
- **Secret guard**: `commit` scans the staged diff for secret-like content and refuses on a match.
- **Human checkpoints & directives**: `checkpoint_every` pauses to consult you; `directive-add` records standing rules.
- **Expansion phase**: `expand_after_goals: true` keeps improving after goals are met; `config-set --no-expand-after-goals` turns that back off at runtime.
- **Post-goal direction prediction (Wave 0)**: `goal-met --next-step` seeds causal follow-ups with a hit-rate feedback loop.
- **Deep Expansion & anti-idle**: thin backlog is never a stop — rotate the 16-lens set (see `references/expansion-lenses.md`).
- **Early-stop hard gates**: `finish` is refused while budgets remain and ready work exists unless `--force`.
- **Safety rails**: refuses to commit user changes, never rewrites history, defaults to no pushing.

## Installation

Ship surface is `SKILL.md`, `scripts/`, `references/`, and `agents/`. Copy (or link) the folder into your agent's skills directory:

- opencode: `~/.config/opencode/skills/auto-iterate-project/`
- Claude Code: `~/.claude/skills/auto-iterate-project/`
- Codex: `~/.codex/skills/auto-iterate-project/`
- generic / MiMo: `~/.agents/skills/auto-iterate-project/`

If you keep a git clone of this repository, prefer a directory junction (Windows) or symlink (macOS/Linux) so `git pull` upgrades the installed skill in place. Keep the clone on a stable branch — a checkout changes the installed skill.

The helper script is standalone and works on Windows, macOS, and Linux (Python 3.6+).

## Quick Start

Inside any git repository:

```powershell
python <skill-dir>/scripts/autopilot_state.py init --repo <repo> --max-rounds 5 --branch-mode feature
```

Then run the loop in `SKILL.md`: check → analyze → backlog → begin-round → implement → verify → commit → complete-round.

## Configuration

All options live in `.autopilot/config.json`. Highlights are documented in `SKILL.md`; the full field reference is [config.md](config.md).

## Testing

```powershell
python <skill-dir>/scripts/test_autopilot_state.py
python -m py_compile <skill-dir>/scripts/autopilot_state.py
```

## Project Layout

```
auto-iterate-project/
├── SKILL.md                     # Skill entry point and workflow
├── agents/openai.yaml           # Agent store metadata
├── references/
│   ├── config.md                # Full configuration reference
│   ├── expansion-lenses.md      # 16-lens Deep Expansion catalog
│   ├── wave0-prediction.md      # Post-goal direction prediction
│   ├── troubleshooting.md       # Symptom → cause → fix
│   └── overview.md              # This file
└── scripts/
    ├── autopilot_state.py       # Thin CLI entry point
    ├── test_autopilot_state.py  # Self-test suite
    └── autopilot/               # Implementation package
```
