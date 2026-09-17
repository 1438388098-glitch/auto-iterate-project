# Changelog

All notable changes to auto-iterate-project are documented here.

## 1.3.0 (2026-09-17)

Post-Goal Direction Prediction (Wave 0): completed goals become predicted
follow-up directions ("because we shipped A, B is next"), consumed before any
lens-scan expansion. Designed in `docs/post-goal-prediction-proposal.md`
(刀 A; the 刀 B anti-noise ranking factors ship separately in 1.3.1).

### Added

- `goal_events` / `goal_seeds` in `state.json` (schema v5 → v6, migrated
  idempotently on read; `completed_goals` stays a plain string array; lists are
  bounded at 20 events / 50 seeds with 500-char text caps).
- `goal-met --next-step <title>` (repeatable) — each occurrence creates one
  direction seed; `--unlocked-capability`, `--seed-type/--seed-value/--seed-effort`
  override the defaults (seed type defaults to the first non-saturated type);
  `--no-auto-context` skips the automatic snapshot. Every call also records a
  structured goal event (recent commit topics, saturated types, round candidates).
- `backlog-add --from-seed <id>` — promotes an open seed into a candidate
  (title/type/value/effort/risk default to the seed; explicit flags override;
  the candidate carries `from_seed` + `hypothesis`; the seed becomes `promoted`).
- Seed write-back on round close: `complete-round` → `verified`,
  `block-round` → `refuted` (block reason stored as notes — failed hypotheses
  never re-enter the pool to game the stats), `cancel-round` → `open` again.
- `check` in the expand phase reports an `expansion` context: open seeds,
  completed goals, recent/saturated/underused types, suggested themes. The key
  set is stable — `seeds` is `[]` rather than a missing key when no seeds exist;
  iterate-phase `check --brief` output is unchanged (no `expansion` key).
- Thin/empty-backlog warnings in the expand phase point at Wave 0 (seed
  verification) before lens-scan Deep Expansion.
- SKILL.md: Post-Goal Direction Prediction chapter (causal hypothesis classes,
  hypothesis loop, Wave 0 / Wave 1+ ordering with the all-seeds-dead exception),
  new Anti-Idle violations for prediction without verification.

## 1.2.1 (2026-09-11)

Audit hardening from multi-lens Deep Expansion: fail-closed guards, ready-aware
check signals, protocol fixes that remove remaining early-stop / idle paths.

### Fixed

- Secret scan and `max_round_scope` no longer **fail open** when `git diff` /
  `--numstat` fails — both refuse instead of treating the tree as clean/empty.
- `report` / `retrospective` honor `--json` (wrap markdown in a JSON object)
  instead of printing raw Markdown.
- `max_rounds` now counts cancelled + reverted rounds, matching the round-number
  counter (previously cancel churn could never hit the budget stop).
- Lock from **another hostname** is no longer deleted as stale (shared-disk
  safety); the run refuses until the lock is removed manually.
- `git checkout` of state-controlled branch names is validated
  (`_assert_safe_ref_name`) so a corrupted `state.json` cannot inject git options.
- `finish` retrospective write failures warn instead of being silently swallowed.
- `check` `needs_expansion` / `action_hint` treat **ready==0** (dependency-blocked
  pending pool) as expand, not work — no more check↔begin-round deadlock.
- `begin-round` refuses an empty round when the backlog has ready pending
  candidates (exploratory empty rounds still allowed when nothing is ready).
- `backlog-add` refreshes `last_activity_at` so expansion scouting does not burn
  `max_minutes` without any recorded activity.

### Docs / protocol

- Stop Conditions: goals stop only when `expand_after_goals` is false.
- Deep Expansion: expand **in parallel with** ready work (no expand-only deadlock);
  serial in-process lens fallback when the host has no subagent tool.
- Undo guidance unified on `undo-round` (no competing `git revert` advice).
- Config example, init flags, check --brief fields, ranking/secret-pattern docs
  brought in line with 1.1.0/1.2.0 behavior.

## 1.2.0 (2026-09-11)

Anti-early-stop and anti-idle protocol. Empty or thin backlog is no longer an
escalation/stop; it is a mandatory Deep Expansion trigger. The agent must keep
working (or spawn explore subagents) until a real budget stop, never wait out
the deadline.

### Added

- `min_pending_candidates` config (default 3) and `init --min-pending-candidates N`.
- `check` now reports `backlog` (`pending`, `ready`, `min_pending_candidates`,
  `needs_expansion`) and `action_hint` (`work` | `expand` | `stop`).
- When pending backlog is below the threshold (or empty), `check` warns to run
  Deep Expansion — spawn explore subagents, `backlog-add` value-gated
  candidates — instead of idling.
- SKILL.md: **Deep Expansion Protocol** (3-6 parallel explore subagents across a
  rotating 16-lens set: architecture, tests, security, perf, docs/DX, debt,
  API, concurrency, i18n/encoding, config, observability, packaging, data
  integrity, UX copy, deps, cross-cutting resilience) and **Anti-Idle
  Discipline** (every continue=true check must begin-round or expand; waiting
  out the deadline is a protocol violation).
- Failed expansion waves never escalate the conversation — they escalate
  effort (new lenses → lower value floor → deeper end-to-end reads → wider
  scope). Escalation is reserved for true external blockers and budget stops.

### Changed

- Quality gate: "no clear value" no longer means stop-and-ask on the first
  miss; it means Deep Expansion first. Empty/thin backlog never escalates —
  keep expanding while budgets remain.
- Expansion Phase is continuous (thin backlog / empty backlog / goals met),
  not only after `expand_after_goals`.
