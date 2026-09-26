# Auto Iterate Project

> Historical archive (1.3.3). This repository intentionally ships **no root
> README.md**: skill folders must not ship one — the agent-facing overview
> lives at [references/overview.md](../references/overview.md) and the
> operating contract at [SKILL.md](../SKILL.md). A version-consistency test
> enforces the absence, so do not reintroduce a root README.

Version 1.3.3 — see [CHANGELOG.md](CHANGELOG.md) for release history.

Automatically iterate any git project inside an agent session: analyze the repository, pick the next high-value improvement, implement small changes, verify, commit, and loop until a goal is met or configurable round/time/token limits are reached.

Works with opencode, Claude Code, Codex, and other agent runtimes (auto-detected). Git is the only external dependency.

## Features

- **Self-owned loop**: deterministic state, backlog, branch, and budget management via `scripts/autopilot_state.py`; no host-specific tooling required.
- **Efficiency-first defaults**: one round works on `candidates_per_round` (default 4) candidates, full verification runs every `verify_every_rounds` (default 3) rounds, and commits are flushed once every `commit_every_rounds` (default 5) rounds instead of per candidate — far fewer commits and test runs per improvement.
- **Smart backlog ranking**: candidates are ranked by **expected value per round** (`ranking_mode: expected`, default): value × success rate learned from the type's blocked history × value calibration learned from past self-review scores, with a dependency-unlock bonus, a risk factor that grows stricter as the round budget is consumed, type saturation (logarithmic decay, floored at 0.35 — it dents the diversity signal, never vetoes value), a prospective backlog-mix penalty, and an effort cost that is free within one round's batch width and linear beyond it — cheap busywork can no longer outrank valuable work. A hard value floor (`min_candidate_value`), a per-round type quota (`max_same_type_per_round`), separate origin quotas (`max_predicted_per_round` for unproven predictions, `max_expansion_per_round` default uncapped for value-gated expansion work), and a 40% score cutoff based on the first selected entry shape each recommended batch (`selected` flags with `cut_reason`; `check` reports `selected_count`/`selected_empty_reason` so an empty batch is never mistaken for "no work"). `ranking_mode: classic` restores the legacy value/effort ratio.
- **Repo analysis cache**: `.autopilot/analysis.json` caches repository understanding across rounds and auto-invalidates when HEAD or config changes, so the loop does not re-read the whole repo every round.
- **Secret guard**: `commit` scans the staged diff for secret-like content (AWS keys, private keys, GitHub/Slack/Google tokens, `sk-*`) and refuses on a match; `secret-scan` checks the staged diff on demand.
- **Human checkpoints & directives**: `checkpoint_every: N` pauses the loop to consult you every N rounds; `directive-add` records standing rules that every round must honor (`.autopilot/directives.json`) — steer a long run without stopping it.
- **Expansion phase**: `expand_after_goals: true` keeps the loop improving after all goals are met, scouting fresh candidates and value-gating them through the backlog rank instead of stopping or bloating.
- **Post-goal direction prediction (Wave 0)**: `goal-met --next-step` turns each completed goal into direction seeds — "because we shipped A, B is next" hypotheses with verifiable evidence. After a goal, the first expansion must consume these seeds (verify evidence → value-gate → `backlog-add --from-seed`) before any lens scanning; round outcomes resolve the seeds automatically (`complete-round` → verified, `block-round` → refuted with notes, cancel → open again), closing a hit-rate feedback loop for the predictions.
- **Prediction anti-noise**: promoted seeds are scored with a confidence discount, capped at `max_predicted_per_round` (default 1) per batch, cut in the late run, and tracked in their own blocked/review sub-account — wrong hypotheses sink over time without dragging down observed work, and the report shows the hit rate.
- **Deep Expansion & anti-idle**: empty or thin backlog is never a stop or an escalation — `check` reports `action_hint: expand` when pending candidates fall below `min_pending_candidates` (default 3). The agent must spawn explore subagents across a rotating 16-lens set, keep raising effort when a wave is thin, and never wait out the deadline. Rotation is verifiable: each wave is recorded with `expansion-record --lens ...` (bounded at 20 waves in state), `check` reports `wave_no`/`lenses_used`/`lenses_unused` so the next wave draws from what was not tried, repeating the previous wave's lens set warns, and the `analysis` payload (`status`/`commits_behind`) exposes how stale the cached repo analysis is before a wave rescans.
- **Early-stop hard gates**: `finish` is refused in code while no stop condition is reached and value>=floor ready candidates remain (or the backlog needs expansion) — only a reached stop condition or `finish --force` (user-approved) ends the run. A `goal-met` without `--round <completed round>` is recorded as unverified and withholds the "all goals met" stop; `check` reports unverified goals under `goals_unverified`. `config-set --expand-after-goals` reopens an "all goals met" stop at runtime without a config-drift false alarm.
- **Quality red line**: `review_threshold` requires every completed round to self-score at or above a minimum before it is recorded; `complete-round --below-threshold` admits a quality-failed round as completed with its low score recorded (no blocked count), which is the only in-run exit once `max_blocked_in_a_row` fires.
- **Honest accounting**: a zero-work cancel (no changes, no commit, under 10 minutes) records an `aborted` round that consumes no round budget and no token base; tokens are billed monotonically per run so a line is charged exactly once no matter when it gets committed; `check` reports `blocked_streak`, `budget` (`remaining_minutes`/`deadline_remaining_minutes`), and boundary-correct `next_commit_round`/`next_verify_round`/`next_checkpoint_round` hints.
- **Multiple stop conditions**: goals, `max_rounds`, `max_minutes`, `max_tokens`, `max_blocked_in_a_row`, and a `deadline` timer (e.g. "iterate until tomorrow morning") that stops at an absolute wall-clock moment.
- **Safety rails**: refuses to commit user changes, never rewrites history, defaults to no pushing, optional `allow_paths`/`deny_paths` whitelist.
- **Resumable**: unfinished runs resume from `.autopilot/state.json` instead of starting over.
- **Retrospectives**: `finish` writes `.autopilot/retrospective.md` — per-type completion/blocked stats, blocked rounds, and the next likely improvement.

