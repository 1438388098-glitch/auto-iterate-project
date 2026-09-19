# Configuration Reference

> Single source of truth for field defaults: `scripts/autopilot/config.py::default_config`.
> The tables below mirror it; when in doubt, the code wins. Update both together.

## Config File

`.autopilot/config.json` lives at the target repository root. The state helper creates it during `init`; edit it before a run to set limits and goals. Every field has a default, so a minimal file works.

The helper validates field types on every read: a non-object config, a string `max_rounds`, a bad `branch_mode`, or a non-list `check_commands` produces a clear error instead of a traceback. Invalid regex in `secret_patterns` is also rejected with a clean error. Delete the file and run `init` again to reset.

> Security note: `check_commands` are executed by the loop as configured here, and `init`
> records a fingerprint of this file in `state.json`. `check` warns when the config
> changes after `init` — treat such a warning as a prompt to verify the change was
> intentional before continuing an autonomous run.

## Fields

### goals

Array of explicit goals. Empty means open-ended improvement. Use the exact goal text when calling `goal-met`. `init --goals-from-prompt "<request>"` splits a natural-language request (Chinese or English) into `goals` automatically. `--goal` and `--goals-from-prompt` are last-write-wins: passing both keeps only the `--goals-from-prompt` result, so pass one of them.

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

The helper auto-estimates tokens per round when `--tokens` is omitted, using the heuristic `500 + 12 * changed lines + 100 * binary files`. Accounting is run-level monotonic: the run's total changed units are the committed diff from `run_start_sha` (anchored at `init`) to `HEAD` plus the current working-tree/index delta against `HEAD` — two disjoint measurements whose sum is the run's true total, so each line is billed exactly once no matter how many rounds it sat uncommitted before being committed. Each round is charged only the delta above the persisted high-water marks (`billed_text`/`billed_binary` in state); the 500-token base applies to every round closed with real work (a zero-work cancel is `aborted` and burns nothing). For a tighter estimate, pass `--tokens` with your own value from the session — the override still advances the water marks so the real lines are not billed again later.

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

Integer. Default `3`. Maximum fix-and-retry attempts within one round before the round is marked blocked. `0` disables retrying entirely (block on the first failure); a negative value is rejected by validation. This is an **agent-side convention**: the helper does not count retries, so the agent enforces the budget itself and switches to `block-round` when exhausted.

### candidates_per_round

Integer, default `4`. How many backlog candidates one round works on. Each candidate is still implemented and reviewed as its own unit inside the round, so one round batches several independent changes and amortizes the per-round overhead. Set it to `1` for the original one-change-per-round contract. `begin-round --candidate-id` is repeated once per candidate; the helper accepts any number and updates every attached candidate's status when the round closes.

```json
{
  "candidates_per_round": 4
}
```

### commit_every_rounds

Integer, default `5`. How often accumulated changes are committed. Rounds before the boundary (`round % commit_every_rounds == 0`) implement, verify, and record work via `complete-round` **without** a commit SHA; their changes stay in the working tree/index. The boundary round stages everything and flushes it as one batch commit. If the run stops mid-batch, flush pending changes as a final commit before `finish`. Set to `1` to commit every round (the original contract). `check` reports the next commit round in `next_commit_round`. Batched commits skip the "round started with a dirty tree" refusal because accumulated changes are expected; `max_round_scope` still applies per commit, so a batch larger than the scope limit must be committed in subsets.

### verify_every_rounds

Integer, default `3`. How often the full verification set (`check_commands`, tests, build, lint) runs. Between verification rounds the agent runs only a cheap smoke check when one exists (`python -m py_compile`, `node --check`, `cargo check`) or states a lightweight verification method. A commit round is always a verification round, and a failing verification is never ignored to make a commit. Set to `1` to run full verification every round. `check` reports the next verification round in `next_verify_round`.

### checkpoint_every

Integer, default `null`. When set to `N`, the loop pauses after every Nth round and consults the user before continuing (progress summary, top ranked candidates, standing directives, budgets). This gives a human a steering wheel on long autonomous runs. `check` reports the next pause in `next_checkpoint_round`. `null` disables checkpoints.

### expand_after_goals

Boolean, default `false`. When `true`, reaching all configured goals does **not** stop the loop: `check` reports `"phase": "expand"` and `begin-round` keeps working on new, value-gated candidates instead of stopping. Modeled on the "goal-met -> expand" pattern: after the core goal is real, the agent scouts fresh candidates with a detached perspective and lets `backlog-rank` (expected value per round, risk, type saturation) act as a value gate — no random feature bloat. Expansion remains bounded by the normal budgets (`max_rounds`, `max_minutes`, `deadline`, `max_tokens`, `max_blocked_in_a_row`); because goals no longer stop the loop in this mode, `check` warns when no other stop condition is configured.

