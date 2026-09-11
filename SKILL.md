---
name: auto-iterate-project
version: 1.0.0
description: Automatically iterate any git project inside the current agent session by analyzing the repository, choosing the next high-value improvement, implementing small changes, verifying, committing, and looping until a goal is met or configurable round/time/token limits are reached. Use when the user asks for autonomous project iteration, continuous self-improvement, auto-improve, keep improving this project, full-auto development, or wants the agent to keep making and committing improvements without per-step approval. Also use for Chinese requests like 全自动迭代这个项目, 自动改进并提交这个仓库, 连续自动开发, or 自动推进项目改进.
---

# Auto Iterate Project

## Operating Contract

- Operate in the current working directory unless the user names a different git repository path.
- Batch by default for efficiency. Each round works on `candidates_per_round` backlog candidates (default `3`); each candidate is implemented and verified as its own unit inside the round. Full regression verification runs once every `verify_every_rounds` rounds (default `3`). Commits are deferred and flushed once every `commit_every_rounds` rounds (default `5`), so work accumulates across rounds and is committed as one batch instead of per candidate. Set any of these to `1` for the original one-change-per-round / verify-and-commit-every-round contract.
- Commit only after verification. Do not push by default.
- Do not rely on host-specific goal tools. This skill owns its loop and state through `.autopilot/`.
- Maintain a visible improvement backlog in `.autopilot/backlog.json`.
- Use `branch_mode: feature` when autonomous work should be isolated from the current branch.
- Resume unfinished state from `.autopilot/state.json` instead of starting over.

## Locate Resources

Resolve this skill directory from this `SKILL.md`:

1. This file is the skill's entry point; its parent directory is the skill root.
2. Resolve the skill root to an **absolute path** and substitute it for every `<this-skill>` below before running any command.
3. If the environment provides a `$SKILL_DIR` variable, prefer it; otherwise use the path you resolved.

- `scripts/autopilot_state.py` is the deterministic state, backlog, branch, budget, ranking, secret-scan, and agent-detection helper.
- `references/config.md` documents every configuration field.
- `scripts/test_autopilot_state.py` is the self-test suite for the helper.

## Detect the Runtime Agent

Before running any command, detect which agent runtime is driving this session and adapt command syntax, the Python launcher, and path references accordingly:

```powershell
python <this-skill>/scripts/autopilot_state.py detect-agent --repo <repo>
```

The helper returns JSON with:

- `agent` — `opencode`, `claude-code`, `codex`, or `generic` (unknown).
- `detected_by` — the signal that won: an `AUTOPILOT_AGENT` override env var, a runtime env var (`OPENCODE`, `CLAUDE_CODE`, `CODEX`), a project marker file (`opencode.json`, `CLAUDE.md`, `AGENTS.md`), or the fallback.
- `shell` / `adaptation.command_style` — `powershell` or `bash`. **Use the detected style for every command example in this file**, not the literal `powershell` syntax shown below.
- `python_cmd` / `adaptation.use_python` — the launcher to use (`python`, `python3`, or `py` on Windows).
- `skill_dir` — where this runtime expects skills to live.
- `agent_config` — the file to touch if you need to register agent-level config for this runtime.

Detection precedence is: `AUTOPILOT_AGENT` override > runtime env var > project marker file > home marker file > `generic` fallback. You can force an agent with `AUTOPILOT_AGENT=opencode|claude-code|codex|generic` in the environment.

## Environment

- Use `python` first; if it is missing, try `python3`, then `py` (Windows launcher). Use the exact `python_cmd` reported by `detect-agent`; if that launcher fails to run, fall back to `python3` then `py`. The helper targets Python 3.6+.
- Command examples in this file use PowerShell syntax by default. **When `detect-agent` reports `bash`, translate quoting accordingly** (for example, `$SKILL_DIR` on bash). When it reports `powershell`, keep the examples as written.
- The helper works on Windows, macOS, and Linux; git is the only external dependency.

## Setup

