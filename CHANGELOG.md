# Changelog

All notable changes to auto-iterate-project are documented here.

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
