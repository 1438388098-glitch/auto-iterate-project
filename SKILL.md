---
name: auto-iterate-project
version: 1.5.0
description: Automatically iterate any git project inside the current agent session by analyzing the repository, choosing the next high-value improvement, implementing small changes, verifying, committing, and looping until a goal is met or configurable round/time/token limits are reached. Use when the user asks for autonomous project iteration, continuous self-improvement, auto-improve, keep improving this project, full-auto development, or wants the agent to keep making and committing improvements without per-step approval. Also use for Chinese requests like 全自动迭代这个项目, 自动改进并提交这个仓库, 连续自动开发, or 自动推进项目改进. Do NOT use for one-off bugfixes, single-file edits, doc-only changes, or when the user wants step-by-step approval of each change.
---

# Auto Iterate Project

## Important (red lines)

1. Never rewrite git history (`reset --hard`, `commit --amend`, force push, rebase).
2. Never push unless `push: true` in config.
3. Commit only after verification passes.
4. Never run without a stop condition (goals, `max_rounds`, `max_minutes`, `max_tokens`, `deadline`, or `max_blocked_in_a_row`).
5. Never idle: while `check` says continue, `mine` / begin a round / Deep Expansion immediately.
6. Touching this skill's own scripts requires `python scripts/test_autopilot_state.py` green before you finish.
7. **Mining before invention**: when you do not know what to do, run `mine --apply` first; lens-scan Deep Expansion is the second wave, not the first.

## Operating Contract

- Operate in the current working directory unless the user names a different git repository path.
- Batch by default. Each round works on `candidates_per_round` backlog candidates (default `4`); each candidate is implemented and verified as its own unit. Full regression verification runs once every `verify_every_rounds` rounds (default `3`). Commits are deferred and flushed once every `commit_every_rounds` rounds (default `5`). Set any of these to `1` for the original one-change-per-round contract.
- Maintain a visible improvement backlog in `.autopilot/backlog.json`.
- When `check` reports `action_hint: "mine"`, run `python <this-skill>/scripts/autopilot_state.py mine --repo <repo> --apply` immediately, then `backlog-rank` and open a round. When it reports `action_hint: "expand"`, mine is already fresh — run Deep Expansion. Empty backlog is not a stop.
- Use `branch_mode: feature` when autonomous work should be isolated from the current branch.
- Resume unfinished state from `.autopilot/state.json` instead of starting over.
- Do not rely on host-specific goal tools. This skill owns its loop and state through `.autopilot/`.

## Locate Resources

This file is the skill entry point; its parent directory is the skill root. Resolve it to an **absolute path** and substitute it for every `<this-skill>` below. If `$SKILL_DIR` is set, prefer it.

- `scripts/autopilot_state.py` — deterministic state, backlog, branch, budget, ranking, secret-scan, and agent-detection helper.
- `scripts/test_autopilot_state.py` — self-test suite (run this after changing `scripts/`).
- `references/config.md` — every configuration field.
- `references/expansion-lenses.md` — 16-lens Deep Expansion catalog (names must match `EXPANSION_LENSES`).
- `references/wave0-prediction.md` — post-goal direction prediction (Wave 0).
- `references/troubleshooting.md` — symptom → cause → fix.
- `references/overview.md` — install / features / layout.

If this skill directory is a git junction/symlink to a clone, keep that clone on a stable branch: a checkout changes the installed skill.

## Detect the Runtime Agent

Before running any command, detect the runtime and adapt command syntax, the Python launcher, and path references:

```powershell
python <this-skill>/scripts/autopilot_state.py detect-agent --repo <repo>
```

The helper returns JSON with `agent` (`opencode`|`claude-code`|`codex`|`generic`), `detected_by`, `shell` / `adaptation.command_style` (`powershell`|`bash`), `python_cmd` / `adaptation.use_python`, `skill_dir`, and `agent_config`. **Use the detected style for every command below**, not the literal `powershell` syntax shown.

Detection precedence: `AUTOPILOT_AGENT` override > runtime env var (`OPENCODE`/`CLAUDE_CODE`/`CODEX`) > project marker (`opencode.json`/`CLAUDE.md`/`AGENTS.md`) > home marker > `generic`. Force with `AUTOPILOT_AGENT=opencode|claude-code|codex|generic`.

If the host has no Task/Agent/explore tool, set an in-process lens mode (see Deep Expansion fallback) instead of claiming expansion is impossible.

## Environment

- Use the `python_cmd` from `detect-agent`; if it fails, try `python3` then `py`. The helper targets Python 3.6+.
- Translate PowerShell examples when `command_style` is `bash`.
- git is the only external dependency.