## Installation

The shipped surface is `SKILL.md`, `scripts/`, `references/`, and `agents/` — everything else in the repository is working material (proposal archive in `docs/`, audit records at the root, CI in `.github/`). Copy (or link) the folder into your agent's skills directory:

- opencode: `~/.config/opencode/skills/auto-iterate-project/`
- Claude Code: `~/.claude/skills/auto-iterate-project/`
- Codex / generic: `~/.codex/skills/` or `~/.agents/skills/auto-iterate-project/`

If you keep a git clone of this repository, prefer a directory junction (Windows) or symlink (macOS/Linux) instead of a copy, so `git pull` upgrades the installed skill in place:

```powershell
# Windows (no admin needed): remove the copy first, then
cmd /c mklink /J "%USERPROFILE%\.agents\skills\auto-iterate-project" "D:\path\to\auto-iterate-project"
# macOS / Linux
ln -s /path/to/auto-iterate-project ~/.agents/skills/auto-iterate-project
```

Caveat: with a link, the installed skill follows the clone's checked-out branch and uncommitted changes — keep the clone on a stable branch.

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
python <skill-dir>/scripts/autopilot_state.py expansion-record --repo <repo> --lens tests --lens performance
python <skill-dir>/scripts/autopilot_state.py config-set --repo <repo> --expand-after-goals
python <skill-dir>/scripts/autopilot_state.py finish --repo <repo> --reason "goal met"
```

## Configuration

All options live in `.autopilot/config.json` (created by `init`) and can be set via `init` flags or edited directly. Highlights:

| Field | Default | Purpose |
|---|---|---|
| `candidates_per_round` | `4` | Backlog candidates worked per round; batches several independent changes into one round |
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
| `ranking_mode` | `expected` | Backlog scoring: `expected` (value × success rate per round) or `classic` (legacy value/effort ratio) |
| `min_candidate_value` | `3` | Candidates below this value are demoted (`below_floor`); quick-wins only fill slots left open by the main pool |
| `max_predicted_per_round` | `1` | Anti-noise quota: max predicted-origin candidates per recommended batch (`null` disables) |
| `max_expansion_per_round` | `null` | Quota for expansion-origin candidates per batch (`null` = uncapped; separate from the predicted quota) |
| `max_same_type_per_round` | `2` | Diversity quota: max same-type candidates per recommended round |
| `min_pending_candidates` | `3` | `check` sets `action_hint: expand` when pending backlog is below this |
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
        ├── state.py             # state/backlog, seeds, stop conditions, reports
        ├── agent.py             # runtime agent detection
        ├── commands.py          # CLI command handlers
        ├── secrets.py           # staged-diff secret scanning
        ├── guard.py             # allow_paths/deny_paths commit path matching
        ├── verify.py            # verification command discovery
        └── cli.py               # argument parsing + dispatch
```
