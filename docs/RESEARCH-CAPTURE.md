# Research capture

Activated 2026-11-01 at approximately `23:30:55Z` on the existing Helios
shadow bot. The first capture completed successfully. See
`docs/RESEARCH-CAPTURE-DEPLOYMENT-2026-11-01.md` for release identity,
validation and rollback. The trading candidate remains offline-only.

`RESEARCH_CAPTURE=true` / `--research-capture` enables a read-only background
worker in shadow mode. Paper, live, and dry-run modes do not start it. Trading
still uses the existing pricing model, consensus, sizing and exit rules. The
offline forward/flat-IV candidate is logged only and cannot submit orders.

## Evidence

At the existing marks cadence (900 seconds by default), schema-v2 marks add:

- Spot, explicit valuation timestamp, forward, separate legacy and smile IV,
  skew slope, full-precision model probabilities and candidate touch price.
- Market/condition/token identifiers and Gamma question, description,
  creation/start/end fields, resolution source and slug.
- Both pricing legs' inputs for range markets.
- Entry-window, threshold, allowed-side and consensus configuration, and the
  Gamma list's fetch timestamp. Both models use the recorded valuation time;
  the formulas are unchanged.

The worker independently enriches a copy of the snapshot with:

- Real YES and NO token books, each with up to ten levels per side and its
  own observation time, source, fee lookup time and schedule. Missing books
  and fees remain missing. Fees can come from the existing one-hour CLOB
  cache; they are not labeled as newly observed. Quotes follow the pricing
  timestamp, so future replays must respect that latency.
- Binance BTCUSDT/ETHUSDT final one-minute candle evidence for prior touches.
  The public market-data-only endpoint is
  `https://data-api.binance.vision/api/v3/klines`; no credentials or proxy
  exchange candles are used.
- Sampling, dropped-snapshot counts, capture completion times, payload hash,
  and pricing/capture source-code hashes.

## Coverage and touch labels

Books are sampled round-robin across **all observed markets in the configured
entry window**, before consensus, direction or portfolio gates. Up to 20
markets (both tokens) are sampled per snapshot. This includes gate-rejected
markets but does not cover the entire universe simultaneously. Unselected
markets are explicitly labeled `not_sampled`, never zero fills. Sampling
cursor and counters restart with the process, with a distinct run timestamp.

Touch history recognises only the two inspected Binance monthly templates:
calendar-month rules counted from 00:00 ET on the first, and replacement
markets counted from their creation timestamp. Original metadata is always
retained for other rules, which remain `unknown` until a parser is validated.

Backfill is bounded to three pages of up to 1,000 minutes per asset per
snapshot and resumes from the earliest missing minute. A SQLite index tracks
received minutes; compressed raw pages preserve source payloads, retrieval
times, request ranges and hashes. Gaps, outages and invalid OHLC are explicit.

`prior_touch.state` is one of:

- `confirmed_touched`: a completed Binance minute wholly within the contract
  window witnesses a touch before the valuation timestamp.
- `no_touch_in_completed_candles`: the completed-minute interval is fully
  covered and contains no touch. This is **not** proof about the current
  incomplete minute; that uncertainty and the exact coverage cutoff are
  retained separately.
- `unknown`: coverage/rules are incomplete, or a creation-boundary candle
  touched but cannot show whether the touch happened before creation.
- `not_applicable`: a terminal contract, rather than a touch market.

The current bot spot feed is labeled as model input, not Binance resolution
evidence. Neither spot observations nor an absent quote can establish that
a barrier has never touched. The first stored candle observation is retained;
the collector does not silently rewrite historical candles on later revisions.

## Storage and operation

Under the mounted state directory, `research/` contains:

- `snapshot-<UTC timestamp>.json.gz`: one atomic compressed snapshot per file.
- `binance-raw-<asset>-<minute>-<payload hash>.json.gz`: immutable source pages.
- `binance-minutes.sqlite`: derived minute index for coverage/touch checks.
- `status.json`: last completion/error, run start, drops, sampling counts and
  backfill progress. `collecting` means the last capture completed; inspect
  timestamps and coverage fields for freshness/completeness.

No automatic deletion occurs. The worker stops adding evidence at 2 GiB of
research storage or below 1 GiB free filesystem space, writes a storage-limit
status, and leaves trading running. Archive/preserve evidence before changing
the quota. Existing rotating marks are retained separately and can rotate
faster because schema-v2 rows are larger.

The bounded queue holds one pending snapshot. A busy worker can drop new
submissions explicitly; active work can be interrupted by process shutdown.
Atomic compressed files protect previously completed evidence. Temporary
files from an interrupted write are not completed observations. There is no
extra bot instance, wallet access, notification, or order operation in capture.

Use these new observations as prospective evidence. The inspected October
cohort remains development data. Evaluate multiple new expiry groups before
considering any strategy promotion; this collector does not authorize one.