1. Run `detect-agent --repo <repo>` and note the reported `agent`, `shell`/`command_style`, and `python_cmd`. Apply them to every command below.
2. Resolve the target repository root. If the user gives a path, use that path; otherwise use the current working directory.
3. Run `git rev-parse --is-inside-work-tree` in that path. Stop if it is not a git repository.
4. Run `git status --short --branch`. If there are uncommitted changes and the config does not allow them, stop and report that the repo must be clean or `allow_uncommitted_changes` must be true. This check is also **enforced in code**: `init` refuses to start and `begin-round` refuses to open the first round when the tree is dirty and `allow_uncommitted_changes` is false (changes under `.autopilot/` are ignored).
5. Run `diagnose` to surface environment risks (no commits yet, detached HEAD, missing git identity, dirty tree, pre-commit hooks) and resolve them before starting:

```powershell
python <this-skill>/scripts/autopilot_state.py diagnose --repo <repo>
```

6. Read `.autopilot/config.json` if it exists. If it does not exist, initialize with:

```powershell
python <this-skill>/scripts/autopilot_state.py init --repo <repo> [--branch-mode feature] [--max-rounds N] [--max-minutes N] [--deadline "<expr>"] [--max-tokens N] [--max-round-scope N] [--goal "<goal>"] [--goals-from-prompt "<request>"] [--check-commands "<cmd>"] [--push] [--commit-message-prefix <prefix>] [--retries-per-round N] [--candidates-per-round N] [--commit-every-rounds N] [--verify-every-rounds N] [--checkpoint-every N] [--expand-after-goals] [--review-threshold N] [--max-blocked-in-a-row N] [--scan-secrets] [--no-scan-secrets] [--secret-pattern <regex>] [--type-saturation-threshold N] [--allow-path <glob>] [--deny-path <glob>] [--report-lang zh|en] [--track-state]
```

`--deadline` is the **timer (定时器)** stop: an absolute wall-clock moment when the run must stop, unlike the `--max-minutes` countdown (倒计时) which measures duration since the last round activity. Accepts an ISO timestamp (`2026-08-10T08:00:00`), a relative duration (`+8h`, `+30min`, `+1d`, `+2w`), or a local `HH:MM` (today, or tomorrow if already passed — e.g. `08:00` for "iterate until tomorrow morning"). It is resolved to an absolute UTC timestamp at init time. See `references/config.md` for details.

`--goals-from-prompt` splits a natural-language request (Chinese or English) into `goals` automatically. `--allow-path`/`--deny-path` seed the commit path whitelist (repeatable).

7. If `branch_mode` is `feature`, run `ensure-branch`. On a fresh run `init` already created the branch, so this is only needed when resuming; calling it again is safe.
8. If `.autopilot/state.json` already exists and is unfinished, run `read` and `check`, then continue from the current state instead of initializing again.

## Round Loop

Repeat these steps until `check` reports `"continue": false`.

### 1. Check State

Run `python <this-skill>/scripts/autopilot_state.py check --repo <repo>` (add `--brief` to return only `continue`/`stop_reason`/`warnings`, saving tokens on every loop). Stop if it says no. Read the `warnings` array and resolve anything actionable. Warnings cover dirty trees (versus `allow_uncommitted_changes`), empty goals, missing remotes when `push` is enabled, missing stop conditions, detached HEAD, and `.autopilot/config.json` changes since `init` (a config fingerprint is stored in state; check_commands are executed and budgets trusted by the loop, so verify such a change was intentional before continuing).

### 2. Analyze

Use the analysis cache to avoid re-reading the whole repo every round:

1. Run `python <this-skill>/scripts/autopilot_state.py analysis-load --repo <repo>`.
2. If it reports `"valid": true`, reuse the cached analysis and do **not** re-scan the tree, README, or CI files. Update only what the current candidate needs.
3. If it reports `missing`/`stale`, gather fresh context and save it back with `analysis-save --content '<json>'` so the next rounds inherit this understanding. The cache auto-invalidates when `git HEAD` moves or `.autopilot/config.json` changes, so it never goes stale silently.

Fresh analysis gathers:

