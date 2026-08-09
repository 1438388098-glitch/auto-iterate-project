# Configuration Reference

## Config File

`.autopilot/config.json` lives at the target repository root. The state helper creates it during `init`; edit it before a run to set limits and goals. Every field has a default, so a minimal file works.

The helper validates field types on every read: a non-object config, a string `max_rounds`, a bad `branch_mode`, or a non-list `check_commands` produces a clear error instead of a traceback. Delete the file and run `init` again to reset.

## Fields

### goals

Array of explicit goals. Empty means open-ended improvement. Use the exact goal text when calling `goal-met`. `init --goals-from-prompt "<request>"` splits a natural-language request (Chinese or English) into `goals` automatically.

```json
{
  "goals": [
    "Make the test suite pass",
    "Add documentation for the public API"
  ]
}
```

### max_rounds

Hard stop after this many completed or blocked rounds. Default is `10`.

### max_minutes

Hard stop based on wall-clock time elapsed since the most recent round activity (`init`, `begin-round`, `complete-round`, `block-round`, `cancel-round`, or `goal-met`). Pausing the run does not pause the clock: a pause longer than the remaining budget stops the resumed run on the first `check`. `null` means unlimited.

### deadline

Timer (定时器) stop: a hard stop at an **absolute** point in time, independent of how many rounds have run or how much time has passed since the last activity. This complements the `max_minutes` countdown (倒计时): countdown measures duration since the last round; the deadline stops the run at a fixed wall-clock moment, so "iterate until tomorrow morning" is expressed as a deadline instead of a guessable minute count.

The value stored in `config.json` is a normalized **ISO-8601 timestamp** (for example `"2026-08-10T08:00:00+08:00"`). Set it on `init` with `--deadline <expr>`, which accepts:

| Expression | Meaning |
|---|---|
| `2026-08-10T08:00:00` | Absolute time (naive timestamps are treated as UTC) |
| `2026-08-10T08:00:00+08:00` | Absolute time with a timezone offset |
| `+8h` / `+30min` / `+1d` / `+2w` | Relative duration from **now** (also `+8hours`, `+1day`, `+2weeks`) |
| `08:00` | Local wall-clock time **today** at 08:00; if that time is already past, **tomorrow** at 08:00 |

```powershell
python <this-skill>/scripts/autopilot_state.py init --repo <repo> --deadline "+12h"
python <this-skill>/scripts/autopilot_state.py init --repo <repo> --deadline "08:00"
python <this-skill>/scripts/autopilot_state.py init --repo <repo> --deadline "2026-08-10T08:00:00+08:00"
```

The expression is resolved to an absolute UTC timestamp once at `init` time, so re-reading the config never shifts the timer. Because the stored value must be absolute, hand-editing `config.json` requires a full ISO timestamp (the relative `+8h`/`HH:MM` forms are only expanded by `init --deadline`). A deadline in the past stops the run on the first `check`; remove it (set `"deadline": null`) to disable. `null` means no deadline.

### max_tokens

Soft stop based on `estimated_tokens_used` accumulated by `complete-round`, `block-round`, and `cancel-round`. `null` means unlimited.

The helper auto-estimates tokens per round when `--tokens` is omitted, using the heuristic `500 + 12 * changed lines + 100 * binary files`, where changed lines come from the diff between the round's start SHA (recorded by `begin-round`) and `HEAD`, plus any still-uncommitted working-tree/staged changes. Binary files are charged a flat cost because line counts are meaningless for them. Anchoring on the round start SHA means a round never re-counts lines committed in earlier rounds. For a tighter estimate, pass `--tokens` with your own value from the session.

### max_round_scope

Integer, default `null`. Maximum number of changed lines (added + removed) a single round's staged diff may contain; each binary file in the staged diff counts as one unit. The `commit` helper refuses to commit when exceeded, forcing the agent to split the round. `null` means no limit.

### allow_uncommitted_changes

Boolean. Default `false`. When false, the skill refuses to start if the working tree is not clean, so it never commits user-owned changes by accident. This is **enforced in code**: `init` refuses to run and `begin-round` refuses to open the first round on a dirty tree (changes under `.autopilot/` are ignored). Set true only when the user explicitly authorizes continuing from a dirty tree.

### push

Boolean. Default `false`. The skill commits locally and does not push. When `true`, `complete-round` automatically runs `git push` for the current branch after recording a successful round, using an explicit non-force `branch:branch` refspec (it never pushes unrelated branches, matching-mode refs, or hidden refs), and the `push` command is available for explicit pushes. The `push` command itself refuses to run while `push: false`. Pushes are always non-force, and a push failure never rewrites the round as failed (a warning is emitted instead).

### branch_mode

String. Default `"current"`. When set to `"feature"`, the skill creates or checks out an `autopilot/<run-id>` branch so autonomous commits do not pollute the user's current branch. On `finish` it returns to the branch active at init unless `--stay` is passed.

### commit_message_prefix

