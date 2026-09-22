# Post-Goal Direction Prediction (Wave 0)

Deep Expansion answers "what else could be improved?"; this protocol answers
"because we just shipped X, what does the user need next?". Completed goals become
*direction seeds* (hypotheses in `state.json` under `goal_seeds`) that are consumed
before any lens scanning.

## When it triggers (unattended, never ask the user first)

1. `goal-met` just marked a goal met.
2. `check` reports `"phase": "expand"` and the first expansion after that is about to run.
3. A goal is met mid-run while `check` still reports `continue: true`.

## Generate 2-3 hypotheses

Each is a hard causal sentence: "because A is done, B is next, and the evidence is C
(a verifiable repo fact)". Four causal classes cover most cases:

| Class | Template | Example |
|---|---|---|
| enablement | A unlocked capability U → wire U to a real entry point / docs / CLI | just added API X → add README + example |
| exposure | A changed surface S → harden S's tests / error paths | just changed auth → add 401/403 matrix |
| journey | with A in hand the user will do J next → make J not break | import works → handle first-sync failure |
| debt | A bypassed D to hit the goal → remove D | hardcoded path → config option |

## Hypothesis loop

1. Predict (causal sentence + evidence).
2. Verify the evidence at minimum cost — a hypothesis whose evidence is gone is
   rejected via `seed-reject --id <id> --reason "<why>"` (dead seeds leave the open
   list so the Wave 0 exception can fire).
3. Value-gate via `backlog-add --from-seed <id>` + `backlog-rank` (same red lines as Expansion).
4. Work the most credible 1-2 via `begin-round`.
5. The seed resolves automatically from the round outcome:
   - `complete-round` → `verified`
   - `block-round` → `refuted` (block reason as notes — failed hypotheses never
     flow back to game the stats)
   - `cancel-round` → `open` again

A validated causal chain may spawn one more layer of hypotheses, at most two layers deep.

## Anti-noise guardrails (刀 B)

- Promoted seeds carry `origin: "predicted"` and a `confidence` (default 0.75) that
  discounts their score — belief is not value.
- `max_predicted_per_round` (default 1) caps how many predicted candidates enter one
  recommended batch; observed work wins score ties.
- In the late run (progress > 0.7) predicted work is cut entirely while observed
  candidates are still ready.
- Predictions keep their own blocked/review sub-account: consecutive failures sink
  future predictions without contaminating observed work's stats. The run report
  shows the hit rate (「方向假设」section).
- `expansion`-origin candidates are a separate lane with their own quota
  (`max_expansion_per_round`, default null = uncapped) and are **not** cut in the
  late run — they already passed the main agent's value gate.
- When tuning `max_predicted_per_round` or confidence defaults, justify the change
  from the sub-account's hit-rate samples — not from intuition.

## Wave order

- **Wave 0 (seed wave)**: right after a goal is met, the first expansion must consume
  open seeds — verify evidence, value-gate, promote, work. Lens-scan Deep Expansion
  (16 lenses) is **forbidden** in this wave. Exception: if every open seed has been
  rejected/refuted (none survives the evidence check), skip straight to Wave 1+
  instead of burning a round re-judging dead seeds.
- **Wave 1+ (lens waves)**: see `references/expansion-lenses.md`. Subagent prompts
  should attach a summary of still-open seeds; proposals hitting the same causal
  chain merge with the seed instead of double-counting.

`check` in expand phase carries the `expansion` context (open seeds, completed goals,
saturated/underused types, suggested themes) so both the main agent and its subagents
reason from it instead of rescanning.