Independent of this flag, a thin backlog (`pending` < `min_pending_candidates`) is always an expansion trigger while the run continues — see Deep Expansion Protocol in `SKILL.md`.

### review_threshold

Integer 1-5, default `null`. When set, `complete-round` requires a `--review-score` (self-assessment on the same 1-5 scale) at least as high, and records the score plus optional `--review-notes` in the round history. A score below the threshold is refused with an error — the round must be reworked or blocked. This turns the pre-commit self-review into a hard quality red line. `null` disables the gate.

### scan_secrets

Boolean, default `true`. When true, `commit` scans the **added lines** of the staged diff for secret-like content before committing and refuses on a match. Built-in patterns cover AWS access keys (`AKIA...`), private-key blocks, GitHub personal access tokens (`ghp_...` and `github_pat_...`), Slack tokens (`xox...`), Google API keys (`AIza...`), OpenAI-style `sk-...` keys (including `sk-proj-`/`sk-ant-`), JWTs (`eyJ...`), and `api_key = ...` assignments. If `git diff` fails during the scan, the helper **fails closed** (treats the scan as dirty) instead of allowing the commit. Bypass a false positive with `commit --allow-secrets`, or disable entirely by setting this to `false`. Run `secret-scan` at any time to check the staged diff without committing. Use `secret_patterns` to add repository-specific regexes.

### secret_patterns

Array of regex strings, default `[]`. Extra patterns appended to the built-in secret scan. Each pattern is matched against every added line of the staged diff; the commit helper reports the file and matched text when refusing. Patterns are treated as regular expressions, so escape literal dots.

### type_saturation_threshold

Integer, default `2`. After this many candidates of the same `type` have been completed, `backlog-rank` downweights further same-type pending candidates by `0.7` per additional completion (`0.7 ** (completed - threshold)` in the default `ranking_mode: expected`; `0.85` in `classic`). This stops the loop from grinding out the same low-hanging-fruit category forever. Set to a large number to disable.

### ranking_mode

String, default `"expected"`. How `backlog-rank` scores candidates:

- `expected` (default): expected **value per round** — the scarce resource in a run is rounds, not effort. Score = `value × success_rate (1 − blocked_rate of the type) × calibration (learned from past self-review scores, clamped 0.6-1.5, needs ≥3 samples) × unlock_bonus (1 + 0.15 × pending candidates that depend on this one) × risk_factor × saturation × mix_penalty (1 − 0.3 × pending share of the type) ÷ log2(1 + effort)`. The risk factor tightens as the round budget is consumed (`risk_weight` 0.05 → 0.15), so early rounds take swings and late rounds play it safe.
- `classic`: the legacy `value / effort` ratio with fixed risk/saturation/blocked discounts.

Every `backlog-rank` entry carries a `score_breakdown` exposing each factor, plus `selected` (the recommended round batch), `below_floor`, `unlocks`, and `ready`/`blocked_by`.

### min_candidate_value

Integer 1-5, default `3`. Pending candidates whose (resolved) value is below this are demoted: `below_floor: true`, ranked after same-score above-floor candidates, and such quick-wins only fill slots the main pool left open in the recommended batch (subject to the same origin quotas and the 40% cutoff). `begin-round` prints a warning when several below-floor candidates are picked in one round, and refuses an empty `--candidate-id` round while only below-floor candidates are ready. Set to `null` to disable the floor.

### max_same_type_per_round

Positive integer, default `2`. Diversity quota used when marking the recommended round batch (`selected: true`): at most this many candidates of the same type are included per round. The batch cutoff uses 40% of the FIRST SELECTED entry's score as its base and only prunes below-floor entries — above-floor candidates stay eligible until the batch is full. Ready above-floor entries skipped by a constraint carry `cut_reason` (`quota`/`late_run`/`type`/`cutoff`, or `batch_full`), which `check` surfaces as `selected_empty_reason` when the batch comes back empty. Set to `null` to disable the quota.

### min_pending_candidates

Non-negative integer, default `3`. When the backlog has fewer than this many `pending` candidates, `check` sets `action_hint: "expand"` and warns the agent to run Deep Expansion (spawn explore subagents, `backlog-add`) instead of idling or treating the thin backlog as a stop. Pending count of `0` always triggers the expansion warning while the run continues. Set to `0` to disable the thin-backlog trigger (empty backlog still warns). Init flag: `--min-pending-candidates N`.

