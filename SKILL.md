---
name: auto-iterate-project
description: Automatically iterate any git project inside the current agent session by analyzing the repository, choosing the next high-value improvement, implementing small changes, verifying, committing, and looping until a goal is met or configurable round/time/token limits are reached. Use when the user asks for autonomous project iteration, continuous self-improvement, auto-improve, keep improving this project, full-auto development, or wants the agent to keep making and committing improvements without per-step approval. Also use for Chinese requests like 全自动迭代这个项目, 自动改进并提交这个仓库, 连续自动开发, or 自动推进项目改进.
---

# Auto Iterate Project

## Operating Contract

- Operate in the current working directory unless the user names a different git repository path.
- Keep every change small, scoped, and committed within a single round. Each round works on `candidates_per_round` backlog candidates (default `1`, keeping the original one-change-per-round contract); when it is greater than `1`, each candidate is still implemented, verified, and committed as its own commit inside the same round. When `max_round_scope` is set, the commit helper enforces the size limit per commit.
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

- `scripts/autopilot_state.py` is the deterministic state, backlog, branch, budget, and agent-detection helper.
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
python <this-skill>/scripts/autopilot_state.py init --repo <repo> [--branch-mode feature] [--max-rounds N] [--max-minutes N] [--max-tokens N] [--max-round-scope N] [--goal "<goal>"] [--goals-from-prompt "<request>"] [--check-commands "<cmd>"] [--push] [--commit-message-prefix <prefix>] [--retries-per-round N] [--candidates-per-round N] [--max-blocked-in-a-row N] [--allow-path <glob>] [--deny-path <glob>] [--report-lang zh|en] [--track-state]
```

`--goals-from-prompt` splits a natural-language request (Chinese or English) into `goals` automatically. `--allow-path`/`--deny-path` seed the commit path whitelist (repeatable).

7. If `branch_mode` is `feature`, run `ensure-branch`. On a fresh run `init` already created the branch, so this is only needed when resuming; calling it again is safe.
8. If `.autopilot/state.json` already exists and is unfinished, run `read` and `check`, then continue from the current state instead of initializing again.

## Round Loop

Repeat these steps until `check` reports `"continue": false`.

### 1. Check State

Run `python <this-skill>/scripts/autopilot_state.py check --repo <repo>` (add `--brief` to return only `continue`/`stop_reason`/`warnings`, saving tokens on every loop). Stop if it says no. Read the `warnings` array and resolve anything actionable. Warnings now cover dirty trees (versus `allow_uncommitted_changes`), empty goals, missing remotes when `push` is enabled, and missing stop conditions.

### 2. Analyze

Gather:

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

When nothing matches, run `python <this-skill>/scripts/autopilot_state.py detect-verify --repo <repo>` to have the helper scan the entry points and recommend `check_commands` (add `--apply` to write them into the config). When nothing matches at all, run the tool's `--help` as a smoke check, and if verification genuinely cannot run, state that explicitly in the round summary and keep the change low-risk. Never run the loop with an unknown verification path when `check_commands` names a command that does not exist — treat that as a stop-and-ask condition (see Escalation).

### 3. Maintain the Backlog

- Use `backlog-add` to record 3-5 concrete improvement candidates with title, reason, a numeric `value` (1-5) and `effort` (1-5).
- Use `backlog-rank` to list pending candidates sorted by value-to-effort ratio.
- Choose the top `candidates_per_round` pending candidates by value-to-effort ratio (default `1`). Set `candidates_per_round: N` in `.autopilot/config.json` (or `init --candidates-per-round N`) to batch N independent changes per round and amortize the per-round overhead; keep the default `1` for maximal safety.
- Do not combine unrelated candidates into a single commit; when a round carries multiple candidates, commit each one separately.
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

### 5. Implement

When the round carries multiple candidates, work through them **one at a time**: implement candidate 1, verify, commit, then candidate 2, and so on. For each candidate, make the smallest change that satisfies it. Follow existing project patterns. Do not reformat unrelated code, add dependencies, or touch user-owned files. If a single candidate starts growing beyond `max_round_scope`, cancel it and split the work.

### 6. Verify

- If `check_commands` is set, run each of those commands and require them to pass.
- Otherwise, if tests exist, run the project's test command and require it to pass. If build or lint exists, run it too.
- If something fails, fix the cause and retry up to `retries_per_round` (default 3). This budget is an agent-side convention: the helper does not count retries, so enforce it yourself and switch to `block-round` when exhausted.
- If there are no tests/build/lint, only make low-risk changes and state the verification method in the round summary.

### 7. Commit

Only when verification passes. In a multi-candidate round, commit **each candidate's change as its own commit** (all commits in the round share the `<prefix>(round-<N>)` message prefix but carry distinct summaries), then close the round once with `complete-round`.

1. Run `git status` and review the intended changes for scope before staging.
2. `git add <intentionally changed files>` (only this candidate's files)
3. Use the helper to verify the staged scope, check git identity and `max_round_scope`, build the `<prefix>(round-<N>): <summary>` message, and commit:

```powershell
python <this-skill>/scripts/autopilot_state.py commit --repo <repo> --summary "<summary>"
```

`commit` requires an open round; without one it refuses unless you pass `--round <n>` explicitly (use that only to record an intentional orphan commit, e.g. after `cancel-round`). When `allow_paths`/`deny_paths` are configured, the helper refuses to commit any staged file outside the whitelist.

4. Repeat steps 1-3 for each remaining candidate, then record the **last** commit SHA from the helper output in `complete-round`.

Do not stage `.autopilot/` unless config sets `track_state: true`. Do not push unless config sets `push: true`; when it does, `complete-round` pushes automatically (explicit `branch:branch` refspec, never force) and you can also run `python <this-skill>/scripts/autopilot_state.py push --repo <repo>` to push explicitly — the `push` command itself refuses to run when `push: false`. The helper logs every state-changing action to `.autopilot/log.jsonl`.

### 8. Record the Round

- On success: run `complete-round` with title, summary, and commit SHA. Token accounting is estimated automatically from the diff unless you pass `--tokens`.
- On blocked: run `block-round` with title and reason, and do not commit that round.
- To abandon a round without counting it as blocked: run `cancel-round` (the round's candidates return to `pending`).
- The helper automatically updates every backlog candidate attached to the round to `completed`, `blocked`, or back to `pending`.
- **Phase reports**: every time `complete-round` reaches a multiple of 10 completed rounds, the helper writes a Chinese-language phase report to `.autopilot/phase-report-round-<N>.md` (language from config `report_lang`, default `zh`) and prints a `[PHASE]` note. Read it, report the phase summary to the user, and continue.
- **Undoing a bad commit**: run `undo-round --sha <sha> --summary "<why>"` instead of hand-reverting. It `git revert`s the commit (never rewriting history), records a `revert` history entry, and advances the round-number counter so the revert and the next round get distinct numbers.

### 9. Mark Goals

If the round satisfies one of the configured goals, run `goal-met --goal "<exact goal text>"`.

### 10. Repeat

Run `check` again after each round.

## Stop Conditions

Stop when any of these is true:

- All configured goals are met.
- `max_rounds` is reached.
- `max_minutes` is reached. This is wall-clock time since the most recent round activity (`init`/`begin-round`/`complete-round`/`block-round`/`cancel-round`/`goal-met`). Pausing the run does not pause the clock: if the pause is longer than the remaining budget, the resumed run stops on the first `check`. Set `max_minutes` to `null` for unlimited.
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

## Finish

When the loop stops:

1. Run `finish --reason "<stop reason>"`. It auto-cancels any still-open round (returning its candidate to pending) and, in `feature` mode, returns to the branch you started on; pass `--stay` to remain on the autopilot branch.
2. Write `.autopilot/last-summary.md` with completed rounds, blocked rounds, commit SHAs, active branch, remaining goals, backlog status, and the next likely improvement. Write it in the user's language when you know it (the default English trigger suggests English output; the Chinese trigger suggests Chinese). You can generate the underlying data deterministically with `python <this-skill>/scripts/autopilot_state.py report --repo <repo> [--lang zh|en] [--output <file>]`.
3. Report a short summary to the user.

## Reports & Dry-Run

- `report` prints (or writes with `--output`) a deterministic markdown report of the run: goals, round counts, round history, backlog, recent commits, and the next likely improvement. Default language comes from config `report_lang` (`zh` or `en`); override with `--lang`.
- `detect-verify` scans the repo entry points and prints recommended `check_commands`; `--apply` writes them into the config.
- Every state-changing command accepts `--dry-run`: it prints what would happen and changes neither `.autopilot/` nor git. Use it to rehearse a step before committing to it.

## Safety Rules

- Never run `git reset --hard`, `git clean -f`, `git rebase`, `git commit --amend`, force push, or any history-rewriting command. This applies to git commands you run yourself, not just the helper — the helper never issues them.
- Never push unless config sets `push: true`; this skill defaults to false, and the `push` command refuses to run when disabled.
- Never commit user changes that existed before the run unless `allow_uncommitted_changes` is true. The helper now enforces this by refusing `init` and the first `begin-round` on a dirty tree.
- Never ignore a failing verification result to make a commit.
- Never run without a stop condition. The helper warns when none is configured; enforce at least one of goals, `max_rounds`, `max_minutes`, `max_tokens`, or `max_blocked_in_a_row`.
- Never delete files outside the change needed for the current round unless the repo's tests prove the deletion is safe.
- Never switch away from the autopilot feature branch or delete it while a run is active.
- To undo a bad round's commit, use `git revert <sha>` and treat it as a new round; never rewrite history.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `commit` fails with "Git identity is not configured" | `user.name`/`user.email` unset | Run `git config user.name ...` and `git config user.email ...`, then retry |
| `commit` fails after hooks | pre-commit hook rejects the change | Fix the hook failure; never bypass with `--no-verify` unless the user approves |
| `commit` fails with "exceeds max_round_scope" | The round is too large | Split the change into smaller rounds |
| `begin-round` says a round is open | A previous round was interrupted | `read` the state, then `complete-round`, `block-round`, or `cancel-round` |
| `begin-round` fails with "Autopilot is stopped" | A stop condition is already reached | Run `check`, resolve the stop reason (e.g. raise `max_rounds`) or run `finish` |
| `commit` fails with "No round is open" | Nothing began the current round | Run `begin-round` first, or pass `--round <n>` for an intentional orphan commit |
| "Another autopilot run appears active" | Stale `.autopilot/lock` or a real concurrent run | Wait for the other run, or delete `.autopilot/lock` if that process is dead; the helper now also removes corrupt lock files and locks left by other hosts automatically |
| `check` reports `max_minutes` right after resume | Budget measures wall-clock since last activity, and the pause consumed it | Expected behavior; raise `max_minutes` or set it to `null` |
| `git log` fails during analysis | Repo has no commits yet | Skip log analysis; the first round creates the initial commit |
| `begin-round` refuses with "Working tree is dirty" | Dirty tree on the first round with `allow_uncommitted_changes: false` | Commit/stash user changes, or set `allow_uncommitted_changes: true` (or run `init --force` to override) |
| `push` refuses with "push is disabled" | `push: false` in config | Only push when the config enables it; set `push: true` to allow pushing |
| `complete-round` fails with "commit-sha does not resolve" | The SHA recorded by `commit` was not passed through | Use the exact SHA the `commit` helper printed |
| `init` refuses with "Working tree is dirty" | Pre-existing uncommitted changes at run start | Commit/stash them, or pass `--allow-uncommitted-changes` / `--force` |
| `ensure-branch`/`finish` fails with branch errors | `state.json` was hand-edited or the origin branch was deleted | Reset `.autopilot/state.json` and re-run; `finish` only warns when the origin branch is gone |
| `commit` fails with "violate allow_paths/deny_paths" | A staged file is outside the path whitelist | Only stage files the whitelist permits, or adjust `allow_paths`/`deny_paths` |
| `undo-round` fails with "revert failed" | The revert conflicts with later commits | Resolve the conflict manually, commit, then record the round with `commit`/`complete-round` |
| `detect-verify` reports nothing | No recognized build/test config | Set `check_commands` manually or pass `--check-commands` on init |
| Tests fail under `python` but not `python3` | Mixed Python installations | Use `py`/`python3` consistently via the Environment note |
