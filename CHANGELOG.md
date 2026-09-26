# Changelog

All notable changes to auto-iterate-project are documented here.

## 1.8.0 (2026-09-26)

The read-only observation dashboard: a self-hosted web panel that opens when
a run starts and shows how the project grew — a capability evolution tree
(domain → module → file) beside a per-round evolution card stream, with
replay. Pure stdlib; the loop never depends on it.

### Added

- `autopilot dashboard` subcommand: 127.0.0.1-only HTTP server (two GET
  endpoints: `/` page + `/api/snapshot`), mtime-keyed snapshot cache, idle
  self-exit after 30 minutes, `--stop` to shut it down.
- `dashboard` config section (`enabled`/`auto_open`/`port`/`domain_map`,
  deep-merged and validated) + `config-set --dashboard/--no-dashboard/
  --dashboard-port`; `init --dashboard` is NOT added — enable via config-set.
- begin-round ensure hook: spawns the detached server when enabled, reuses
  a live one, cleans stale pid files; failures log a warning and never
  block the round (hard rule: the loop must not depend on the panel).
- Data pipeline (dashboard_data.py, pure functions): per-round numstat diff
  anchored on history commit_shas, module aggregation (domain_map longest-
  prefix over builtin heuristics), event correlation (cancelled/aborted
  rounds honestly kept), round stats. Missing anchors degrade the growth
  view instead of fabricating it.
- Frontend (dashboard.html, zero dependencies): dual-theme design tokens
  (prefers-color-scheme + manual toggle), orthogonal-link evolution tree
  with expansion, evolution card stream with click-to-highlight linkage,
  replay with staggered growth animation (60ms/cap 1.2s) and cursor
  scrubbing, prefers-reduced-motion full degradation.

### Tests

- 526 → 528 (dashboard config/pipeline/lifecycle/server/page-contract suites,
  plus the two begin-round hook tests).

## 1.7.0 (2026-09-26)

Efficiency pass on top of 1.6.0, driven by measuring a real 30-round run
(BrainFog) instead of guessing: what costs tokens per round is the JSON the
agent reads, not the helper's runtime (0.58s per call).

### Added

- `smoke_commands`: a declared place for the cheap between-verification check.
  The loop verifies fully only every `verify_every_rounds` rounds and the
  skill told it to "run a cheap smoke check when one is available" — with
  nowhere to declare it, each round improvised, so the choice differed run to
  run and left no trace in the config. New field (init `--smoke-commands`,
  `config-set --smoke-commands`/`--clear-smoke-commands`), validated as an
  array of strings, and `detect-verify` now reports `smoke_recommended`
  (syntax-level checks per technology) as a suggestion only — it is never
  auto-applied, and a test pins that `--apply` still writes only
  `check_commands`.
- `max_expansion_waves`: a cap that covers what `max_tokens` cannot see.
  Token accounting is diff-based (`500 + 12 x changed lines`), so Deep
  Expansion subagent spend never entered the run's budget: a run could
  outspend every configured limit while `check` reported the budget healthy.
  New config field (init `--max-expansion-waves`, `config-set
  --max-expansion-waves N`/`--clear-max-expansion-waves`), counted on a new
  monotonic `state.expansion_wave_seq` (the 20-entry rolling window saturates,
  so it cannot count a run). `check` reports `expansion_budget`
  (`waves_used`/`max_waves`/`waves_left`) and warns at the cap;
  `expansion-record` refuses past it and names the escape hatch. Default
  `null` keeps existing runs uncapped.
- `round-prep`: the loop's round start in one call. The payload is check's
  loop-driving JSON plus the suggested candidates (brief form), the fresh
  cached analysis, the standing directives and the round number `begin-round`
  would open. The loop used to pay four script calls — and four tool
  round-trips for the agent — per round (check, analysis-load, backlog-rank,
  directive-list) before it started working; one call replaces them
  (measured 2783 bytes vs 3401 across the four). `cmd_check` now builds its
  payload through the shared `build_check_payload`, so the two can never
  drift.
- `backlog-rank --pending-only --top N --brief`: the loop read an 18KB
  ranking every round (16 candidates, 12 of them completed history, plus a
  ~1.1KB `score_breakdown` per entry) to pick a batch the helper had already
  marked. Measured on a real 30-round repo: 18165 -> 1252 bytes with no
  change to any score. Truncation is announced on stderr so a short list is
  never mistaken for the whole backlog.