## Setup

1. Run `detect-agent --repo <repo>` and note `agent`, `shell`/`command_style`, and `python_cmd`.
2. Resolve the target repository root (user path or cwd).
3. Run `git rev-parse --is-inside-work-tree`. Stop if not a git repository.
4. Run `git status --short --branch`. If dirty and `allow_uncommitted_changes` is false, stop. This is also enforced: `init` and the first `begin-round` refuse a dirty tree (changes under `.autopilot/` are ignored).
5. Run `diagnose` and resolve environment risks (no commits yet, detached HEAD, missing git identity, dirty tree, pre-commit hooks):

```powershell
python <this-skill>/scripts/autopilot_state.py diagnose --repo <repo>
```

6. Read `.autopilot/config.json` if present; otherwise initialize:

```powershell
python <this-skill>/scripts/autopilot_state.py init --repo <repo> [--branch-mode feature] [--max-rounds N] [--max-minutes N] [--deadline "<expr>"] [--max-tokens N] [--goal "<goal>"] [--goals-from-prompt "<request>"] [--check-commands "<cmd>"] [--candidates-per-round N] [--commit-every-rounds N] [--verify-every-rounds N] [--checkpoint-every N] [--expand-after-goals] [--review-threshold N] [--report-lang zh|en]
```

`--deadline` is the timer (定时器) stop: an absolute wall-clock moment. Accepts ISO (`2026-08-10T08:00:00`), relative (`+8h`, `+30min`, `+1d`, `+2w`), or local `HH:MM` (today, or tomorrow if already past). Complements `--max-minutes` (倒计时 = duration since last round activity). Ranking/recovery flags: see `references/config.md`.

7. If `branch_mode` is `feature`, run `ensure-branch` when resuming (init already creates the branch; calling again is safe).
8. If `.autopilot/state.json` exists and is unfinished, run `read` and `check`, then continue from current state.

## Round Loop

Repeat until `check` reports `"continue": false`.

### 1. Check State

```powershell
python <this-skill>/scripts/autopilot_state.py check --repo <repo> --brief
```

`--brief` returns loop-driving fields only (`continue`, `stop_reason`, `warnings`, `goals_met`, `phase`, `backlog`, `action_hint`, next verify/commit/checkpoint rounds). Stop if it says no. Resolve actionable `warnings` (dirty tree, empty goals, missing remotes when pushing, missing stop conditions, detached HEAD, thin backlog, config drift).

### 2. Analyze

Use the analysis cache:

1. `analysis-load --repo <repo>`.
2. If `"valid": true`, reuse it; do **not** re-scan the tree.
3. If missing/stale, gather fresh context and `analysis-save --content '<json>'`. Before every expansion wave, check `analysis` (`status`, `commits_behind`) and rescan + `analysis-save` when stale.

Fresh analysis gathers: `git log --oneline -20` (skip if unborn), `git status --short`, project tree (top two levels), README/docs, TODO/FIXME/HACK, test/build/lint/CI files, current failures, and `check_commands` when set.

Discover test/build/lint in priority order (do not guess blindly):

| Repo signal | Prefer | Fallback |
|---|---|---|
| `pyproject.toml` / `setup.py` / `pytest.ini` | `pytest` | `python -m py_compile <package>` smoke |
| `package.json` | `npm test` / `npm run lint` | `node --check` on entry files |
| `Cargo.toml` | `cargo test` | `cargo check` |
| `go.mod` | `go test ./...` | `go vet ./...` |
| `Makefile` | documented `test`/`lint` targets | `make -n` dry run |
| `CMakeLists.txt` | build-then-`ctest` | `cmake --build .` |

When nothing matches, run `detect-verify --repo <repo>` (`--apply` writes `check_commands`). If verification genuinely cannot run, say so in the round summary and keep changes low-risk. If `check_commands` names a missing command, treat that as stop-and-ask (Escalation).

### 3. Maintain the Backlog

