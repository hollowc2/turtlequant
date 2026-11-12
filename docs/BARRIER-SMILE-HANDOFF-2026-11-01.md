# Barrier-smile research handoff — 2026-11-01

Status: **insufficient evidence to deploy the candidate**. Resume after fresh
data covers additional expiry groups. Leave the running shadow bot unchanged.
No bot restart, deployment, configuration change, or trading activation was
performed during this investigation.

Follow-up: richer capture was approved and activated on Helios at approximately
`2026-11-01T23:30:55Z`; see `docs/RESEARCH-CAPTURE.md` and the dated deployment
record. The first production snapshot captured 317 markets and 40 token books;
Binance touch coverage is still backfilling. This is a telemetry change, not
activation of the trading candidate. The original investigation did not restart
the bot; this subsequent approved activation did. The limitations below describe
the original October evidence.

## What prompted the investigation

The November 1 checkpoint covered 82 resolved markets observed in the deployed
168–2160 hour entry window. All 82 shared the November 1 expiry, so they are
not independent market environments. The report's Brier columns use all
tenors even in its entry-window section.

Recalculating Brier only within that window gave:

- Legacy: 0.08647.
- Smile: 0.09139.
- Market midpoint: 0.08904. Lower Brier is better.

Smile's 15 hypothetical down-barrier NO trades lost 12 times and averaged
−0.23902 per share. These are first-qualifying-snapshot counterfactuals, not
the bot's actual portfolio returns. Consensus remained useful; do not remove
it based on the all-tenor results.

The original checkpoint rules in commit `1e86f96` said to stop entries and
rethink when smile no longer beats market-mid Brier. We discussed that rule
and chose to continue the existing **shadow** run unchanged to gather
evidence, without promoting the strategy or increasing risk.

## Confirmed mechanics issue and candidate

`smile_probability` scales flat-GBM touch probabilities by a ratio derived
from terminal digital probabilities. That approximation can produce a
discontinuity at the touch barrier. With the October 5 BTC trade's forward,
IV and slope, it gives about 95.93% just above the down barrier, then 100%
at the barrier. Under its continuous-price assumptions the limit should
approach 100% smoothly.

The offline candidate, `forward_flat_touch_v1`, removes only that terminal
scaling for touches. It retains the forward-implied drift and strike IV.
It is a flat-GBM approximation, not a calibrated local-volatility model.
European pricing and the trader's production pricing path were not changed.

Implementation: `src/turtlequant/barrier_research.py`.
Comparison runner: `scripts/compare_barrier_candidate.py`.
Tests: `tests/test_barrier_research.py` and
`tests/test_compare_barrier_candidate.py`.

## Offline result and its limits

The comparison could reconstruct 593 unclipped smile-mode diagnostic events
across 10 markets: 591 up-barrier events across nine markets and only two
down-barrier events from one market. It excluded 32 legacy-mode events
(the logged sigma is not necessarily smile IV) and three clipped or
already-touched events.

Equal-market Brier on this selected matched subset:

- Current smile: 0.23463.
- Candidate: 0.22978.
- Market midpoint: 0.20818.

The candidate improves on the model but still trails the market. These
numbers describe the diagnostic subset, not the 82-market window above.

Only one entry clears the baseline's net-edge threshold in the captured
eligible subset. The candidate blocks it and has no qualifying entries
there. It was the October 5 NO entry on BTC dipping to $85,000
(`market_id=5288011`), with a recorded shadow loss of $5.93. Holding the
captured fill cost fixed, removing scaling reduces estimated net edge from
5.08% to 4.45%, below the 5% threshold.

This is a retrospective ablation, not proof of improved profitability.
Diagnostics are selected by the baseline's gates; candidate-only
opportunities are absent. Reconstruction holds the recorded side and
fee-inclusive execution cost fixed. It does not replay Kelly sizing,
portfolio caps, re-entry, exits, or a NAV path. Do not infer dollar strategy
returns or confidence intervals from it.

Verification completed: all 271 tests passed, repository Ruff checks passed,
and diff whitespace checks passed. Independent Brownian-bridge checks
supported the flat reflection formulas. Boundary tests cover both touch
directions. The original production pricing file's hash matched the frozen
baseline after the work.

## Evidence and reproduction

Baseline checkout: `3927f5b6a6826544ac74d6af570bae1f2d706e07`.
Observation cutoff: `2026-11-01T16:06:00+00:00`.
Selected marks span October 1–25 UTC.

Local artifacts:

- `data/research/barrier-smile-2026-11-01/evidence.json`: filtered marks,
  Gamma metadata/resolutions, cohort history, retained diagnostics, source
  paths and hashes.
- The same directory contains `collect.py`, `analyze.py`, and the original
  `results.json`.
- `data/research/barrier-flat-candidate-2026-11-01/spec.json`: frozen
  single-candidate experiment specification; no parameter search.
- The candidate directory also contains `results.json` and `validation.json`.

These data directories are **Git-ignored local artifacts**, not a committed
or independently backed-up dataset. At handoff the candidate module, runner,
and tests are also uncommitted. Preserve them before changing checkouts or
cleaning the workspace. Do not overwrite the original evidence or results;
use a new run directory for subsequent analysis.

From the repository root:

```bash
.venv/bin/python scripts/compare_barrier_candidate.py --evidence data/research/barrier-smile-2026-11-01/evidence.json
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

## Resume checklist

1. Read this note and inspect the working tree before editing. Verify the
   deployed version and shadow configuration read-only; do not assume the
   local candidate has been deployed.
2. Check what new observations and resolved expiry groups actually exist.
   Prefer fresh evidence after this investigation, with multiple additional
   expiry groups. More snapshots of the same outcome are not independent
   confirmation. No numerical promotion sample size has been established.
3. Audit feature coverage **before attempting a full replay**. Existing marks
   omit spot, forward, slope, separate legacy/smile IVs, and prior-touch state.
   Waiting alone does not repair those omissions in the original October
   evidence. The subsequent approved schema-v2 capture adds these fields;
   use its source hashes, exact timestamps, sample flags and touch coverage
   rather than assuming all new observations are complete.
4. A fair new comparison needs timestamped spot, forward, smile IV, slope,
   contract start/expiry and prior-touch state, baseline/candidate prices,
   and executable quotes/depth/fees for both strategies, including
   baseline-blocked opportunities. Respect contract-specific start times:
   some repeated monthly questions only count touches after market creation.
5. Freeze the new evaluation dates and decision criteria before inspecting
   results. Keep this October cohort as development evidence. Evaluate both
   directions, both assets, and concentration by expiry; include market-mid
   calibration as a benchmark and credible net execution costs.
6. Do not promote on the basis of avoiding the known $5.93 loss. Require
   fresh calibration and execution evidence; report missed winners and
   candidate-only trades as well as avoided losses. If feature coverage is
   still insufficient, keep that conclusion explicit rather than treating
   missing trades as zero returns.

Next decision: whether the candidate merits a controlled paper comparison
after fresh evidence. Research results alone do not authorize deployment,
starting another bot, or enabling live trading.
