# Research capture deployment — 2026-11-01

Status: **activated and first capture verified**. Corey explicitly approved
the named deployment in the conversation. The bot restarted at approximately
`2026-11-01T23:30:55Z` (15:30 PST). Validation at `23:31:20Z` confirmed the
approved image, healthy service, unchanged exporter, one bot instance, all
six original simulated positions, no new orders and no startup tracebacks.

First production capture: `2026-11-01T23:30:57.609129+00:00`, completed at
`23:31:02Z`: 317 market rows, 142 candidate touch prices, and 40 real token
books across 20 sampled markets out of 88 in the entry window. Source and
payload hashes were verified. Both assets fetched 3,000 Binance minutes;
prior-touch coverage is still backfilling and incomplete intervals are
explicitly unknown. Initial research storage was 741,098 bytes, with no
dropped snapshots. No rollback was needed.

Ongoing cadence is the existing 900-second marks interval. A subsequent
scheduled capture was not yet due during this initial validation; check
`research/status.json` after approximately `23:46Z` for the next completion.
The remote release directory contains `activation.json` (including the
rollback baseline) and `latest-validation.json` with the measured results.

## Scope and baseline

- Host/account: `billy@helios`.
- Existing Compose project: `turtlequant-app`, directory `/opt/turtlequant-app`.
- Only service to recreate: `turtlequant-bot`. Do not recreate the exporter.
- Existing checkout: clean `main` at
  `3927f5b6a6826544ac74d6af570bae1f2d706e07` before staging the release directory.
- Running image: `turtlequant:local`, image ID
  `sha256:4afa9816cb021dab4e11f563fb212836e9d4ab8e22baf9f64387a6d777003a11`.
- Preflight: one healthy shadow bot, healthy exporter, six open simulated
  positions, recent ledger orders marked shadow / `sent=false`. About 15 GiB
  filesystem space was free before the candidate build.

This release enables observation only. It keeps smile pricing, consensus,
YES/NO trading, the 5% entry threshold, 168–2160 hour window and EV exits.
Both models receive one recorded valuation timestamp for reproducibility;
their formulas and strategy parameters are unchanged. The research candidate
is never selected for order decisions. The capture client is public-data
only and does not load wallet credentials or invoke order methods.

## Staged release

- Image: `turtlequant:research-capture-20261101`.
- Image ID:
  `sha256:b9b49886c150a16be744c5abf3ebf48949d175a6336be89ba1d4b6f78e216912`.
- Revision label:
  `3927f5b6a6826544ac74d6af570bae1f2d706e07-capture-340df2ca5c8c`.
- Archive SHA-256:
  `340df2ca5c8ca246189814597277b9e2b860cc069a827fafd2cf9ea96203f368`.
- Remote release directory:
  `/opt/turtlequant-app/releases/research-capture-20261101`.
- That directory contains source archive, extracted source, `build.log`,
  `compose.capture.yml`, and the smoke-test evidence under `smoke/research/`.
- Main remote source files and default image tag were not replaced.
- Post-build disk check: approximately 13 GiB free; both existing containers
  still healthy on the baseline image.
- Local code changes are uncommitted. Keep the source archive as the frozen
  release identity rather than treating the baseline Git SHA as sufficient.

The dedicated image built successfully. All 278 local tests, repository
Ruff checks and diff whitespace checks passed. An isolated one-shot image
smoke test completed at `2026-11-01T23:25:07Z`: three real monthly BTC markets,
six captured CLOB token books, model and candidate inputs, and 1,165 Binance
minute candles with complete required minute coverage. No Trader instance
or order method was invoked. This is **smoke evidence**, not an ongoing
collector or fresh strategy performance sample.

## Approved activation command (executed)

First recheck image IDs, bot uniqueness/health and current position/order
state, since these can change after the preflight above. Confirm the staged
override only changes the bot image and `RESEARCH_CAPTURE=true`.

From `/opt/turtlequant-app` on Helios:

```bash
docker compose -p turtlequant-app -f docker-compose.yml -f releases/research-capture-20261101/compose.capture.yml up -d --no-build --no-deps turtlequant-bot
```

This replaces the existing instance, with a brief restart; it must not start
a second bot. Existing `/opt/turtlequant/state` and `/opt/turtlequant/data`
mounts remain in use. Do not run a new standalone trading container.

Validate:

1. Exactly one `turtlequant-bot` runs the staged image and becomes healthy;
   the exporter remains unchanged and healthy.
2. Filter startup logs for errors and confirm the same shadow strategy
   settings; never print secrets or full container environments.
3. Check positions and recent orders for continuity and shadow-only behavior.
4. Verify `/opt/turtlequant/state/research/status.json`, a new atomic snapshot,
   source hashes, both token books/fee statuses and Binance coverage fields.
   Unknown touch coverage must remain explicitly unknown.
5. Inspect a subsequent scheduled snapshot to establish continuing capture,
   not just startup success. Update this record with activation time, image
   ID, observation counts, quota usage and validation outcome.

The richer captures run at the existing marks cadence, normally 15 minutes.
Books sample up to 20 entry-window markets per snapshot across both tokens;
unselected markets are labeled. Research storage stops safely at 2 GiB or
less than 1 GiB free, without deleting evidence or stopping trading.

## Rollback

The original image tag is retained. Confirm `turtlequant:local` still points
to the baseline ID above. From `/opt/turtlequant-app`:

```bash
docker compose -p turtlequant-app -f docker-compose.yml up -d --no-build --no-deps turtlequant-bot
```

This restores the original image and environment without the capture
override. Preserve all state and collected evidence. Recheck health,
uniqueness, positions and order mode. No database migration, state rewrite,
source reset, broad Compose shutdown or volume deletion is required.

Future Compose commands must include the staged override to retain this
telemetry release; a default `up` alone restores the baseline. No default
checkout/image change or Git push is included in this deployment.
