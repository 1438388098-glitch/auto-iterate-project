# Deep Expansion Lenses

Single authority for lens names is `EXPANSION_LENSES` in `scripts/autopilot/state.py`.
`expansion-record --lens <name>` refuses any name outside that tuple. Keep this table
and the constant in the same order and the same spelling.

## Lens catalog (16)

| Lens | Probe command (adapt to the repo) | Required evidence |
|---|---|---|
| architecture | `git log -p` on the hottest files plus an import-direction scan | layering violations / circular imports as module:line pairs |
| tests | `python -m coverage run -m pytest && coverage report --show-missing` (or the repo's equivalent) | list of uncovered public entry points / branches, with file:line |
| security | `pip-audit`; `pip list --outdated` (Node: `npm audit`) | outdated/vulnerable dependency list with versions |
| performance | `python -m cProfile -s cumtime <real workload>` | hotspot functions with elapsed-time percentages |
| docs | walk the README quickstart end-to-end by hand | step-by-step friction points (which step, what tripped) |
| dead-code | public symbol audit + `grep -rn "TODO\|FIXME\|HACK"` | unused/dead symbols and unfinished work with file:line |
| api-design | read the public entry-point signatures and their docs | inconsistency / breaking-change risks as symbol:line |
| concurrency | `grep -rn "threading\|asyncio\|Lock\|thread=True"` | shared mutable state and missing cleanup, file:line |
| i18n | grep for hardcoded user-facing strings and non-ASCII handling | encoding/copy leaks with file:line |
| config | diff `--help` output against `references/config.md`/defaults | defaults that disagree with docs, flag by flag |
| observability | `grep -rn "except Exception\|except: pass\|TODO\|FIXME" --include=*.py` | itemized list of swallowed exceptions / silent failures (file:line) |
| packaging | install/build smoke (`pip install -e .` or equivalent, then `--help`) | broken entry points / missing metadata, step by step |
| data-integrity | exercise migration/rollback paths and idempotency tests | gaps where data can be corrupted or half-written |
| ux-copy | walk the main CLI help and user-facing messages end-to-end | unclear/error-prone copy as string:line |
| dependency-graph | list "A must land before B" pairs from imports/entry points | explicit pairs, each promoted with `backlog-add --depends-on` |
| cross-cutting | `grep -rn "timeout\|retry\|backoff"` at network/subprocess call sites | call sites missing timeouts/retries, file:line |

## Lens → helper-signal map

| Lens family | Helper signals to ground the proposals |
|---|---|
| tests / coverage gaps | `check` warnings, `state.type_stats` blocked rates, per-type test counts |
| architecture / dead-code / api-design | `analysis-load` cache, `git log -p`, public symbol audit |
| security / config surface | `secret-scan`, `config.py` validation branches, `references/config.md` |
| docs + ux-copy + CLI ergonomics | diff between `SKILL.md`/`references/*` claims and `--help` output |
| observability / data-integrity | `.autopilot/log.jsonl` event coverage, migration tests in `scripts/test_autopilot_state.py` |
| any origin tagging | `backlog-add --origin expansion` (quota: `max_expansion_per_round`, default uncapped) |

## Rotation rules

1. Pick this wave's lenses from `check`'s `lenses_unused`, never from memory.
2. Record the wave with `expansion-record --repo <repo> --lens <lens> ...` (repeat `--lens` per lens).
3. Repeating the previous wave's exact lens set triggers a `[WARN]` and an `expansion-wave` warn event.
4. Each subagent must return 2-5 concrete improvement candidates **with evidence** (file:line or probe output).