### Docs

- The config-set guard-rail list no longer claims `check_commands` is
  un-settable: `config-set --check-commands` has written it since 1.6.0
  (verified against the CLI, not inferred from the flag table).
- `references/config.md`, `SKILL.md` and `references/troubleshooting.md`
  document the three view flags, `round-prep`, `smoke_commands` and the
  expansion budget, including why subagent spend is outside `max_tokens`.

### Fixed

- The lock-probe test now patches the probe the current platform actually
  uses. It patched `subprocess.run`, but `_pid_alive` probes with `os.kill`
  on POSIX, so on Linux the test asserted nothing and failed outright on a
  nonexistent PID (v1.6.0 shipped with one red test outside Windows). Added
  the counterpart guard that a genuinely missing PID still reads as dead, with
  both platforms driven through a stubbed probe so the test never depends on
  the host's real tasklist.

## 1.6.0 (2026-09-26)

Self-hosted overnight iteration: the skill ran its own loop against its own
repository for 14 verified rounds. Every fix below came from actually using
the tool (or a debug agent simulating a first-time user) and hitting the
friction for real. The release also carries the P0 anti-early-stop hardening
(an autonomous run once finished ~5 hours before its deadline with a "backlog
exhausted" reason and zero helper pushback — the helper now enforces honest
finishing in code instead of trusting the prompt) plus the ranking/batch and
QA-audit work previously stranded in a floating `## Unreleased` block.

### Added

- `config-set` accepts runtime field flags beyond `expand_after_goals`:
  cadence (`--candidates-per-round`, `--commit-every-rounds`,
  `--verify-every-rounds`, `--checkpoint-every`), budgets with `--clear-*`
  twins (`--max-rounds`, `--max-minutes`, `--max-tokens`),
  `--deadline EXPR`/`--clear-deadline` (init's expression parser),
  `--push`/`--no-push`, `--scan-secrets`/`--no-scan-secrets`,
  `--report-lang`. Every invocation refreshes the state config fingerprint.
  Guard-rail fields stay deliberately unsettable at runtime.
- CLI flag aliases matching natural agent phrasing: `directive-add --directive`
  (= `--text`), `backlog-remove --candidate-id` (= `--id`).
- Direct unit-test packs for the foundation layers: `io` (JSON atomic
  write/read, git state probes, identity), `agent` (detect_python probe
  chain, agent_profile, cmd_detect_agent payload), pure functions
  (`emit_result` contract, config paths/defaults, `validate_config(source)`,
  `any_ready_candidates`, `now_iso`, `git_dir_for`).

- Config/IO/scanner hardening from the QA audit (all reproduced on this repo
before fixing):
  - `max_blocked_in_a_row`/`retries_per_round` reject negatives at load time
    (a hand-edited `-1` used to produce `max_blocked_in_a_row reached (0/-1)`
    — a permanently stopped run); `0` stays legal and is now documented.
  - Budget knobs reject NaN/±inf: `init --max-minutes nan` used to succeed
    (rc 0, literal `NaN` written to config.json, every `check` continuing
    forever); a hand-edited `1e999` is rejected at load.
  - `init` validates the merged config with the same `validate_config` the
    next `load_config` applies, and `init --force` no longer inherits the
    existing config — it rebuilds from defaults + CLI flags, a real recovery
    exit for a config that could never be loaded again.
  - Staged BINARY files produce a `binary-staged` finding (content is not
    scannable), so committing one requires the same explicit
    `--allow-secrets`; the fail-closed promise of the scan now covers blobs.
  - `split_goals` no longer splits on decimal points ("升级到 v1.3.3" stayed
    one goal); the report's goal checkboxes use the same goal-text
    normalization as `all_goals_met` (no more `[ ]` contradicting a stopped
    run); `save_json` fsyncs before the atomic rename and refuses NaN/inf
    payloads; `_pid_alive` treats a PermissionError (EPERM, POSIX) as
    "alive", aligning the fail-closed behavior with the Windows branch.
- Budget & round accounting fixes from the QA/ES audit:
  - Zero-work cancels no longer consume the run: a cancel with no working-tree
    delta against the round's snapshot, no commit, and a life under 10 minutes
    records an `aborted` history entry (labels: 空转取消 / aborted), advances
    no round counter and burns no token base; round numbers stay unique via a
    new `round_seq` sequence (migrated from the old counters). A real-work
    cancel still counts as `cancelled`.
  - Tokens are billed monotonically per run: `run_start_sha` (anchored at
    init) plus the worktree/index delta are two disjoint measurements of the
    run's true total, and each round is charged only the delta above the
    persisted `billed_text`/`billed_binary` water marks. The QA-3 scenario
    (two staged rounds committed in a third) now bills [620, 620, 500] instead
    of double-counting the same 20 lines ([620, 620, 740]). A `--tokens`
    override still advances the water marks so overridden lines are not billed
    again later.
  - `check` reports `blocked_streak` (warning at 1..max-1: "one more blocked
    round stops the run"), a `budget` payload (`max_minutes`,
    `last_activity_at`, `remaining_minutes`, `deadline_remaining_minutes`),
    and boundary-correct schedule hints (round 5 with
    `commit_every_rounds: 5` reports `next_commit_round` 5, not 10).
  - `complete-round --below-threshold` records a quality-failed round as
    completed with its low score (calibration still learns from it) without
    counting blocked — the only in-run exit once `max_blocked_in_a_row` fires,
    since begin-round is refused after the stop. The max_blocked stop message
    and the SKILL.md troubleshooting row point at it.
- Expansion-wave protocol is now recorded in state instead of being an
  unenforceable prompt rule: `expansion-record --lens <lens> ...` writes
  bounded `{at, lenses, added}` records into `state.expansion_waves` (new
  state field, backfilled by migration, capped at 20 waves); an unknown lens
  exits 2, and repeating the previous wave's exact lens set warns (an
  `expansion-wave` warn event) without refusing. `check` reports `wave_no`,
  the `lenses_used` union, and the ordered `lenses_unused` remainder, so the
  16-lens rotation and the Wave 2/3/4 escalation ladder are finally auditable.