### max_predicted_per_round

Non-negative integer or `null`, default `1`. Anti-noise quota for direction-seed predictions (刀 B): at most this many `predicted`-origin candidates may enter one recommended round batch (`selected`). The `expansion` origin has its own separate quota (`max_expansion_per_round`) — expansion work already passed the main agent's value gate, while a prediction is still an unproven hypothesis. Predicted candidates also carry a `confidence` (0.5-1.0, default 0.75; expansion with evidence defaults to 0.9) that discounts their score, observed work wins score ties, and in the late run (progress > 0.7) predicted work is cut entirely while observed candidates remain ready (expansion is not cut). Predicted candidates keep their own blocked/review sub-account, so consecutive failures sink future predictions without contaminating the observed work's per-type statistics; with fewer than 3 resolved predictions the score uses a conservative prior (type success rate × 0.75). `ranking_mode: classic` ignores every prediction factor. Set to `null` to disable the quota. Init flag: `--max-predicted-per-round N`. Tune this (or the confidence defaults) only from the predicted sub-account's hit-rate samples in `report` — not from intuition.

### max_expansion_per_round

Non-negative integer or `null`, default `null` (uncapped). Quota for `expansion`-origin candidates per recommended round batch, kept separate from `max_predicted_per_round`: expansion candidates were judged valuable by the main agent before entering the backlog, so they are not discounted by default and are not cut in the late run. Set an integer to cap how many expansion candidates join one batch. Init flag: `--max-expansion-per-round N`.

### max_blocked_in_a_row

Integer. Default `2`. Hard stop after this many consecutive blocked rounds, checked by the state helper rather than only by agent judgment. `0` means the run stops on the FIRST blocked round (a legal but harsh choice — the configurator owns that); a negative value is rejected by validation.

### check_commands