String. Default `"autopilot"`. The `commit` helper builds messages as `<prefix>(round-<N>): <summary>` and enforces git identity and `max_round_scope` at commit time.

### retries_per_round

Integer. Default `3`. Maximum fix-and-retry attempts within one round before the round is marked blocked. This is an **agent-side convention**: the helper does not count retries, so the agent enforces the budget itself and switches to `block-round` when exhausted.

### candidates_per_round

Integer, default `1`. How many backlog candidates one round works on. The default `1` preserves the original one-change-per-round contract. Set it to `N` (for example `3`) to batch N independent changes per round and amortize the per-round overhead (check/analyze/begin/complete); each candidate is still implemented, verified, and committed as its own commit inside the round, and `begin-round --candidate-id` is repeated once per candidate. This is agent-side guidance: the helper accepts any number of `--candidate-id` values and updates every attached candidate's status when the round closes.

```json
{
  "candidates_per_round": 3
}
```

### max_blocked_in_a_row

Integer. Default `2`. Hard stop after this many consecutive blocked rounds, checked by the state helper rather than only by agent judgment.

### check_commands

Array of strings, default `[]`. Exact verification commands to run each round (for example `["pytest", "python -m py_compile ."]`). When set, the agent prefers these over re-discovering test/build/lint commands, making rounds deterministic and reproducible. The agent runs them; the helper does not execute them itself. If a command in this list does not exist, treat that as a stop-and-ask condition rather than fixing a nonexistent command.

### track_state

Boolean. Default `false`. When `false`, `.autopilot/` is added to `.git/info/exclude` and never committed. Set `true` to version the autopilot state, backlog, and config (useful for sharing a run in a PR). The skill will still not stage `.autopilot/` files on its own.

### allow_paths / deny_paths

Arrays of fnmatch globs, default `[]`. When either is non-empty, `commit` refuses to stage any file that violates them. A path is denied when `deny_paths` matches it, and (when `allow_paths` is non-empty) only paths matching `allow_paths` may be committed. Patterns match against the full path and the basename, so `*.secret`, `config/`, and `**/keys/*` all work. This is the trust gate for fully autonomous runs: seed it with `init --allow-path <glob>` / `--deny-path <glob>` (repeatable).

### report_lang

String, default `"zh"`. Language for generated reports and the automatic 10-round phase report: `"zh"` (Chinese) or `"en"`. Override per call with `report --lang`.

## Example

```json
{
  "goals": [],
  "max_rounds": 5,
  "max_minutes": 30,
  "deadline": "2026-08-10T08:00:00+08:00",
  "max_tokens": 80000,
  "max_round_scope": 400,
  "allow_uncommitted_changes": false,
  "push": false,
  "branch_mode": "feature",
  "commit_message_prefix": "autopilot",
  "retries_per_round": 3,
  "candidates_per_round": 1,
  "max_blocked_in_a_row": 2,
  "check_commands": ["pytest", "npm run lint"],
  "track_state": false,
  "allow_paths": ["src/", "tests/"],
  "deny_paths": ["*.secret", "**/keys/**"],
  "report_lang": "zh"
}
```

## Backlog File

`.autopilot/backlog.json` stores improvement candidates and is managed by the state helper.

Useful commands:

```powershell
python <this-skill>/scripts/autopilot_state.py backlog-add --repo <repo> --title "<title>" --reason "<reason>" --value 4 --effort 2
python <this-skill>/scripts/autopilot_state.py backlog-list --repo <repo>
python <this-skill>/scripts/autopilot_state.py backlog-rank --repo <repo>
```

Each candidate tracks:

- `id`, `title`, `reason`
- `value` (1-5) and `effort` (1-5) used for value-to-effort ranking
- `status`: `pending`, `picked`, `completed`, or `blocked`
- `round` and timestamps

`backlog-rank` sorts pending candidates by `value / effort` descending. `begin-round` marks the chosen candidate(s) `picked` (`--candidate-id` is repeatable for multi-candidate rounds); the helper updates each one to `completed` or `blocked` when the round closes, and back to `pending` on `cancel-round`. Legacy string flags `--impact` and `--effort-level` are still accepted and mapped to numeric scores.

Candidates can be rescored or re-queued with `backlog-update` (update `--title`, `--reason`, `--value`, `--effort`, or `--status`) and removed with `backlog-remove`:

```powershell
python <this-skill>/scripts/autopilot_state.py backlog-update --repo <repo> --id candidate-001 --value 5 --effort 1
python <this-skill>/scripts/autopilot_state.py backlog-remove --repo <repo> --id candidate-001
```

## State File

`.autopilot/state.json` tracks:

- `schema`, `run_id`, `repo`, `branch`, `origin_branch`
- `started_at`, `last_activity_at` (rolling window for `max_minutes`)
- `deadline` (config): absolute ISO-8601 timer stop; see the `deadline` field above
- `round`, `completed_rounds`, `blocked_rounds`, `cancelled_rounds`, `reverted_rounds` (cancelled and reverted rounds advance the round-number counter so numbers are never reused)
- `current_round`, `history`, `completed_goals`
- `estimated_tokens_used`
- `stop_reason` and `finished_at`