- `check` exposes the repository-analysis cache in an `analysis` payload
  (`status` missing/fresh/stale, `reason`, `commits_behind` — how many commits
  the cached analysis predates) and warns when an expansion is due while the
  cache is >=3 commits stale; staleness is a hint, never a stop.
- SKILL.md's Deep Expansion section now maps every lens to a probe command
  and the evidence a subagent must bring back, requires unevidenced proposals
  to be rejected (evidence goes into `backlog-add --evidence`), and defines
  the bar for ever claiming "expansion exhausted" (2 recorded waves x >=3
  subagents x >=3 unused lenses with zero value-gated candidates).
- `finish --force`: `finish` without it is refused (exit 2) while no stop
  condition is reached and value>=floor ready candidates remain (or the
  backlog needs expansion); the refusal mutates nothing, so an open round
  survives a refused finish. A forced finish logs a `finish-forced` event with
  the reason and ready count.
- `goal-met --round <n>` / `--evidence`: `--round` must match a completed
  round in history and marks the claim verified; without it the goal event is
  `unverified: true` and the "all goals met" stop is withheld.
  `unverified_goals()` backs `check`'s new `goals_unverified` payload, plus a
  warning naming the goal while valuable ready candidates remain.
- `config-set --expand-after-goals`: runtime config adjustment that reloads
  and re-validates the config, saves it, and refreshes state's
  `config_fingerprint` (no config-drift false alarm); reopens an "all goals
  met" stop for the expansion phase. `begin-round`'s all-goals-met refusal now
  points at it and reports the remaining ready candidates.
- Test-suite integrity guard: `unittest discover` count must equal the count
  of test methods defined in the source — a duplicate class name that silently
  shadows tests now fails the build.

### Changed