- **Supply first**: if the backlog is thin/empty or `action_hint` is `mine`, run `mine --repo <repo> --apply` (scanners: markers, swallowed, syntax, test-gap, hotspot, dead-export, docs-drift). It writes deduped, evidence-backed candidates. Judgment invention is the fallback, not the default.
- `backlog-add` with title, reason, `value` (1-5), `effort` (1-5), `type` (`bugfix|feature|refactor|perf|test|docs`), optional `risk` (1-5), optional `depends-on`.
- `backlog-rank` sorts by expected value per round (`ranking_mode: expected` is **not** value/effort). Read `score_breakdown`, `selected`, `below_floor`, `cut_reason`, `unlocks`, `ready`/`blocked_by`. `classic` is the legacy ratio.
- Pick the `selected` batch (`candidates_per_round`, default 4). Diversity quota `max_same_type_per_round` (default 2), value floor `min_candidate_value` (default 3). Empty batch while ready entries exist → read `selected_empty_reason` (`quota`/`late_run`/`cutoff`/`floor`) and choose Deep Expansion vs an explicit `--candidate-id` round.
- Do not combine unrelated candidates into one change; each is its own unit inside the round.
- **Thin-backlog rule**: when `action_hint` is `"expand"`, run Deep Expansion **in parallel with** any ready work. Never start a round with zero ready candidates on an empty backlog.
- Quality gate: every candidate needs one concrete user sentence of value. If the best candidate has none, Deep Expansion — do not stop or idle.

### 4. Start the Round

```powershell
python <this-skill>/scripts/autopilot_state.py begin-round --repo <repo> --title "<title>" --reason "<reason>" --candidate-id <id> [--candidate-id <id2> ...]
```

`begin-round` refuses when a stop condition is already reached, when the first round opens on a dirty tree (and `allow_uncommitted_changes` is false), when the stocked backlog has ready above-floor candidates but no `--candidate-id`, or when only below-floor candidates are ready (pass an explicit `--candidate-id` to accept a quick win). Cancelled rounds advance the round counter; a zero-work cancel records `aborted` and burns no round budget / token base.

### 5. Implement & Self-Review

Work multi-candidate rounds **one candidate at a time**. Smallest change that satisfies the candidate; follow project patterns; no drive-by reformat, no new deps, no user-owned files. If a candidate exceeds `max_round_scope`, cancel and split.

Before verify, self-review the diff (`git diff --cached` after staging): scope creep, dead/debug code, broken edges, secret-like content. For blast radius, when a public API signature or symbol rename is touched, `grep` all callers first. Fix real problems within `retries_per_round`. Optionally use the `requesting-code-review` skill here.

### 6. Verify

Full verification every `verify_every_rounds` rounds (default 3), and always on a commit round or before stop — never commit unverified work. Between those, run a cheap smoke check when available.