`repo` is refreshed to the path used on each invocation, so a moved or re-cloned repository resumes correctly. Older state files are migrated automatically on read. Do not hand-edit `state.json` while a run is active; use the helper commands.

## State Helper Commands

- `init` — create config + state (+ optional feature branch). Flags cover every config field: `--goal`, `--goals-from-prompt`, `--max-rounds`, `--max-minutes`, `--deadline`, `--max-tokens`, `--max-round-scope`, `--branch-mode`, `--allow-uncommitted-changes`, `--track-state`, `--check-commands`, `--push`, `--commit-message-prefix`, `--retries-per-round`, `--candidates-per-round`, `--max-blocked-in-a-row`, `--allow-path`, `--deny-path`, `--report-lang`, `--force`.
- `read`, `check`, `diagnose` — inspect state, stop conditions, and repository/git health. `check --brief` returns only `continue`/`stop_reason`/`warnings` (saves tokens in the loop).
- `detect-agent` — detect the runtime agent (opencode / claude-code / codex / generic) and print adaptation context. Honors a `SKILL_DIR` environment variable for the reported skill directory.
- `begin-round`, `complete-round`, `block-round`, `cancel-round` — round lifecycle. `begin-round` enforces the clean-tree rule and refuses to reuse round numbers; `--candidate-id` is repeatable so one round can pick multiple backlog candidates (`candidates_per_round`). `complete-round` auto-writes a Chinese phase report (`.autopilot/phase-report-round-<N>.md`) every 10 completed rounds.
- `commit` — staged-change check, git identity check, path whitelist check (`allow_paths`/`deny_paths`), scope guard (including binary files), open-round requirement, and prefix message building.
- `undo-round` — `git revert` a bad commit (never rewriting history), record a `revert` history entry, and advance the round counter.
- `goal-met`, `finish` — goals and run closure. `finish` auto-cancels any still-open round and returns to the origin branch in feature mode.
- `report` — print (or write with `--output`) a deterministic markdown run report; `--lang zh|en` overrides `report_lang`.
- `detect-verify` — scan repo entry points and recommend `check_commands`; `--apply` writes them into the config.
- `backlog-add`, `backlog-update`, `backlog-remove`, `backlog-list`, `backlog-rank`, `backlog-pick` — backlog management
- `ensure-branch` — create or check out the autopilot feature branch
- `push` — push the current branch to its remote using an explicit non-force refspec; refuses to run when `push: false`

Every state-changing command accepts `--dry-run` to rehearse the action without touching `.autopilot/` or git.

## Runtime Agent Detection

`detect-agent` identifies which agent runtime is driving the session so the skill can adapt command syntax, the Python launcher, and path references:

```powershell
python <this-skill>/scripts/autopilot_state.py detect-agent --repo <repo> [--home <dir>]
```

Detection precedence:

1. `AUTOPILOT_AGENT` environment variable (`opencode`, `claude-code`, `codex`, or `generic`).
2. Runtime environment variables: `OPENCODE` → opencode, `CLAUDE_CODE` → claude-code, `CODEX` → codex.
3. Project marker file in the working directory: `opencode.json`, `CLAUDE.md`, or `AGENTS.md`.
4. Home-directory marker file.
5. `generic` fallback.

Output includes `agent`, `detected_by`, `shell`/`command_style` (`powershell` or `bash`), `python_cmd` (`python`/`python3`/`py`), `skill_dir`, and `agent_config`. The `--home` flag overrides the home directory used for marker detection, which is also useful for tests.

## Stop-Condition Enforcement

`begin-round` refuses to open a new round when any stop condition is already reached (all goals met, `max_rounds`, `max_minutes`, `deadline`, `max_tokens`, `max_blocked_in_a_row`, or a finished run), so the loop cannot overrun its own limits.

`commit` requires an open round. It refuses to commit when no round is open unless `--round <n>` is passed explicitly, which prevents orphan commits after `cancel-round` or `block-round` from being committed with a misleading round number.

## JSON Output

State-changing commands accept `--json` and emit a single machine-readable result object `{ "ok": true, "message": "...", ... }` on stdout instead of human text. This now includes `init`, `commit`, `ensure-branch`, and `finish` (informational lines are routed to stderr in JSON mode). Error cases emit `"ok": false` and exit with a non-zero code, so automation can branch on the code and the payload.

## Audit Log

Every state-changing action is appended as a newline-delimited JSON line to `.autopilot/log.jsonl` (inside the excluded `.autopilot/` directory). Each entry records a timestamp, an `event`, a `status`, and event-specific fields, giving a replayable trail of init, rounds, commits, pushes, and backlog changes.

## Testing the Skill

Run `python <this-skill>/scripts/test_autopilot_state.py` to execute the unit + integration suite (it creates throwaway git repos in a temp directory). Also run `python -m py_compile <this-skill>/scripts/autopilot_state.py` as a syntax smoke check.