- `git log --oneline -20` (skip silently if the repo has no commits yet)
- `git status --short`
- project tree at the top two levels
- README or docs
- TODO/FIXME/HACK markers
- test files, build config, lint config, CI files
- current test/build failures if any
- if `check_commands` is set in config, prefer those exact commands over re-discovery

To discover the test/build/lint command, do not guess blindly. Decide from the repo's entry points, in priority order:

| Repo signal | Prefer | Fallback |
|---|---|---|
| `pyproject.toml` / `setup.py` / `pytest.ini` | `pytest` (or `python -m pytest`) | `python -m py_compile <package>` smoke |
| `package.json` | `npm test` / `npm run lint` | `node --check` on entry files |
| `Cargo.toml` | `cargo test` | `cargo check` |
| `go.mod` | `go test ./...` | `go vet ./...` |
| `Makefile` | documented `test`/`lint` targets | `make -n` dry run |
| `CMakeLists.txt` | build-then-`ctest` | `cmake --build .` |

When nothing matches, run `python <this-skill>/scripts/autopilot_state.py detect-verify --repo <repo>` to have the helper scan the entry points and recommend `check_commands` (add `--apply` to write them into the config). `detect-verify` also reports `gitleaks`/`detect-secrets` when installed so they can be added to the verification set. When nothing matches at all, run the tool's `--help` as a smoke check, and if verification genuinely cannot run, state that explicitly in the round summary and keep the change low-risk. Never run the loop with an unknown verification path when `check_commands` names a command that does not exist — treat that as a stop-and-ask condition (see Escalation).

### 3. Maintain the Backlog

- Use `backlog-add` to record 3-5 concrete improvement candidates with title, reason, a numeric `value` (1-5), `effort` (1-5), a `type` (`bugfix|feature|refactor|perf|test|docs`, default `feature`), an optional `risk` (1-5, default 1), and optional `depends-on <candidate-id>` prereqs.
- Use `backlog-rank` to list pending candidates sorted by **adjusted** value/effort. The rank is not a raw ratio: it discounts high `risk`, downweights types you have already saturated (see `type_saturation_threshold`, default 2), downweights types that keep blocking, and pushes candidates with unfinished `depends_on` prereqs to the bottom (`"ready": false`, with `blocked_by` reasons and a `score_breakdown` showing every factor). Read the breakdown to pick deliberately instead of reflexively.
- Choose the top `candidates_per_round` **ready** pending candidates (default `3`). Set `candidates_per_round: N` in `.autopilot/config.json` (or `init --candidates-per-round N`) to batch N independent changes per round and amortize the per-round overhead.
- Do not combine unrelated candidates into a single change; within a round, each candidate is still implemented and reviewed as its own unit.
- Quality gate: only open a round whose changes you can justify in one concrete sentence each ("why is this valuable to the user"). If the best available candidate has no clear value, stop and ask the user instead of producing trivial churn.

### 4. Start the Round

Run `begin-round` with a title, a one-sentence reason, and each chosen candidate id. With `candidates_per_round` set to N, pass `--candidate-id` once per chosen candidate:

```powershell
python <this-skill>/scripts/autopilot_state.py begin-round --repo <repo> --title "<title>" --reason "<reason>" --candidate-id <id>
python <this-skill>/scripts/autopilot_state.py begin-round --repo <repo> --title "<title>" --reason "<reason>" --candidate-id <id1> --candidate-id <id2> --candidate-id <id3>
```

All passed candidates are marked `picked`; the helper records the list in `current_round.candidate_ids` and updates every one of them to `completed`/`blocked`/`pending` when the round closes.