On a verification round: run each `check_commands` entry (or the project's test/build/lint) and require pass. On failure, fix and retry up to `retries_per_round` (default 3; the helper does not count retries — switch to `block-round` when exhausted). With no tests, keep changes low-risk and state the verification method in the summary.

### 7. Commit

Commits flush every `commit_every_rounds` rounds (default 5) as one batch. On stop mid-batch, flush before `finish`. Never stage `.autopilot/` unless `track_state: true`.

1. `git status` and review scope.
2. `git add <intentionally changed files>` for the whole batch.
3. Helper verifies staged scope, git identity, `max_round_scope`, `allow_paths`/`deny_paths`, and scans for secrets (AWS keys, private keys, GitHub/Slack/Google tokens, `sk-*`, JWTs). On match it refuses — remove the secret or use `--allow-secrets` / `scan_secrets: false` only when certain. Then:

```powershell
python <this-skill>/scripts/autopilot_state.py commit --repo <repo> --summary "<summary>"
```

`commit` requires an open round unless you pass `--round <n>` (use for the final flush after the last `complete-round`). Over `max_round_scope`: stage subset → commit → rest → commit (same round). Do not push unless `push: true`; `complete-round` then pushes (explicit `branch:branch` refspec, never force). Every state-changing action is logged to `.autopilot/log.jsonl`.

### 8. Record the Round

- Success: `complete-round --summary ... [--commit-sha <sha>] --review-score <1-5> [--review-notes ...]`. Omit `--commit-sha` on deferred rounds. `--review-score` is always required in spirit (feeds ranking calibration); `review_threshold` only decides enforcement. `--below-threshold` records a low score as completed (not blocked). Tokens are estimated from the round's own diff unless `--tokens` is passed.
- Blocked: `block-round --title ... --reason ...` (do not commit).
- Abandon without blocked count: `cancel-round` (candidates return to `pending`).
- Every 10 completed rounds the helper writes a phase report to `.autopilot/phase-report-round-<N>.md` (`report_lang`, default `zh`). Read it, report the summary, continue.
- Undo a bad commit: `undo-round --sha <sha> --summary "<why>"` (git revert + history; never rewrite).

### 9. Mark Goals

```powershell
python <this-skill>/scripts/autopilot_state.py goal-met --repo <repo> --goal "<exact goal text>" --round <n> [--evidence "..."] [--next-step "<predicted follow-up>"] [--unlocked-capability "<capability>"] [--seed-type ... --seed-value N --seed-effort N]
```

`--round` must match a completed history entry; without it the goal is **unverified** and withholds the "all goals met" stop. Each `--next-step` creates one direction seed — see `references/wave0-prediction.md`. Prefer explicit `--seed-type`/`--seed-value`/`--seed-effort` over defaults.

### 10. Repeat

Run `check` again after each round.

## Human Checkpoints & Directives

`checkpoint_every: N` pauses every N rounds (`next_checkpoint_round`). At a checkpoint: summarize progress (`backlog-rank` top, tokens, commits), read `directive-list`, then ask the user: continue / change goals / change budgets / stop. Record standing rules with `directive-add`. At the start of **every** round's Implement step, read `directive-list` and honor them.

## Post-Goal Direction Prediction (Wave 0)

After `goal-met`, consume direction seeds **before** any lens scanning. Full protocol: `references/wave0-prediction.md`. Summary:

1. Generate 2-3 "because A, B next, evidence C" hypotheses (enablement / exposure / journey / debt).
2. Verify evidence (reject dead seeds via `seed-reject`).
3. Value-gate (`backlog-add --from-seed` + `backlog-rank`).
4. Work 1-2 via `begin-round`; outcomes resolve seeds automatically.
5. Anti-noise: `origin: predicted`, confidence discount, `max_predicted_per_round` (default 1), late-run cut of predicted work. Expansion-origin is a separate uncapped lane.

Wave 0 is mandatory on the first expand after a goal; lens-scan Deep Expansion is forbidden that wave unless every open seed died in the evidence check.

## Expansion Phase

Expansion is continuous. It runs when `expand_after_goals: true` and goals are met (`phase: expand`), when pending < `min_pending_candidates` (`action_hint: expand`), or when the best ready candidate has no concrete user value.

In every wave: rescan + `analysis-save` if the cache is stale; pick lenses from `lenses_unused`; propose 3-5 candidates from a detached perspective; value-gate before `backlog-add` (`type`/`value`/`effort`/`risk`, evidence in `--evidence`); honor review/verify/commit batching. Record the wave with `expansion-record --lens ...`. Lens catalog: `references/expansion-lenses.md` (names must match `EXPANSION_LENSES` exactly — e.g. `docs` and `ux-copy` are separate lenses).

Expansion is still bounded by normal stop conditions. Keep at least one budget configured in this mode.

## Deep Expansion Protocol

Order of supply when you need work: **(1) `mine --apply` (deterministic) → (2) Wave 0 seeds after goals → (3) lens-scan Deep Expansion (this section)**. Never start here while mine is stale.

When `action_hint: "expand"` (mine already fresh), the backlog is empty after mine, or local analysis cannot justify the next round, run Deep Expansion **immediately**. Failed waves escalate *effort* (lenses, depth, scope, more subagents), never the conversation. Hard stops are only the normal budgets and true external blockers.

### Wave shape

1. Pick 3-6 unused lenses from `check`'s `lenses_unused` (see `references/expansion-lenses.md`). Each subagent returns 2-5 candidates **with evidence**. Record with `expansion-record --lens ...`. Repeating the previous wave's exact set warns.
2. Judge and ingest: one concrete user-facing sentence of value **and** evidence (file:line or probe output). Unevidenced proposals get one follow-up, then reject. `backlog-add` with explicit scores and `--evidence`. Do not reject solely for high effort.
3. Still thin → escalate search depth (not the user): Wave 2 lower the value floor temporarily; Wave 3+ read hardest paths / real user journeys / public symbols; Wave 4+ widen scope (siblings, CI, packaging, examples). Keep spawning until `min_pending_candidates` ready items or a budget stop. **Never finish with "expansion exhausted" while budgets remain** — `finish` without `--force` is refused while stop conditions are unmet and ready work exists. Declaring exhaustion requires 2 consecutive recorded waves (≥3 subagents, ≥3 unused lenses each) with zero value-gated candidates — and even then the run stops only on a budget.
4. **No-subagent fallback**: run the same lenses serially in-process (one lens per pass, `analysis-save` between passes). Do not claim "cannot expand".
5. After goals are met, treat every thin backlog the same way.

## Anti-Idle Discipline

Every `check` with `"continue": true` must be followed by `mine --apply` (when `action_hint` is `mine`), `begin-round`, Deep Expansion, or a scheduled human checkpoint. Forbidden: waiting out the deadline; re-reading without adding work; sitting on "nothing to do"; placeholder/churn rounds; declaring done while pending < `min_pending_candidates` without mine+expansion; skipping Wave 0 when open seeds exist; hypotheses without causal evidence; seeds that are never value-gated; idling for user confirmation of predicted directions.

## Stop Conditions

Stop when any is true:

- All goals are met **and** `expand_after_goals` is false (unverified goals withhold this stop).
- `max_rounds` reached (completed + blocked + cancelled + reverted).
- `max_minutes` reached (wall-clock since last round activity; pause does not pause the clock). `null` = unlimited.
- `deadline` reached (absolute timer). Past deadline stops on first `check`. `null` = disabled.
- `max_tokens` soft budget reached (auto-estimated from diffs).
- `max_blocked_in_a_row` consecutive blocked rounds (default 2).
- A round's staged diff exceeds `max_round_scope` and cannot be split.
- The user interrupts or changes the request.

## Escalation

Stop and ask the user when: a `check_commands` command does not exist / verification cannot run; `goals` is empty and all budgets unset (no automatic stop); `max_round_scope` cannot be split; dirty tree cannot be resolved; a checkpoint round is reached; `expand_after_goals` is on with no remaining budget.

An empty or thin backlog is **never** an escalation.

## Finish

1. If commits are batched and the last round did not commit, flush with `commit --round <n> --summary "flush accumulated changes"`.
2. `finish --reason "<stop reason>"` (add `--stay` to remain on the feature branch). The gate refuses while no stop condition is reached and any of these remains: thin backlog needing expand/mine, any ready candidate (above **or below** `min_candidate_value`), a non-empty recommended batch, or mining not yet exhausted (never-mined ≠ exhausted). `--force` only when the user explicitly asked to stop. Auto-cancels any open round, writes `.autopilot/retrospective.md`, and in `feature` mode returns to the origin branch.
3. Write `.autopilot/last-summary.md` in the user's language (completed/blocked rounds, commits, branch, remaining goals, backlog, next likely improvement). Data: `report --repo <repo> [--lang zh|en] [--output <file>]`.
4. Report a short summary to the user.

## Reports & Dry-Run

- `report` / `retrospective` — deterministic markdown (goals, rounds, backlog, commits, next improvement / type stats).
- `analysis-load` / `analysis-save` — repo analysis cache.
- `secret-scan` — staged-diff secret scan (also automatic on `commit`).
- `directive-add` / `directive-list` / `directive-remove` — standing rules.
- `detect-verify` — recommend `check_commands` (`--apply` writes them).
- `mine [--apply] [--kind K]` — deterministic repo mining into backlog candidates (first move when you do not know what to do).
- Every state-changing command accepts `--dry-run`.

## Safety Rules

- Never run `git reset --hard`, `git clean -f`, `git rebase`, `git commit --amend`, force push, or any history-rewriting command — including commands you run yourself.
- Never push unless `push: true`.
- Never commit pre-existing user changes unless `allow_uncommitted_changes` is true.
- Never ignore a failing verification to make a commit.
- Never commit content that looks like a secret. Scan covers AWS `AKIA*`, private-key blocks, GitHub `ghp_`/`gho_`/`ghs_`/`ghu_`/`ghr_`/`github_pat_`, Slack tokens, Google API keys, JWTs, `sk-*` including `sk-proj-`/`sk-ant-`. Bypass only with `--allow-secrets` (or `scan_secrets: false`) after verifying safety. Unreadable staged diffs and staged BINARY files fail closed (`binary-staged`).
- Never run without a stop condition.
- Never idle until the deadline.
- Never delete files outside the current round's change unless tests prove it safe.
- Never switch away from or delete the autopilot feature branch while a run is active.
- Undo bad commits with `undo-round --sha <sha> --summary "<why>"` only — never hand-rewrite history.

## Troubleshooting

Full table: `references/troubleshooting.md`. Common ones:

| Symptom | Fix |
|---|---|
| Git identity missing | `git config user.name` / `user.email`, retry |
| Dirty tree at `init`/first `begin-round` | Commit/stash, or `allow_uncommitted_changes: true` / `init --force` |
| All goals met, loop continues | `expand_after_goals: true` → `config-set --no-expand-after-goals` to stop at goal |
| `action_hint: mine` | Run `mine --apply` now — do not idle |
| `action_hint: expand` | Mine is fresh; Deep Expansion now |
| `config-set` requires a field | Pass `--expand-after-goals` or `--no-expand-after-goals` |