- Ranking/batch fixes from the real-data audit (ES/DX/TG findings, deduped):
  - `_mark_selection`: `expansion`-origin candidates are no longer capped by
    the predicted quota nor cut in the late run (they get their own
    `max_expansion_per_round`, default uncapped — expansion work already
    passed the main agent's value gate). The 40% cutoff now uses the FIRST
    SELECTED entry as its base (the top-ranked entry can be quota-cut, which
    used to raise the bar for everything else and empty the batch) and only
    prunes below-floor entries; above-floor candidates stay eligible until
    the batch is full. The below-floor quick-win fallback fills all remaining
    slots (still obeying origin quotas). Skipped ready entries carry
    `cut_reason` (`quota`/`late_run`/`type`/`cutoff`/`batch_full`).
  - Saturation decay is logarithmic with a 0.35 floor (28 completed over
    threshold now yields 0.417 instead of 4.6e-05), so the dominant type's
    value>=4 work is dented, not vetoed. Effort cost is free within one
    round's batch width and linear beyond it, replacing the unconditional
    log2(1+effort) that fought the batch-width default.
  - `check` runs one `rank_candidates` pass and reports `selected_count` /
    `selected_empty_reason`; `action_hint` is forced to `expand` when the
    batch is empty while ready candidates remain (contract: "work" implies a
    non-empty selection). New warnings: calibration never activated
    (`review_threshold` unset, 3+ completed rounds, zero review scores) and a
    capacity hint when pending >= 2x the batch width.
  - `compute_type_stats` calibration cold start: a type with <3 reviewed
    candidates borrows the run-wide review/value ratio (clamped 0.6-1.5).
  - `backlog-update`/`backlog-remove` refresh `state.type_stats`, so the
    expansion payload can no longer contradict what ranking computes fresh.
- Defaults & seeds: `candidates_per_round` 3 -> 4 (per-round overhead
  amortizes over more work); `begin-round`'s no-candidate refusal now splits
  above-floor from below-only pools ("Only below-floor candidates are ready
  (N). Run Deep Expansion first, or pass --candidate-id explicitly to accept
  a quick win."); `backlog-add --origin expansion --evidence ...` defaults to
  confidence 0.9 (predictions stay 0.75); goal-met seeds skip `bugfix` as the
  default type and default to `risk` 2.

### Fixed

- Miner false-positive root causes (a stocked backlog is only useful if its
  supply is clean):
  - `test-gap` now skips CLI dispatch handlers registered via
    `set_defaults(func=...)` — their coverage lives in CLI-level tests that
    static symbol matching cannot see (32 phantom candidates on this repo).
  - `dead-export` skips `unittest.TestCase` subclasses, resolved transitively
    through local intermediate base classes (20 phantom candidates); fixing
    this immediately surfaced 2 real dead symbols, now removed
    (`io.is_detached_head`, `state.update_candidate_status`).
  - `markers`/`swallowed` skip test files — fixture strings are sample data,
    not tech debt (13 phantom TODO candidates).
  - `hotspot` reads `--name-status` and skips deleted paths (no more
    "review" suggestions for files that no longer exist).
- Branch-drift guard gap in the report layer: `report`/retrospective fall
  back to the real current branch when state records none.
- `diagnose` on a non-git directory is a quiet probe (`is_git_repo: false`,
  exit 0) instead of leaking git_dir_for's `[ERROR]` prefix.
- `init` config-validation errors distinguish flag sources: the message
  points at the offending init flag instead of a config.json that does not
  exist yet.
- Lock acquisition grants a 100 ms grace retry before stealing a
  just-created (empty-looking) lock file, closing the create-vs-payload
  race window; retry budget widened so the grace attempt cannot exhaust the
  loop.
- `check` merges its duplicate `current_branch` probes (one fewer git
  subprocess on the loop's hottest command).
- A state file migrated from a pre-v1.4 version had no `run_start_sha` anchor,
  and token estimation silently fell back to `EMPTY_TREE`: the next
  complete/block/cancel-round billed the entire `EMPTY_TREE..HEAD` diff as one
  round (12 tokens/line) and could fake-trigger `max_tokens` — an early stop
  of exactly the kind this release set out to kill. `load_state` now anchors
  such a state at the current `HEAD` on first load (work committed before the
  upgrade stays unbilled on purpose; an existing `EMPTY_TREE` value is left
  alone so an unborn repo does not churn the file). Guard test:
  `test_migrated_state_anchors_tokens_at_load_not_empty_tree`.
- `references/config.md` still documented the removed `0.7 ** (completed -
  threshold)` saturation decay and the `÷ log2(1 + effort)` effort divisor;
  synced to the shipped formulas (logarithmic saturation with a 0.35 floor,
  batch-width-aware linear effort cost) and the calibration cold-start global
  fallback. `SKILL.md` default `candidates_per_round` 3 -> 4, and
  `--max-expansion-per-round` added to both flag lists.
- Two test classes shared the name `ImportUnitTests`; discovery collected only
  the second, so the first class's 14 cases (among them the sequential-id
  truncation test) never ran while the suite stayed green. Renamed to
  `SeedScoringHelperTests` / `PureHelperUnitTests` (280 -> 303 collected).
- `test_sequential_id_never_reuses_truncated_ids` asserted an unreachable
  scenario: `_next_sequential_id` numbers from the max surviving suffix + 1
  and the bounded lists truncate only from the head, so the maximum suffix
  always survives and no id is ever reused in the normal flow. The assertion,
  and the `_next_sequential_id` docstring that overclaimed a persistent
  "monotonic counter", now describe the actual (and still safe) mechanism.

### Docs

- `references/config.md` documents the full `config-set` field list;
  SKILL.md and troubleshooting.md sync, including using `config-set` to
  raise or clear a stop condition mid-run.
- Root **README.md reinstated** as the repository's GitHub face (owner
  decision, superseding the earlier "skill folders must not ship a root
  README" rule); the version-consistency tests now require it to carry the
  release version, and docs/README.legacy.md records the decision history.

### Tests

- 420 → 460 (36 new regression tests across the miner false-positive,
  config-set, alias, lock-race, and unit-pack areas, plus the P0 early-stop,
  QA-audit, and ranking/batch guards folded in from the previously stranded
  `## Unreleased` block).

## 1.5.0 (2026-07-18)

Mining supply side + early-stop gate hardening (user-reported: weak discovery, still early-stopping).

### Added

- `mine` command (`scripts/autopilot/miner.py`): deterministic scanners —
  `markers`, `swallowed`, `syntax`, `test-gap`, `hotspot`, `dead-export`,
  `docs-drift`. `--apply` writes deduped findings into the backlog
  (`from_mine`, evidence `kind | file:line`); `--dry-run` never mutates.
- `check.action_hint: "mine"` when the backlog is thin/empty and mining is
  stale or never ran — the loop always has a next command before lens expansion.
- `check.mining` payload (`runs` / `last_findings` / `last_applied` / `exhausted`)
  and `state.mining_runs` (bounded at 20) so "mining exhausted" is evidence-based.
- `mining_exhausted()`: only two consecutive zero-finding mine runs (plus empty
  expansion waves when present) count as exhaustion; never-mined is not exhausted.

### Fixed

- Finish gate no longer early-stops with below-floor ready work or a non-empty
  selected batch on the table (the old gate only counted `value >= floor`).
  Finish without `--force` now also requires mining exhaustion when no stop
  condition has been reached.
- `mine --dry-run` does not write `mining_runs` or backlog entries.

### Fixed (usage-review pass: 6 simulated-user debug agents, 32 issues)

Safety & guardrails:
- **Branch guard (P0)**: in feature mode `begin-round`/`commit`/`complete-round`
  now refuse when the repo left the autopilot branch or is on a detached HEAD
  (previously a manual `git checkout main` let commits — and pushes — land on
  the wrong branch silently); `check` warns on branch drift.
- Secret scan no longer skips added lines that start with `++` (they looked
  like diff file headers); commit refusals now name the matched pattern; staged
  `.autopilot/**` is refused when `track_state` is false even via `git add -f`;
  fail-closed paths for unreadable staged diffs got test coverage.
- Pre-existing user changes can no longer be absorbed into autopilot commits:
  rounds record their dirty-start snapshot (`start_dirty_files`) and `commit`
  refuses staged files from that set unless `allow_uncommitted_changes` is true
  (batch mode included). `commit --round <n>` validates the round exists in the
  completed history instead of acting as an unchecked escape hatch.

Miner:
- Dedup keys no longer embed line numbers or churn counts, so line drift and
  hotspot churn no longer duplicate candidates; hotspots require >=2 commits.
- `mining_runs` records the **new**-findings count and exhaustion means two
  consecutive zero-new runs — resident-finding repos no longer lock the finish
  gate behind `--force` forever.
- Test-file detection requires real test names/paths (`contest.py` no longer
  counts); swallowed-exception scan catches trailing-comment and one-line
  forms; non-ASCII paths print unescaped (`core.quotepath=false`); null-bytes
  files report as encoding problems, not `line 0` syntax errors; TODO/XXX in
  code files rank as `refactor`, not `docs`; `SKIP_DIRS` matching is
  case-insensitive; `--limit 0` means zero instead of falling back to 20.

Lifecycle & state:
- `begin-round` validates everything (dirty tree, branch, candidate
  availability) before any state mutation — refusals no longer leak candidates
  into `picked`.
- `init` validates the git repo before touching the filesystem and `--dry-run`
  is side-effect free everywhere; refusals on uninitialized repos no longer
  create `.autopilot/`.
- Future state schemas are refused instead of silently misread; a null
  `state.json` reports "not a valid JSON object" instead of "not found";
  duplicate `init` exits non-zero; `check`/`read` report "Not a git repository"
  instead of a misleading missing-state error.
- `finish` warns with the list of uncommitted files and, in feature mode, says
  explicitly that commits remain on the autopilot branch (output and
  retrospective, zh/en); reports fall back to the real current branch when the
  state records none.
- `goal-met` without `--round` now says the goal is unverified; check warnings
  no longer contradict each other on thin backlogs (single mine-first path) and
  the dirty-tree warning only appears when it applies.
- `detect-agent` derives `skill_dir` from its own location and probes the
  returned `python_cmd` before recommending it.

Batch cadence:
- `commit` on a non-flush round warns with the next flush round
  (`commit_every_rounds` contract is now observable instead of silent).

## 1.4.0 (2026-07-18)

Skill-packaging and correctness fixes from a full review pass.

### Fixed

- Skill folder no longer ships a root `README.md` (skill-creator ERROR); install/overview lives in `references/overview.md`.
- `SKILL.md` slimmed under the 5000-word guidance; expansion lenses, Wave 0, and the full troubleshooting table moved to `references/`.
- Deep Expansion lens names in docs now match `EXPANSION_LENSES` exactly (16 names; `docs` and `ux-copy` are separate — the old merged `docs / ux-copy` row was rejected by `expansion-record`).
- `config-set --no-expand-after-goals` can turn the expansion phase off again (previously only `--expand-after-goals` existed).
- `TOKEN_BASE` is only charged when a round introduces new text/binary units; verify-only / commit-only closes no longer drain `max_tokens`.
- `parse_time` only rewrites a trailing `Z` as UTC (mid-string `Z` is left alone).
- Secret scan: `api_key` pattern uses a scoped `(?i:...)` group (Python 3.11+ safe); `+++` path parsing handles quoted paths and `/dev/null`.
- Path guard: `dir/**` now matches the directory itself and its whole subtree (gitignore-like).
- `detect-verify` probes the installed `gitleaks` subcommand (`detect` vs `git`) instead of assuming `gitleaks git`.
- CLI honors the `[ERROR]+2` contract for `ValueError`/`KeyError`/`TypeError`, and UTF-8 stdio wrapping is idempotent with line buffering.
- Log rotation keeps 3 generations (`.1`–`.3`) instead of overwriting a single `.1`.

### Changed

- Audit notes and proposal archive moved under `docs/`; version authority and the consistency test now reference `references/overview.md`.
- `config-set` help and troubleshooting document both expand flags.


## 1.3.3 (2026-09-18)

Second autonomous iteration: fourth scout wave (CLI surface / test blind-spot
matrix / docs consistency) landed 10 more candidates.

### Fixed

- `init` numeric knobs reject negatives up front (a negative
  `max_blocked_in_a_row` silently stopped the loop at zero rounds; the other
  knobs faked a successful init then failed on every config load). Zero stays
  legal where it is meaningful (max_rounds 0, retries 0).
- `complete-round --review-score` is range-checked (1-5) even without a
  configured threshold — an out-of-band 99 pinned the learned calibration.
- `commit --round 0` no longer falls through the truthiness check into the
  open-round error; `--round`/`--tokens` reject non-positive values.
- `detect-verify --json` now carries `ok`/`message` in every mode (shape was
  drifting between --apply and plain).
- docs: `--goal`/`--goals-from-prompt` last-write-wins documented; the
  "skipped in batched mode" commit description corrected; the self-depending
  example fixed; README project layout lists all nine modules; the Windows
  junction example is free of stray control characters.

### Added

- Test blind-spot matrix round: merge-commit undo guidance, goal-met seed
  boundary values, push-without-remote JSON contract, a 15-command `--dry-run`
  zero-mutation scan, report `--output` relative-to-repo positive case,
  uninitialized-guard matrix, commit identity gate, push-failure warning
  branch, agent empty-env contract. The dry-run scan caught and fixed a real
  bug (`directive-remove --dry-run` actually removed the directive).
- `directive-list` entries now carry their 1-based `index`
  (`directive-remove --index` source of truth).
- `AUTOPILOT_AGENT` invalid values warn on stderr instead of silently
  degrading to generic.
- `depends_on`: unknown ids warn loudly (forward references stay legal);
  self-reference is rejected.

## 1.3.2 (2026-09-17)

Hardening release from a three-agent audit (robustness deep-review, functional
completeness, test quality). No protocol changes — the v1.3.0/v1.3.1 semantics
are unchanged; the edges stop biting.

### Fixed

- `--confidence nan` no longer bypasses the discount: the CLI uses a chained
  range check, the scorer falls back to the default for NaN/±inf/bool/strings,
  and NaN `review_score`s are dropped from both calibration ledgers (NaN used
  to clamp to the 1.0/1.5 ceilings).
- `_mark_selection`: the below-floor quick-win fallback now obeys the predicted
  quota and the late-run cut (a value-2 predicted quick-win used to sneak into
  every batch, even with `max_predicted_per_round: 0`).
- Seed state machine: terminal states (`verified`/`refuted`/`rejected`) are
  immutable — a cancelled round can no longer resurrect a verified seed; seed
  write-back checks ownership (`promoted_candidate_id` must belong to the
  closing round) and warns (instead of logging success) when the seed no longer
  exists; write-back is folded into the round-close state save (no crash window
  between backlog and seed ledger).
- Seed ids are monotonic after truncation — a truncated `seed-001` is never
  reused, so stale `from_seed` references can't point at a newer seed.
- Junk in hand-edited state degrades cleanly: seed `value`/`effort`/`risk`
  strings coerce or fall back; `±inf` values raise no OverflowError; corrupt
  `type_stats` values and a malformed `backlog.json` fail with a clear message
  and exit 2 (no traceback); `check`'s expansion payload skips non-dict entries.
- Windows console/pipe encoding: the CLI forces UTF-8 (errors replaced) on
  stdout/stderr — zh reports no longer crash on non-GBK characters.

### Added

- `seed-reject --id <id> --reason "<why>"`: the missing write path for the
  `rejected` state (evidence check refused the hypothesis). Rejected seeds
  leave the open-seeds list, making SKILL.md's Wave 0 exception reachable.
- `goal-met` dedupes repeated `--next-step` seeds per goal; goal events record
  the last completed round's commit SHA(s); seeds carry `source_event_id`.
- `report` / `retrospective` ranking now honors `completed_goals` (goal-chain
  bonus consistent with `backlog-rank`).
- Test suite: adversarial direct unit tests (state machine, confidence table,
  id monotonicity, selection quota edges), 10 subprocess regressions (corrupt
  data fail-clean, dedupe, write-back warn, field coercion), config validation
  matrix entries, wider `GIT_*` isolation, subprocess timeouts.

### Expansion waves (same release; two scout waves + hardening rounds)

- `goal-met` text mode now echoes the created seed ids (Wave 0's next step
  needs `--from-seed <id>`; agents used to guess `seed-00N`).
- Goal text is normalized (NFC + zero-width/BOM stripped + trimmed) before
  recording and inside `all_goals_met`: an invisible character could mark a
  goal "met" while the stop condition stayed false forever. A `--goal` that
  matches no configured goal records a warning.
- Report/retrospective hardening: markdown table cells (backlog, history,
  seeds) and suggestion lines sanitize newlines/tabs/pipes; `--output`
  relative paths resolve against `--repo`, not the process CWD.
- Audit trail: `state-migrate`, `config-drift`, `analysis-load`, and
  `integrity` (state/backlog validation failures) events in `log.jsonl`.
- `directive-remove --index N` (directives.json was add-only); malformed
  `directives.json` now fails cleanly like state/backlog.
- Fail-clean completeness: corrupt `current_round` (missing `round`) and
  junk `history` entries exit 2 with a message instead of KeyError/AttributeError
  (the old failure bricked the run — begin-round refused while every closer
  crashed); malformed `--repo` paths surface as `[ERROR]` + exit 2 via an
  OSError net in the entry point.
- Guidance: unknown seed/candidate ids list the open/pending ids; `undo-round`
  on a merge commit points at `git revert -m 1` instead of a phantom conflict.
- Consistency: `backlog-update`/`backlog-pick`/`backlog-remove` require init
  like their siblings; empty commit subjects no longer leak bare SHAs into
  `suggested_themes`.
- Performance: the test harness dispatches in-process (589 interpreter startups
  eliminated), setup writes repo-local git config directly, and hot commands
  merge redundant git calls — suite runtime 358s → ~193s.
- SKILL.md: init flag list completed (`--allow-uncommitted-changes`/`--force`),
  lens→helper-signal map for Deep Expansion subagents.
- Guards and scanners: `deny_paths`/`allow_paths` patterns are normalized to
  forward slashes (a Windows-style `secrets\\` deny rule silently never
  matched — fail-open) and `**/` collapses to zero directories
  (`src/**/*.py` now also matches `src/a.py`); the secret scanner catches
  PKCS#8 `ENCRYPTED PRIVATE KEY` blocks and `gho_/ghs_/ghu_/ghr_` GitHub
  tokens, and `sk-` requires a word boundary (long hyphenated words ending in
  "-sk-" no longer block commits); `--deadline` overflow durations fall to the
  friendly parse error; `git push` prefers `origin`/`upstream` over the
  alphabetically-first remote.
- Release hygiene: `scripts/autopilot/__init__.py` gains `__version__` (single
  authority) with a `--version` flag and a consistency test across SKILL.md /
  openai.yaml / README / CHANGELOG; CI adds a concurrency group, job timeouts,
  and package-wide syntax check; README documents the shipped-file surface and
  junction/symlink installs; `agents/openai.yaml` gains a description with the
  trigger phrases.

## 1.3.1 (2026-09-17)

刀 B — anti-noise guardrails for direction-seed predictions (proposal §6).
Predictions now carry belief, a quota, and their own ledger, so a run of wrong
hypotheses cannot crowd out real observed work.

### Added

- Candidates accept `origin` (`observed` | `predicted` | `expansion`, default
  `observed`; `--from-seed` defaults `predicted`), `confidence` (0.5-1.0,
  default 0.75 for predicted/expansion, observed always 1.0), `based_on` (the
  goal the prediction came from; from-seed defaults to the seed's source goal),
  and `evidence` (audit-only).
- Expected-mode scoring multipliers: `confidence_factor` (the candidate's
  confidence) and `goal_chain_factor` (`based_on` matches a met goal → 1.08,
  deliberately below the dependency-unlock 0.15). Both land in
  `score_breakdown` and are neutral (1.0) for observed work — legacy ranking
  order is unchanged.
- Predicted sub-account (`compute_predicted_account`): predicted-origin
  candidates keep their own completed/blocked/review ledger and are excluded
  from `type_stats`. With fewer than 3 resolved predictions, scoring uses a
  conservative prior (type success rate × 0.75); with enough samples the
  sub-account's own success rate and calibration take over, so consecutive
  failures sink future predictions without contaminating observed work.
- `max_predicted_per_round` (default 1): at most one predicted-origin candidate
  per recommended batch; `null` disables. Observed wins score ties. In the
  late run (progress > 0.7) predicted work is cut entirely while observed
  candidates remain ready.
- `report` gains a 「方向假设 / Direction seeds」 section: the seed table plus
  the predicted sub-account hit rate; predicted backlog rows are marked `[P]`.
- `init --max-predicted-per-round N`; `ranking_mode: classic` deliberately
  ignores every prediction factor (legacy ratio stays pure).

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
