# Auto Iterate Project

Version 1.8.0 — see [CHANGELOG.md](CHANGELOG.md) for release history.

Automatically iterate any git project inside an agent session: analyze the repository, pick the next high-value improvement, implement small changes, verify, commit, and loop until a goal is met or a configured budget (rounds / minutes / tokens / absolute deadline) runs out.

Works with opencode, Claude Code, Codex, and other agent runtimes (auto-detected). Git is the only external dependency.

## Why

"Keep improving this project" usually fails one of two ways: the agent idles ("nothing obvious to do"), or it churns without verification. This skill owns the loop deterministically — a visible backlog, per-candidate verification, batched commits, hard early-stop gates, and an anti-idle contract — so a multi-hour autonomous run stays honest without per-step approval. It has been running against its own repository: the 1.6.0 release is the product of a 20-round self-hosted overnight iteration. 1.7.0 is a measured efficiency pass on top of it — the per-round cost of an agent run is the JSON it reads and the number of helper calls it makes, not the helper's runtime.

## Quick start

Tell your agent, in any supported runtime:

```text
Use the $auto-iterate-project skill to autonomously improve this git project.
迭代到明天早上 8 点 / iterate until 8 AM (deadline), or set --max-rounds / --max-minutes / --max-tokens.
```

The skill's entry point is [SKILL.md](SKILL.md) — the agent reads it, runs `detect-agent`, initializes `.autopilot/` state in your repo, and starts the round loop. You stay in control through human checkpoints (`checkpoint_every`), standing rules (`directive-add`), and budgets you can change mid-run (`config-set --clear-max-rounds` and friends).

## How a round works

1. **check** — deterministic stop conditions, budget, and `action_hint` (work / mine / expand / stop).
2. **mine** — seven scanners turn repository facts (markers, swallowed exceptions, syntax errors, test gaps, hotspots, dead exports, docs drift) into evidence-backed backlog candidates; judgment-based Deep Expansion across a rotating 16-lens set is the second wave, never the first.
3. **rank** — candidates scored by expected value per round with diversity quotas, a value floor, dependency unlocks, and a transparent `score_breakdown`.
4. **implement → verify → commit** — each candidate is its own unit; full verification every N rounds and always before a commit; secret-scan on every staged diff.
5. **record** — round history, token accounting, phase reports every 10 rounds, a retrospective at `finish`.

## Highlights

- **Self-owned state**: `.autopilot/` (config, state, backlog, directives, logs) — resumable, migratable, corruption fails clean, and every state write keeps a one-generation `.bak`.
- **Honest accounting**: unverified goals withhold the "all goals met" stop; zero-work cancels consume no budget; `finish` is refused while real work remains unless a stop condition (or you) says otherwise.
- **Safety rails**: never rewrites git history, never pushes unless configured, refuses to absorb pre-existing user changes into autopilot commits, `allow_paths`/`deny_paths` whitelists, secret patterns (AWS / private keys / GitHub / Slack / Google / `sk-*` / JWT).
- **Deterministic helper**: all of the above runs through `scripts/autopilot_state.py` (Python 3.6+, stdlib only), covered by a 528-test suite on 3.8–3.13 across Linux and Windows.
- **Observation dashboard (optional)**: a 127.0.0.1-only read-only web panel that opens when a run starts — an evolution tree (domain → module → file) beside a per-round card stream, with replay. Pure stdlib; the loop never depends on it (`dashboard --stop` shuts it down).

## Install

Copy (or link) the folder into your agent's skills directory — for example `~/.claude/skills/auto-iterate-project/`, `~/.config/opencode/skills/auto-iterate-project/`, or `~/.agents/skills/auto-iterate-project/`. A git junction/symlink keeps the installed skill upgradable via `git pull`. The agent-facing overview lives at [references/overview.md](references/overview.md).

## Documentation

- [SKILL.md](SKILL.md) — the operating contract the agent follows (setup, round loop, stop conditions, escalation).
- [references/config.md](references/config.md) — every config field, default, and command flag.
- [references/troubleshooting.md](references/troubleshooting.md) — symptom → cause → fix.
- [references/expansion-lenses.md](references/expansion-lenses.md) / [references/wave0-prediction.md](references/wave0-prediction.md) — Deep Expansion and post-goal direction prediction.
- [CHANGELOG.md](CHANGELOG.md) — release history.

## Development

```bash
# run the test suite (Windows example; use python3/python consistently elsewhere)
py -3.13 -m pytest scripts/test_autopilot_state.py -q
# or the full runner
python scripts/test_autopilot_state.py
```

CI runs the suite on Python 3.8 and 3.13, Ubuntu and Windows. The helper targets Python 3.6+ with a stdlib-only dependency policy.