`begin-round` refuses to open a round when a stop condition is already reached (all goals met, `max_rounds`, `max_minutes`, `max_tokens`, `max_blocked_in_a_row`, or a finished run). It also refuses to open the **first** round of a run on a dirty tree when `allow_uncommitted_changes` is false (later rounds only warn, so a cancelled round's leftover changes never deadlock the loop). If it fails, run `check` and resolve the stop reason instead.

Cancelled rounds advance the round-number counter (`cancelled_rounds`), so `cancel-round` followed by `begin-round` produces a new round number instead of reusing the old one.

### 5. Implement & Self-Review

When the round carries multiple candidates, work through them **one at a time**. For each candidate, make the smallest change that satisfies it. Follow existing project patterns. Do not reformat unrelated code, add dependencies, or touch user-owned files. If a single candidate starts growing beyond `max_round_scope`, cancel it and split the work.

Before a candidate moves on to verification, do a **self-review** of its diff (`git diff --cached` after staging, or `git diff` before): check for scope creep, dead/debug code, broken edge cases, and anything that looks like a secret. For **blast radius**, when a change touches a public API signature or renames a symbol, `grep` for all callers first and confirm every one is updated or intentionally left. If the review finds real problems, fix them inside the `retries_per_round` budget; this is the gate that keeps batched, low-frequency commits from accumulating junk. For a structured pass, you may use your `requesting-code-review` skill here.

### 6. Verify

Verify **every `verify_every_rounds` rounds** (default 3), and always on a round that commits or when the run is about to stop — never commit unverified work. `check` reports the next verification round in `next_verify_round`. Between verification rounds, run only a fast smoke check when one is cheap (`python -m py_compile`, `node --check`, `cargo check`); if none is cheap, keep the change low-risk and state the lightweight verification method in the round summary.

On a verification round:

- If `check_commands` is set, run each of those commands and require them to pass.
- Otherwise, if tests exist, run the project's test command and require it to pass. If build or lint exists, run it too.
- If something fails, fix the cause and retry up to `retries_per_round` (default 3). This budget is an agent-side convention: the helper does not count retries, so enforce it yourself and switch to `block-round` when exhausted.
- If there are no tests/build/lint, only make low-risk changes and state the verification method in the round summary.

### 7. Commit

Commits are deferred and flushed once every `commit_every_rounds` rounds (default 5): rounds 1-4 implement, verify, and record work without committing, and the boundary round stages **all accumulated changes** and commits them as one batch. `check` reports the next commit round in `next_commit_round`. If the run stops mid-batch (goal met, budget reached), flush the pending changes as a final commit before `finish`. Only commit after verification passes, and only stage this batch's files — never `.autopilot/` (unless `track_state: true`).

1. Run `git status` and review the intended changes for scope before staging.
2. `git add <intentionally changed files>` for the whole batch.
3. Use the helper to verify the staged scope, check git identity, `max_round_scope`, `allow_paths`/`deny_paths`, and **scan the staged diff for secret-like content** (AWS keys, private keys, GitHub/Slack/Google tokens, `sk-*`). On a match it refuses to commit — remove the secret, or commit with `--allow-secrets` / set `scan_secrets: false` only when you are certain. It then builds the `<prefix>(round-<N>): <summary>` message and commits:

```powershell
python <this-skill>/scripts/autopilot_state.py commit --repo <repo> --summary "<summary>"
```

`commit` requires an open round; without one it refuses unless you pass `--round <n>` explicitly (use that to record the final flush after the last `complete-round`). If the accumulated diff exceeds `max_round_scope`, stage a subset, commit, then stage the rest and commit again — same round, multiple commits.

Do not push unless config sets `push: true`; when it does, `complete-round` pushes automatically on rounds that committed (explicit `branch:branch` refspec, never force) and you can also run `python <this-skill>/scripts/autopilot_state.py push --repo <repo>` to push explicitly — the `push` command itself refuses to run when `push: false`. The helper logs every state-changing action to `.autopilot/log.jsonl`.

### 8. Record the Round

- On success: run `complete-round` with title and summary. On a commit round, pass `--commit-sha <sha>` from the commit helper; on a deferred round, omit it — the round is recorded and the changes stay in the working tree for the next batch. If `review_threshold` is configured (1-5), `complete-round` requires a `--review-score` at least as high — self-review the round against the quality gate first and pass `--review-notes` to record the rationale. Token accounting is estimated automatically from the round's own diff (no double counting of earlier uncommitted batches) unless you pass `--tokens`.
- On blocked: run `block-round` with title and reason, and do not commit that round.
- To abandon a round without counting it as blocked: run `cancel-round` (the round's candidates return to `pending`).
- The helper automatically updates every backlog candidate attached to the round to `completed`, `blocked`, or back to `pending`, and refreshes the per-type stats (`state.type_stats`) that power ranking and the retrospective.
- **Phase reports**: every time `complete-round` reaches a multiple of 10 completed rounds, the helper writes a Chinese-language phase report to `.autopilot/phase-report-round-<N>.md` (language from config `report_lang`, default `zh`) and prints a `[PHASE]` note. Read it, report the phase summary to the user, and continue.
- **Undoing a bad commit**: run `undo-round --sha <sha> --summary "<why>"` instead of hand-reverting. It `git revert`s the commit (never rewriting history), records a `revert` history entry, and advances the round-number counter so the revert and the next round get distinct numbers.

### 9. Mark Goals

If the round satisfies one of the configured goals, run `goal-met --goal "<exact goal text>"`.

### 10. Repeat

Run `check` again after each round.

## Human Checkpoints & Directives

Set `checkpoint_every: N` to make the loop pause and consult the user every N rounds (`check` reports the next pause in `next_checkpoint_round`). At a checkpoint round, stop after `complete-round`/`block-round`, then:

1. Summarize progress: completed/blocked rounds, `backlog-rank` top candidates, tokens used, recent commits.
2. Read `directive-list` to surface standing directives.
3. Ask the user: continue, change direction (add/edit goals), raise/lower budgets, or stop. Write down any new standing instructions with `directive-add` so every future round honors them.

Directives are persistent — use `python <this-skill>/scripts/autopilot_state.py directive-add --repo <repo> --text "<standing rule>"` and `directive-list` to read them. At the start of **every** round's Implement step, read `directive-list` and honor the standing rules before writing any code. Directives are how you steer a long autonomous run without stopping it.

## Expansion Phase

When `expand_after_goals: true` and all configured goals are met, the loop does **not** stop: `check` reports `"phase": "expand"` and `begin-round` keeps working. This mirrors the "goal-met -> keep improving" pattern: once the core goal is real, the loop switches from chasing the goal to proposing genuinely valuable adjacent work instead of stopping or grinding.

In the expansion phase:

- **Scout with fresh eyes**: do not reuse the same analysis that got you here. Run `analysis-save` with a fresh scan, and propose 3-5 expansion candidates from a detached perspective (what would a user or a different engineer want next?).
- **Value-gate before building**: add candidates with `type`/`value`/`effort`/`risk` and let `backlog-rank` score them. A candidate whose value cannot be stated in one concrete sentence is rejected — no random feature bloat. Prefer candidates in a `type` you have not saturated (`score_breakdown.saturation_factor` exposes this).
- Keep honoring `review_threshold`, verification, and commit batching as usual.

Expansion is still bounded by the normal stop conditions (`max_rounds`, budgets, `max_blocked_in_a_row`). Since goals no longer stop the loop in this mode, keep at least one budget configured, or `check` warns there is no automatic stopping point.

## Stop Conditions

Stop when any of these is true:

- All configured goals are met.
- `max_rounds` is reached.
- `max_minutes` is reached. This is wall-clock time since the most recent round activity (`init`/`begin-round`/`complete-round`/`block-round`/`cancel-round`/`goal-met`). Pausing the run does not pause the clock: if the pause is longer than the remaining budget, the resumed run stops on the first `check`. Set `max_minutes` to `null` for unlimited.
- `deadline` is reached. The timer (定时器) stops the run at an **absolute** wall-clock moment (for example "iterate until tomorrow morning"), independent of round activity. It complements `max_minutes`: the countdown (倒计时) measures duration since the last round; the deadline is a fixed point in time. Set it with `init --deadline <expr>`; a deadline already in the past stops the run on the first `check`. Set `deadline` to `null` to disable.
- `max_tokens` soft budget is reached (auto-estimated from diffs; see `references/config.md`).
- `max_blocked_in_a_row` consecutive blocked rounds is reached (default 2).
- A round's staged diff exceeds `max_round_scope` and cannot be split.
- The user interrupts or changes the request.

## Escalation

Stop the loop and ask the user when any of these is true, instead of grinding on:

- A `check_commands` command does not exist, or verification cannot be run at all.
- `goals` is empty, `max_rounds`/`max_minutes`/`max_tokens` are all unset, and there is no other stop condition — the loop has no automatic stopping point.
- The backlog has no pending candidates left, or the best candidate's value cannot be stated in one concrete sentence.
- `max_round_scope` is exceeded in a way that cannot be split into smaller rounds.
- The tree is dirty, `allow_uncommitted_changes` is false, and the dirty state cannot be resolved by committing or reverting.
- A checkpoint round is reached (`checkpoint_every`) — pause and consult the user before continuing.
- `expand_after_goals` is on, goals are met, and the expansion phase has no remaining budget to bound it.

## Finish

When the loop stops:

1. If `commit_every_rounds` is enabled and the last round did not commit, **flush the pending changes** with a final `commit --round <n> --summary "flush accumulated changes"` (or commit within the open round) so no verified work is left uncommitted.
2. Run `finish --reason "<stop reason>"`. It auto-cancels any still-open round (returning its candidate to pending), writes `.autopilot/retrospective.md` (per-type completion/blocked stats, blocked rounds, verification commands, next likely improvement), and in `feature` mode returns to the branch you started on; pass `--stay` to remain on the autopilot branch.
3. Write `.autopilot/last-summary.md` with completed rounds, blocked rounds, commit SHAs, active branch, remaining goals, backlog status, and the next likely improvement. Write it in the user's language when you know it (the default English trigger suggests English output; the Chinese trigger suggests Chinese). You can generate the underlying data deterministically with `python <this-skill>/scripts/autopilot_state.py report --repo <repo> [--lang zh|en] [--output <file>]`.
4. Report a short summary to the user.

## Reports & Dry-Run

- `report` prints (or writes with `--output`) a deterministic markdown report of the run: goals, round counts, round history, backlog, recent commits, and the next likely improvement. Default language comes from config `report_lang` (`zh` or `en`); override with `--lang`.
- `retrospective` prints (or writes with `--output`) the run-level retrospective: per-type completed/blocked/blocked-rate/avg-effort/avg-value, blocked rounds, configured verification commands, and the next ready candidate. `finish` writes it automatically to `.autopilot/retrospective.md`.
- `analysis-load` / `analysis-save` read and refresh the `.autopilot/analysis.json` repository-analysis cache (invalidated on HEAD or config change).
- `secret-scan` scans the staged diff for secret-like content; `commit` runs the same scan automatically and refuses on a match (see Safety Rules).
- `directive-add` / `directive-list` manage standing directives (`.autopilot/directives.json`) that every round must honor.
- `detect-verify` scans the repo entry points and prints recommended `check_commands` (including `gitleaks`/`detect-secrets` when installed); `--apply` writes them into the config.
- Every state-changing command accepts `--dry-run`: it prints what would happen and changes neither `.autopilot/` nor git. Use it to rehearse a step before committing to it.

## Safety Rules

- Never run `git reset --hard`, `git clean -f`, `git rebase`, `git commit --amend`, force push, or any history-rewriting command. This applies to git commands you run yourself, not just the helper — the helper never issues them.
- Never push unless config sets `push: true`; this skill defaults to false, and the `push` command refuses to run when disabled.
- Never commit user changes that existed before the run unless `allow_uncommitted_changes` is true. The helper now enforces this by refusing `init` and the first `begin-round` on a dirty tree.
- Never ignore a failing verification result to make a commit.
- Never commit content that looks like a secret. The `commit` helper scans the staged diff for common secret patterns (AWS `AKIA*` keys, private-key blocks, GitHub/Slack/Google tokens, `sk-*`) and refuses on a match; only bypass with `--allow-secrets` (or `scan_secrets: false`) when you have verified the content is not sensitive. Run `secret-scan` at any time to check the staged diff.
- Never run without a stop condition. The helper warns when none is configured; enforce at least one of goals, `max_rounds`, `max_minutes`, `max_tokens`, or `max_blocked_in_a_row`.
- Never delete files outside the change needed for the current round unless the repo's tests prove the deletion is safe.
- Never switch away from the autopilot feature branch or delete it while a run is active.
- To undo a bad round's commit, use `git revert <sha>` and treat it as a new round; never rewrite history.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `commit` fails with "Git identity is not configured" | `user.name`/`user.email` unset | Run `git config user.name ...` and `git config user.email ...`, then retry |
| `commit` fails after hooks | pre-commit hook rejects the change | Fix the hook failure; never bypass with `--no-verify` unless the user approves |
| `commit` fails with "exceeds max_round_scope" | The accumulated batch is too large | Stage a subset, commit, then stage the rest and commit again within the same round |
| `commit` fails with "Secret-like content detected" | The staged diff matches a secret pattern | Remove the secret, or commit with `--allow-secrets` / set `scan_secrets: false` after verifying it is not sensitive |
| `begin-round` says a round is open | A previous round was interrupted | `read` the state, then `complete-round`, `block-round`, or `cancel-round` |
| `begin-round` fails with "Autopilot is stopped" | A stop condition is already reached | Run `check`, resolve the stop reason (e.g. raise `max_rounds`) or run `finish` |
| `commit` fails with "No round is open" | Nothing began the current round | Run `begin-round` first, or pass `--round <n>` for an intentional orphan commit |
| "Another autopilot run appears active" | Stale `.autopilot/lock` or a real concurrent run | Wait for the other run, or delete `.autopilot/lock` if that process is dead; the helper now also removes corrupt lock files and locks left by other hosts automatically |
| `check` reports `max_minutes` right after resume | Budget measures wall-clock since last activity, and the pause consumed it | Expected behavior; raise `max_minutes` or set it to `null` |
| `check` reports `deadline reached` right after resume | The absolute `deadline` moment has already passed | Expected behavior; raise it with `init --force --deadline <expr>` or set `deadline` to `null` in config |
| `git log` fails during analysis | Repo has no commits yet | Skip log analysis; the first round creates the initial commit |
| `begin-round` refuses with "Working tree is dirty" | Dirty tree on the first round with `allow_uncommitted_changes: false` | Commit/stash user changes, or set `allow_uncommitted_changes: true` (or run `init --force` to override) |
| `begin-round` refuses with "depends on unfinished work" | The candidate's `depends_on` prereq is not completed | Complete and record the prereq first, or pick a dependency-ready candidate (`backlog-rank` marks `"ready": false`) |
| `complete-round` refuses with "review score" | `review_threshold` is set and `--review-score` is missing or below it | Self-review the round on 1-5 and pass `--review-score` (>= threshold), or `block-round` and rework |
| `goal-met` reports expansion instead of stopping | `expand_after_goals: true` | Expected; the loop keeps improving until a budget stops it. Disable the flag to stop at the goal |
| `push` refuses with "push is disabled" | `push: false` in config | Only push when the config enables it; set `push: true` to allow pushing |
| `complete-round` without a commit SHA | Deferred commit round (default `commit_every_rounds: 5`) | Expected; the round is recorded and changes stay in the working tree until the boundary round commits them. Flush pending changes before `finish` |
| `complete-round` fails with "commit-sha does not resolve" | The SHA recorded by `commit` was not passed through | Use the exact SHA the `commit` helper printed |
| `init` refuses with "Working tree is dirty" | Pre-existing uncommitted changes at run start | Commit/stash them, or pass `--allow-uncommitted-changes` / `--force` |
| `ensure-branch`/`finish` fails with branch errors | `state.json` was hand-edited or the origin branch was deleted | Reset `.autopilot/state.json` and re-run; `finish` only warns when the origin branch is gone |
| `commit` fails with "violate allow_paths/deny_paths" | A staged file is outside the path whitelist | Only stage files the whitelist permits, or adjust `allow_paths`/`deny_paths` |
| `undo-round` fails with "revert failed" | The revert conflicts with later commits | Resolve the conflict manually, commit, then record the round with `commit`/`complete-round` |
| `detect-verify` reports nothing | No recognized build/test config | Set `check_commands` manually or pass `--check-commands` on init |
| Tests fail under `python` but not `python3` | Mixed Python installations | Use `py`/`python3` consistently via the Environment note |