- Safety Rules: never idle until the deadline while `continue` is true.

## 1.1.0 (2026-09-11)

Ranking overhaul: the backlog now ranks by **expected value per round** instead of
a value/effort ratio, so cheap busywork no longer systematically outranks valuable
work.

### Added

- `ranking_mode` config (`expected` default / `classic`): expected-value scoring is
  `value × success rate (from the type's blocked history) × value calibration
  (learned from past self-review scores) × dependency-unlock bonus × budget-aware
  risk factor × saturation × prospective backlog-mix penalty ÷ log2(1+effort)`.
  Rounds — not effort — are the scarce resource, so effort only breaks ties.
- `min_candidate_value` (default 3): below-floor candidates are demoted
  (`below_floor: true`) and at most one quick-win fills a remaining round slot;
  `begin-round` warns when several below-floor candidates are picked together.
- `max_same_type_per_round` (default 2): diversity quota applied when marking the
  recommended round batch.
- Batch cutoff: recommended-round selection stops once the score drops below 40%
  of the best eligible candidate.
- `backlog-rank` entries now carry `selected`, `below_floor`, and `unlocks`, and
  `score_breakdown` exposes every factor (success_rate, calibration, unlock_bonus,
  risk_weight, mix_penalty, effort_cost).
- Value calibration: `complete-round --review-score` is written back to completed
  candidates; per-type calibration (clamped 0.6-1.5, needs ≥3 samples) discounts
  types the agent habitually over- or under-rates.
- Risk weight grows with the consumed round budget (0.05 → 0.15): early rounds
  take swings at risky work, late rounds play it safe.

### Changed

- Default ranking order for mixed backlogs: high-value/larger-effort candidates
  now outrank trivial low-value ones (previously the opposite). Set
  `ranking_mode: classic` to restore the old order.

## 1.0.0 (2026-09-11)

Audit-driven hardening release. No workflow contract changes; all existing
commands keep their CLI surface and JSON shapes (contract tests added).

### Security

- Secret-scan findings are masked before being written to `.autopilot/log.jsonl`
  or printed to stdout — the scan no longer leaks the very secrets it catches.
- `allow_paths`/`deny_paths`: staged paths are now read with
  `core.quotepath=false`, and matching is case-agnostic with directory-prefix
  support (`src/`, `src`), so deny rules can no longer be bypassed via
  quotePath escaping or case games and the documented directory form works.
- Built-in secret patterns extended: `github_pat_` tokens and JWTs are caught;
  `sk-` now matches `sk-proj-`/`sk-ant-` style keys.
- `--allow-secrets` bypasses are now recorded in the audit log
  (`secrets_bypassed: true`).
- `report --output`/`retrospective --output` refuse paths outside the target
  repository unless `--force` is passed.
- `check` warns when `.autopilot/config.json` changed since `init`
  (config fingerprint in state.json) — check_commands are executed by the loop,
  so silent config changes are surfaced.
- Invalid user regex in `secret_patterns` fails with a clean error, not a
  traceback.

### Fixes

- `git status` failures inside `_porcelain_entries` no longer silently report a
  clean working tree; they exit with a clear error.
- Non-ASCII (e.g. Chinese) file names are unquoted correctly (bytes → UTF-8,
  not `unicode_escape` mojibake) and no longer corrupt dirty-tree checks or
  path guards.
- `state.json` field corruption (e.g. string counters) now fails with a clean
  error instead of a traceback.
- Dead lock-file probe on Windows: unparseable `tasklist` output now keeps the
  lock (conservative) instead of deleting it.

### Performance / robustness

- `.autopilot/log.jsonl` rotates at 5 MB (`log.jsonl.1`) and warns to stderr
  when a log write fails instead of silently dropping audit entries.
- `state.json` history is trimmed to the last 100 rounds and long text fields
  are capped, so long-running loops do not grow state without bound.
- The lock file is created atomically (`O_CREAT|O_EXCL`), removing the
  check-then-write race; Windows stale-lock probing timeout dropped to 3 s.
- `check` runs fewer git subprocesses (identity check moved to `commit`/
  `diagnose`, where it is enforced); backlog updates during round close are
  batched into a single read/write.
- Secret patterns are precompiled; the staged diff is fetched with `-U0`.

### Refactoring

- `commands.py` split by concern: `secrets.py` (secret scanning), `guard.py`
  (path allow/deny matching), `verify.py` (verification command discovery).
  Token estimation now has a single implementation in `io.estimate_tokens_for_round`.
- Round-lifecycle bookkeeping (complete/block/cancel/finish) consolidated into
  one `_close_round` helper.
- `state.ensure_branch_impl` renamed to `state.ensure_branch`; `config`
  parameters renamed to `cfg` to stop shadowing the config module.
- CLI dispatch uses `set_defaults(func=...)` (single registration point per
  command); `state.default_state()` is the single authority for state defaults.

### Engineering

- Added GitHub Actions CI (ubuntu/windows × Python 3.8/3.13: syntax check +
  full test suite), a version in `SKILL.md`, and this changelog.
- `--dry-run` is now honored by all state-changing commands, including
  `backlog-add/update/remove/pick`, `directive-add`, `ensure-branch`, and
  `detect-verify --apply`, as SKILL.md always promised.
- Entry script works under `python -P`/`PYTHONSAFEPATH` (explicit sys.path
  bootstrap).
- Expanded test suite: backlog-pick coverage, parametrized secret-pattern
  tests, revert-conflict, corrupt lock/state/analysis failure paths, detached
  HEAD, push failure, JSON contract tests, git environment isolation.