Array of strings, default `[]`. Exact verification commands to run on each verification round (for example `["pytest", "python -m py_compile ."]`). When set, the agent prefers these over re-discovering test/build/lint commands, making rounds deterministic and reproducible. Full verification runs every `verify_every_rounds` rounds and always on a commit round. The agent runs them; the helper does not execute them itself. If a command in this list does not exist, treat that as a stop-and-ask condition rather than fixing a nonexistent command.

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
  "candidates_per_round": 4,
  "commit_every_rounds": 5,
  "verify_every_rounds": 3,
  "checkpoint_every": 6,
  "expand_after_goals": true,
  "review_threshold": 3,
  "max_blocked_in_a_row": 2,
  "check_commands": ["pytest", "npm run lint"],
  "track_state": false,
  "allow_paths": ["src/", "tests/"],
  "deny_paths": ["*.secret", "**/keys/**"],
  "scan_secrets": true,
  "secret_patterns": [],
  "type_saturation_threshold": 2,
  "report_lang": "zh",
  "ranking_mode": "expected",
  "min_candidate_value": 3,
  "max_same_type_per_round": 2,
  "min_pending_candidates": 3
}
```

## Backlog File

`.autopilot/backlog.json` stores improvement candidates and is managed by the state helper.

Useful commands:

```powershell
python <this-skill>/scripts/autopilot_state.py backlog-add --repo <repo> --title "<title>" --reason "<reason>" --value 4 --effort 2 --type refactor --risk 2 --depends-on candidate-002
python <this-skill>/scripts/autopilot_state.py backlog-list --repo <repo>
python <this-skill>/scripts/autopilot_state.py backlog-rank --repo <repo>
```

Each candidate tracks:

- `id`, `title`, `reason`
- `type` (`bugfix|feature|refactor|perf|test|docs`, default `feature`)
- `risk` (1-5, default 1) and `depends_on` (ids that must be completed first)
- `value` (1-5) and `effort` (1-5) used for the base value/effort score
- `status`: `pending`, `picked`, `completed`, or `blocked`
- `round` and timestamps
- when promoted from a direction seed: `from_seed` (the seed id) and `hypothesis`

`backlog-rank` sorts pending, dependency-**ready** candidates by score and marks the recommended round batch (`selected`). In the default `ranking_mode: expected` the score is expected value per round (see the `ranking_mode` section above); `ranking_mode: classic` uses the legacy `value / effort` ratio discounted for `risk` (`max(0.5, 1.0 - 0.08 * (risk - 1))`), type saturation (`0.85 ** max(0, completed_of_type - type_saturation_threshold)`), and blocked history (`0.9 ** blocked_of_type`). Candidates whose `depends_on` is not yet satisfied are marked `"ready": false` with a `blocked_by` reason and ranked after ready candidates. Every entry carries a `score_breakdown` so the ranking is transparent. The per-type counts (including the learned value calibration) come from the backlog and are also persisted to `state.type_stats` for the retrospective. `begin-round` refuses to pick a candidate whose deps are unresolved.

`begin-round` marks the chosen candidate(s) `picked` (`--candidate-id` is repeatable for multi-candidate rounds); the helper updates each one to `completed` or `blocked` when the round closes, and back to `pending` on `cancel-round`. Legacy string flags `--impact` and `--effort-level` are still accepted and mapped to numeric scores.

Candidates can be rescored or re-queued with `backlog-update` (update `--title`, `--reason`, `--value`, `--effort`, `--type`, `--risk`, `--depends-on`, or `--status`) and removed with `backlog-remove`:

```powershell
python <this-skill>/scripts/autopilot_state.py backlog-update --repo <repo> --id candidate-001 --value 5 --effort 1 --type bugfix
python <this-skill>/scripts/autopilot_state.py backlog-remove --repo <repo> --id candidate-001
```

## Direction Seeds (Post-Goal Prediction)

`goal-met` can record *direction seeds* — predicted follow-up work ("because we shipped A, B is next") — alongside the completed goal. Seeds live in `state.json` under `goal_seeds` (bounded at 50, text capped at 500 chars); each completed goal also gets a structured snapshot in `goal_events` (bounded at 20): recent commit topics, saturated types, the round's candidates, unlocked capabilities, and the ids of the seeds it spawned. `completed_goals` remains a plain string array, and state files written by schema v5 are migrated on read.

```powershell
python <this-skill>/scripts/autopilot_state.py goal-met --repo <repo> --goal "<goal>" --round <n> --next-step "<follow-up>" --next-step "<follow-up 2>" --unlocked-capability "<capability>"
```

- `--round <n>` — the round that completed this goal; must match a completed history entry. Without it the goal event is recorded `unverified: true` and the "all goals met" stop is withheld (`check` lists the goals under `goals_unverified`)
- `--evidence` — audit-only note on how you know the goal is met (recorded on the goal event, not scored)
- `--next-step` (repeatable) — one direction seed per occurrence; `type` defaults to the first non-saturated type, `value` to 4, `effort` to 2, `risk` to 1
- `--seed-type` / `--seed-value` / `--seed-effort` — override the defaults
- `--no-auto-context` — skip the automatic snapshot (no goal event is recorded unless `--next-step`/`--unlocked-capability` also produced seeds or capabilities, in which case the event records just those links)
- Repeating a `goal-met` with the same goal and the same `--next-step` titles does not duplicate seeds; each seed carries `source_event_id`, and the event records the last completed round's commit SHA(s) in `commit_shas`
- `seed-reject --id <id> --reason "<why>"` — an open seed whose evidence died in the check is marked `rejected` (terminal) and leaves the open-seeds list, so the Wave 0 exception in `SKILL.md` can fire

A seed moves through `open → promoted → verified` or `open → promoted → refuted`:

- `backlog-add --from-seed <id>` promotes it into a candidate (title/type/value/effort/risk default to the seed; explicit flags override). Only `open` seeds can be promoted.
- `complete-round` on the promoted candidate's round marks the seed `verified`; `block-round` marks it `refuted` with the block reason stored as `outcome` notes (failed hypotheses never re-enter the pool to game the statistics); `cancel-round` returns it to `open`.

In the expand phase (`check` reports `"phase": "expand"`), the `--brief` payload carries an `expansion` object with a stable key set — `seeds` (open seeds with `id`/`title`/`type`/`from_capability`/`source_goal`/`status`; `[]` when none), `completed_goals`, `recent_types`, `saturated_types`, `underused_types`, `suggested_themes`, `min_pending_candidates`. In the iterate phase the key is absent and the `check --brief` output shape is unchanged. See the Post-Goal Direction Prediction chapter in `SKILL.md` for the Wave 0 protocol.

Anti-noise scoring (刀 B): a promoted candidate carries `origin: "predicted"`, `confidence` (default 0.75, clamp 0.5-1.0 — explicit `--confidence` overrides), `based_on` (the seed's source goal; a met goal gives a ≤1.08 goal-chain bonus), and `evidence` (audit only, not scored). `backlog-add --origin predicted --confidence 0.8 --based-on "<goal>" --evidence "<note>"` tags any candidate manually; `expansion` origin marks lens-scouted work. The run `report` lists the seeds and the predicted sub-account hit rate (「方向假设」section).

## State File

`.autopilot/state.json` tracks:

- `schema`, `run_id`, `repo`, `branch`, `origin_branch`
- `started_at`, `last_activity_at` (rolling window for `max_minutes`)
- `deadline` (config): absolute ISO-8601 timer stop; see the `deadline` field above
- `round`, `completed_rounds`, `blocked_rounds`, `cancelled_rounds`, `reverted_rounds` (cancelled and reverted rounds advance the round-number counter so numbers are never reused)
- `current_round`, `history`, `completed_goals`, `goal_events`, `goal_seeds` (direction seeds — see the section above; migrated into older state files automatically)
- `estimated_tokens_used`
- `type_stats` (per-type completed/blocked/blocked-rate/avg-effort/avg-value, refreshed on every round close)
- `stop_reason` and `finished_at`

`repo` is refreshed to the path used on each invocation, so a moved or re-cloned repository resumes correctly. Older state files are migrated automatically on read. Do not hand-edit `state.json` while a run is active; use the helper commands.

## State Helper Commands

- `init` — create config + state (+ optional feature branch). Flags cover every config field: `--goal`, `--goals-from-prompt`, `--max-rounds`, `--max-minutes`, `--deadline`, `--max-tokens`, `--max-round-scope`, `--branch-mode`, `--allow-uncommitted-changes`, `--track-state`, `--check-commands`, `--push`, `--commit-message-prefix`, `--retries-per-round`, `--candidates-per-round`, `--commit-every-rounds`, `--verify-every-rounds`, `--checkpoint-every`, `--expand-after-goals`, `--review-threshold`, `--scan-secrets`/`--no-scan-secrets`, `--secret-pattern`, `--type-saturation-threshold`, `--ranking-mode`, `--min-candidate-value`, `--max-same-type-per-round`, `--min-pending-candidates`, `--max-predicted-per-round`, `--max-blocked-in-a-row`, `--allow-path`, `--deny-path`, `--report-lang`, `--force`.
- `read`, `check`, `diagnose` — inspect state, stop conditions, and repository/git health. `check --brief` returns loop-driving fields only: `continue`/`stop_reason`/`warnings`/`goals_met`/`phase`/`backlog` (`pending`/`ready`/`min_pending_candidates`/`needs_expansion`)/`action_hint` (`work`|`expand`|`stop`)/`next_verify_round`/`next_commit_round`/`next_checkpoint_round`/`blocked_streak`/`wave_no`/`lenses_used`/`lenses_unused`/`analysis`/`budget` (`remaining_minutes`/`deadline_remaining_minutes`) (saves tokens in the loop). In the expand phase an `expansion` object with open seeds and type context is appended (see Direction Seeds above).
- `detect-agent` — detect the runtime agent (opencode / claude-code / codex / generic) and print adaptation context. Honors a `SKILL_DIR` environment variable for the reported skill directory.
- `begin-round`, `complete-round`, `block-round`, `cancel-round` — round lifecycle. `begin-round` enforces the clean-tree rule, refuses to pick candidates with unresolved `depends_on`, and refuses to reuse round numbers; `--candidate-id` is repeatable so one round can pick multiple backlog candidates (`candidates_per_round`). `complete-round` accepts an optional `--commit-sha` (omit it on deferred commit rounds when `commit_every_rounds > 1`), an optional `--review-score`/`--review-notes` (pass a score every round — it feeds the value calibration; enforced only when `review_threshold` is set) and `--below-threshold` (records a below-threshold round as completed with its low score, without counting blocked — the only in-run exit once `max_blocked_in_a_row` fires), auto-writes a Chinese phase report (`.autopilot/phase-report-round-<N>.md`) every 10 completed rounds, and refreshes `state.type_stats`. A `cancel-round` with zero work (no tree delta, no commit, under 10 minutes) records an `aborted` round that consumes no round budget and no token base.
- `commit` — staged-change check, git identity check, path whitelist check (`allow_paths`/`deny_paths`), secret scan (`scan_secrets`, bypassable with `--allow-secrets`), scope guard (including binary files), open-round requirement (always enforced unless `--round <n>` is passed explicitly; what batched mode skips is only the dirty-start check), and prefix message building.
- `undo-round` — `git revert` a bad commit (never rewriting history), record a `revert` history entry, and advance the round counter.
- `goal-met`, `finish` — goals and run closure. `goal-met` accepts `--round`/`--evidence` (verification anchor; omitting `--round` marks the goal unverified) and `--next-step`/`--unlocked-capability`/`--seed-*` to record direction seeds (see Direction Seeds above). `finish` auto-cancels any still-open round, writes `.autopilot/retrospective.md`, and returns to the origin branch in feature mode; while no stop condition is reached and value>=floor ready candidates remain it is refused unless `--force` is passed (a forced finish logs `finish-forced`). `config-set --expand-after-goals` rewrites the config and refreshes the state config fingerprint.
- `report` — print (or write with `--output`) a deterministic markdown run report; `--lang zh|en` overrides `report_lang`.
- `retrospective` — print (or write with `--output`) the run-level retrospective: per-type stats, blocked rounds, verification commands, and the next ready candidate.
- `detect-verify` — scan repo entry points and recommend `check_commands` (including `gitleaks`/`detect-secrets` when installed); `--apply` writes them into the config.
- `analysis-save` / `analysis-load` — persist and read the repository-analysis cache in `.autopilot/analysis.json`; the cache auto-invalidates when HEAD or `.autopilot/config.json` changes. `check` surfaces the cache state in its `analysis` payload (`status`: missing/fresh/stale, `reason`, `commits_behind`) and warns when an expansion is due while the cache is >=3 commits stale.
- `expansion-record` — record one Deep Expansion wave's lens set into `state.expansion_waves` (bounded at 20, oldest trimmed). Lenses must come from the fixed 16-lens list (`EXPANSION_LENSES`); an unknown lens exits 2. `check` reports `wave_no`, the order-preserving `lenses_used` union, and the ordered `lenses_unused` remainder so the next wave rotates lenses deliberately; repeating the previous wave's exact lens set warns (an `expansion-wave` warn event) but is not refused.
- `directive-add` / `directive-list` / `directive-remove` — manage standing directives in `.autopilot/directives.json`; the loop must honor them in every round. `directive-list` prints each entry with its 1-based `index`; `directive-remove --index <n>` retires it (audit-logged).
- `secret-scan` — scan the staged diff for secret-like content and report findings (exit non-zero on a match).
- `backlog-add`, `backlog-update`, `backlog-remove`, `backlog-list`, `backlog-rank`, `backlog-pick` — backlog management (candidates carry `type`, `risk`, and `depends_on`; ranking defaults to expected value per round — see `ranking_mode`). `backlog-add --from-seed <id>` promotes a direction seed (see Direction Seeds above). `backlog-add` also refreshes `last_activity_at` so expansion scouting does not burn `max_minutes` without progress.
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

`begin-round` refuses to open a new round when any stop condition is already reached (all goals met without `expand_after_goals`, `max_rounds` counting completed+blocked+cancelled+reverted, `max_minutes`, `deadline`, `max_tokens`, `max_blocked_in_a_row`, or a finished run), so the loop cannot overrun its own limits. When the backlog has ready pending candidates, `begin-round` requires at least one `--candidate-id`.

`commit` requires an open round. It refuses to commit when no round is open unless `--round <n>` is passed explicitly, which prevents orphan commits after `cancel-round` or `block-round` from being committed with a misleading round number.

## JSON Output

State-changing commands accept `--json` and emit a single machine-readable result object `{ "ok": true, "message": "...", ...data }` on stdout instead of human text. This includes `init`, `commit`, `ensure-branch`, `finish`, `analysis-save`, `secret-scan`, and `report`/`retrospective` (JSON wraps the markdown as `markdown`). Informational lines are routed to stderr in JSON mode. Error cases emit `"ok": false` and exit with a non-zero code, so automation can branch on the code and the payload. `check`/`analysis-load`/`backlog-rank`/`backlog-list`/`read`/`diagnose`/`directive-list` always emit JSON.

## Audit Log

Every state-changing action is appended as a newline-delimited JSON line to `.autopilot/log.jsonl` (inside the excluded `.autopilot/` directory). Each entry records a timestamp, an `event`, a `status`, and event-specific fields, giving a replayable trail of init, rounds, commits, pushes, and backlog changes.

## Testing the Skill

Run `python <this-skill>/scripts/test_autopilot_state.py` to execute the unit + integration suite (it creates throwaway git repos in a temp directory). Also run `python -m py_compile <this-skill>/scripts/autopilot_state.py` as a syntax smoke check.
