# Auto Iterate Project

Automatically iterate any git project inside an agent session: analyze the repository, pick the next high-value improvement, implement small changes, verify, commit, and loop until a goal is met or configurable round/time/token limits are reached.

Works with opencode, Claude Code, Codex, and other agent runtimes (auto-detected). Git is the only external dependency.

## Features

- **Self-owned loop**: deterministic state, backlog, branch, and budget management via `scripts/autopilot_state.py`; no host-specific tooling required.
- **Efficiency-first defaults**: one round works on `candidates_per_round` (default 3) candidates, full verification runs every `verify_every_rounds` (default 3) rounds, and commits are flushed once every `commit_every_rounds` (default 5) rounds instead of per candidate — far fewer commits and test runs per improvement.
- **Smart backlog ranking**: candidates are ranked by an adjusted value/effort score that discounts high `risk`, downweights saturated types (`type_saturation_threshold`), downweights consistently-blocking types, and defers candidates with unfinished `depends_on` prereqs. Ranking adapts to the run's own history (per-type stats in `state.json`).
- **Repo analysis cache**: `.autopilot/analysis.json` caches repository understanding across rounds and auto-invalidates when HEAD or config changes, so the loop does not re-read the whole repo every round.
- **Secret guard**: `commit` scans the staged diff for secret-like content (AWS keys, private keys, GitHub/Slack/Google tokens, `sk-*`) and refuses on a match; `secret-scan` checks the staged diff on demand.
- **Human checkpoints & directives**: `checkpoint_every: N` pauses the loop to consult you every N rounds; `directive-add` records standing rules that every round must honor (`.autopilot/directives.json`) — steer a long run without stopping it.
- **Expansion phase**: `expand_after_goals: true` keeps the loop improving after all goals are met, scouting fresh candidates and value-gating them through the backlog rank instead of stopping or bloating.
- **Quality red line**: `review_threshold` requires every completed round to self-score at or above a minimum before it is recorded.
- **Multiple stop conditions**: goals, `max_rounds`, `max_minutes`, `max_tokens`, `max_blocked_in_a_row`, and a `deadline` timer (e.g. "iterate until tomorrow morning") that stops at an absolute wall-clock moment.
- **Safety rails**: refuses to commit user changes, never rewrites history, defaults to no pushing, optional `allow_paths`/`deny_paths` whitelist.
- **Resumable**: unfinished runs resume from `.autopilot/state.json` instead of starting over.
- **Retrospectives**: `finish` writes `.autopilot/retrospective.md` — per-type completion/blocked stats, blocked rounds, and the next likely improvement.

## Installation

Copy this folder into your agent's skills directory:

- opencode: `~/.config/opencode/skills/auto-iterate-project/`
- Claude Code: `~/.claude/skills/auto-iterate-project/`
- Codex / generic: `~/.codex/skills/` or `~/.agents/skills/auto-iterate-project/`

The skill's `SKILL.md` entry point documents the full workflow. The helper script is standalone and works on Windows, macOS, and Linux (Python 3.6+).

## Quick Start

Inside any git repository:

```powershell
python <skill-dir>/scripts/autopilot_state.py init --repo <repo> --max-rounds 5 --branch-mode feature
```

Then run the loop described in `SKILL.md`: check → analyze → backlog → begin-round → implement → verify → commit → complete-round.

Typical commands:

```powershell
python <skill-dir>/scripts/autopilot_state.py check --repo <repo> --brief
python <skill-dir>/scripts/autopilot_state.py backlog-add --repo <repo> --title "Add docs" --reason "useful" --value 4 --effort 2
python <skill-dir>/scripts/autopilot_state.py begin-round --repo <repo> --title "..." --reason "..." --candidate-id candidate-001
python <skill-dir>/scripts/autopilot_state.py commit --repo <repo> --summary "..."
python <skill-dir>/scripts/autopilot_state.py complete-round --repo <repo> --summary "..." --commit-sha <sha>
python <skill-dir>/scripts/autopilot_state.py finish --repo <repo> --reason "goal met"
```

## Configuration

All options live in `.autopilot/config.json` (created by `init`) and can be set via `init` flags or edited directly. Highlights:

| Field | Default | Purpose |
|---|---|---|
| `candidates_per_round` | `3` | Backlog candidates worked per round; batches several independent changes into one round |
| `commit_every_rounds` | `5` | Commit accumulated changes once per this many rounds (fewer, larger commits) |
| `verify_every_rounds` | `3` | Run full verification once per this many rounds; smoke-check in between |
| `checkpoint_every` | `null` | Pause and ask the user once per this many rounds |
| `expand_after_goals` | `false` | Keep iterating after all goals are met (scout new candidates) |
| `review_threshold` | `null` | `complete-round` requires `--review-score` >= this (1-5) |
| `max_rounds` | `10` | Hard stop after this many completed/blocked rounds |
| `max_minutes` | `null` | Countdown (倒计时): wall-clock budget since last round activity |
| `deadline` | `null` | Timer (定时器): absolute stop time; `init --deadline "+8h"` / `"08:00"` / ISO timestamp |
| `max_tokens` | `null` | Soft token budget |
| `max_round_scope` | `null` | Max changed lines per commit |
| `goals` | `[]` | Explicit goals; loop stops when all are met |
| `check_commands` | `[]` | Exact verification commands to run each verification round |
| `scan_secrets` | `true` | Refuse commits whose staged diff matches a secret pattern |
| `secret_patterns` | `[]` | Extra regex patterns for the commit-time secret scan |
| `type_saturation_threshold` | `2` | Completed same-type candidates before ranking downweights the type |
| `branch_mode` | `current` | `feature` isolates work on an `autopilot/<run-id>` branch |
| `push` | `false` | Push after each completed round with a commit |
| `allow_paths` / `deny_paths` | `[]` | Commit path whitelist/blacklist (fnmatch globs) |
| `allow_uncommitted_changes` | `false` | Allow starting from a dirty tree |
| `report_lang` | `zh` | Language for reports (`zh` or `en`) |

See `references/config.md` for the full reference.

## Testing

```powershell
python <skill-dir>/scripts/test_autopilot_state.py
python -m py_compile <skill-dir>/scripts/autopilot_state.py
```

The test suite spins up throwaway git repos and covers state, backlog, round lifecycle, budgets, branches, and JSON output.

## Project Layout

```
auto-iterate-project/
├── SKILL.md                     # Skill entry point and workflow
├── agents/openai.yaml           # Agent store metadata
├── references/config.md         # Full configuration reference
└── scripts/
    ├── autopilot_state.py       # Thin CLI entry point
    ├── test_autopilot_state.py  # Self-test suite
    └── autopilot/               # Implementation package (one module per concern)
        ├── io.py                # time, JSON, git, lock, token helpers
        ├── config.py            # .autopilot/config.json schema + validation
        ├── state.py             # state/backlog, stop conditions, reports
        ├── agent.py             # runtime agent detection
        ├── commands.py          # CLI command handlers
        └── cli.py               # argument parsing + dispatch
```
