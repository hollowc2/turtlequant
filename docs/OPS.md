# TurtleQuant — Phase 0 Operations

## VPS layout

```bash
# First-time setup: a standalone checkout of this repo
git clone https://github.com/hollowc2/turtlequant.git /opt/turtlequant-app
cd /opt/turtlequant-app
./scripts/setup-vps.sh
cp .env.example .env   # fill secrets on the VPS only
docker compose up -d --build

# Updating
cd /opt/turtlequant-app && git pull --ff-only && docker compose up -d --build
```

| Path | Purpose |
|------|---------|
| `/opt/turtlequant/state` | Positions, history, bot log (bind-mounted) |
| `/opt/turtlequant/state/live-state` | Separate live positions, history, bot log |
| `/opt/turtlequant/data` | Calibration / auxiliary data |
| `/opt/turtlequant-app` | Deploy source: bot, exporter and performance-page cron (compose file lives here) |
| `/opt/polymarket/state` | Other Polymarket bots; not TurtleQuant runtime state |

`/opt/polymarket/app/turtlequant` is the retired monorepo copy the bot ran from until
2026-09-24; it is no longer deployed. Old repo-local samples under
`/opt/polymarket/app/crypto_up_or_down/state/turtlequant` are not the active TurtleQuant state. Check `/opt/turtlequant/state` for the running
bot unless the live override is explicitly in use.

`monitoring_net` must exist before `docker compose up` (created by `/opt/monitoring` stack or `setup-vps.sh`).

## Secrets

Copy `.env.example` → `.env` on the VPS. Never commit `.env`.

Compose interpolates only the variables listed in `docker-compose.yml` from `.env` — do **not** use `env_file:` with a shared monorepo `.env` or unrelated secrets will leak into the container.

| Variable | Required when |
|----------|----------------|
| `DERIBIT_CLIENT_ID`, `DERIBIT_CLIENT_SECRET` | Optional — public IV data works without them; sent in a POST body, never a URL |
| `POLYMARKET_PRIVATE_KEY` (or `PRIVATE_KEY`) | `--live` only (passed by `docker-compose.live.yml`) |
| `POLYMARKET_API_*` | `--live` only — derived from the private key at startup if omitted |
| `POLYMARKET_SIGNATURE_TYPE`, `POLYMARKET_FUNDER` | Proxy wallet setups (`SIGNATURE_TYPE=1`) |

Shadow and paper mode never read the wallet key: books, market info and fee rates are
public CLOB endpoints, so the base `docker-compose.yml` passes no Polymarket secrets.
The exporter runs from the same lockfile-built image and mounts only `src/` and
`scripts/` read-only, so the checkout's `.env` never enters a container.

Legacy aliases from `crypto_up_or_down` are supported: `PRIVATE_KEY`, `CLOB_API_*`, `FUNDER_ADDRESS`, `SIGNATURE_TYPE`.

Trading mode is **CLI only**: `--shadow` (compose default), `--paper`, or `--live --i-accept-live-risk`. There is no `PAPER_TRADE` env var on the bot.

## Live trading prep (CLOB v2)

> **Live trading is hard-disabled in code** (`scripts/turtlequant_bot.py` exits with
> "Live trading is disabled pending supervised broker acceptance"). The steps below
> describe the path once that gate is lifted after supervised broker acceptance. They
> cannot run today, and the live compose override stops after 3 failed starts rather
> than crash-looping. On-chain redemption of resolved positions is not implemented:
> live positions stay `pending_redemption` until redeemed by hand.

TurtleQuant uses `py-clob-client-v2` (pUSD collateral). Wallet USDC.e + V1 exchange approvals are **not** sufficient.

### 1. Fund wallet

- Polygon EOA with USDC.e (and MATIC for gas).
- For signing EOA wallets: set `POLYMARKET_SIGNATURE_TYPE=0` and leave `POLYMARKET_FUNDER` unset/empty.
- For proxy/Magic wallets: set `POLYMARKET_SIGNATURE_TYPE=1` and `POLYMARKET_FUNDER` to the address that holds funds.

### 2. Migrate collateral (USDC.e → pUSD)

```bash
cd /opt/turtlequant-app
set -a && source .env && set +a

uv run scripts/migrate_pusd_v2.py --dry-run   # review plan
uv run scripts/migrate_pusd_v2.py             # wrap all USDC.e + v2 approvals
# uv run scripts/migrate_pusd_v2.py --wrap-usd 50   # partial wrap
```

After migration, CLOB balance should match wrapped pUSD (check script output).

### 3. API credentials

Derived automatically when only `PRIVATE_KEY` is set. To persist explicit keys:

```bash
uv run scripts/derive_clob_api_creds.py >> .env
```

### 4. One-shot live smoke test

```bash
cp /opt/turtlequant/state/turtlequant-positions.json \
   /opt/turtlequant/state/turtlequant-positions.json.bak-$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p /opt/turtlequant/state/live-state
printf '{"nav":50.0,"total_pnl":0.0,"positions":[]}\n' \
  > /opt/turtlequant/state/live-state/turtlequant-positions.json
printf '[]\n' > /opt/turtlequant/state/live-state/turtlequant-history.json

uv run python scripts/turtlequant_bot.py \
  --live --i-accept-live-risk \
  --state-dir /opt/turtlequant/state/live-state \
  --asset btc \
  --starting-nav 50 \
  --entry-threshold 0.10
```

Verify: order in `turtlequant-bot.log`, fill fields on position, exit on test size.

### Order-intent journal

Every live order is journaled in `turtlequant-order-intents.sqlite3` before it is sent:
`pending` → `submitted` → `reconciled` | `failed` | `cancelled`. An order that never
reached the broker is marked `failed` at once. An ambiguous answer (timeout, unconfirmed
status) stays outstanding. While any intent is outstanding, new entries halt (entry-gate
reason `unreconciled_orders`), and that market gets no further orders. The bot retries
reconciliation every 30s and applies a confirmed fill to positions. An outstanding intent
also blocks startup. Check the order at the broker, then:

```bash
uv run python scripts/order_intents.py --state-dir /opt/turtlequant/state/live-state list
uv run python scripts/order_intents.py --state-dir /opt/turtlequant/state/live-state resolve <id> failed "no order at broker"
```

Only mark an intent `failed`/`cancelled` if nothing filled. For a filled order, restart
and let reconciliation apply it.

### 5. NAV reconciliation

Internal `nav` in `turtlequant-positions.json` is bookkeeping, not wallet balance.

```bash
uv run scripts/reconcile_nav.py
```

Compares file NAV to CLOB pUSD balance + open position bid marks. Investigate if drift > 5% of NAV.

For the long-running live container, use the live override so shadow bookkeeping in `/opt/turtlequant/state/turtlequant-positions.json` is not inherited:

```bash
docker compose -f docker-compose.yml -f docker-compose.live.yml up -d --build turtlequant-bot turtlequant-grafana-exporter
```

## Observability

- **Grafana dashboard**: provisioned from `grafana/dashboards/turtlequant.json` via `/opt/monitoring` (provider `turtlequant`).
- **Prometheus scrape**: job `turtlequant` → `turtlequant-grafana-exporter:8004` on `monitoring_net` (already configured in `/opt/monitoring/prometheus.yml`).
- **Alerts**: centrally owned by `/opt/monitoring/prometheus-alerts/turtlequant.yml`.

  ```bash
  docker compose -f /opt/monitoring/docker-compose.yml exec prometheus \
    promtool check rules /etc/prometheus/alerts/turtlequant.yml
  ```

  Native Alertmanager Discord routing runs in `/opt/monitoring`. Operational
  TurtleQuant alerts use its dedicated webhook; shadow-soak diagnostics remain
  visible in Prometheus/Grafana without Discord notifications.

| Alert | Condition |
|-------|-----------|
| Bot stale | `turtlequant_bot_log_age_sec > 180` for 3m |
| No scans | log age > 120s for 5m |
| Failed orders | >3 `failed_order` events in 15m |
| Entries halted | `turtlequant_entries_halted == 1` for 15m (label `reason`: broker_failures, data_errors, stale_data, drawdown, daily_loss, halt_file) |
| NAV drawdown | `current_drawdown_pct > 15%` for 15m |
| Exporter down | `exporter_scrape_success == 0` for 5m |
| No shadow quotes | no `shadow_quote` events for 10m |
| Ask erases edge | `ask_erased_edge_ratio > 25%` for 10m |
| Synthetic books | `synthetic_book_ratio > 20%` for 15m |
| Parser hit rate low | `parser_hit_rate < 75%` for 15m |
| Realized-vol fallback high | `realized_vol_fallback_ratio > 30%` for 15m |

## Ops scripts

| Script | Purpose |
|--------|---------|
| `scripts/monitor_turtlequant.py` | Live terminal dashboard: open positions with reprice edge, recent events, closed-position summary |
| `scripts/calibrate_turtlequant.py` | Validates `probability_engine` model calibration against historical OHLCV (Brier score / RMSE) — not live trading |
| `scripts/migrate_pusd_v2.py` | One-time USDC.e → pUSD collateral migration (see [Live trading prep](#live-trading-prep-clob-v2)) |
| `scripts/derive_clob_api_creds.py` | Derives CLOB API credentials from the wallet private key |
| `scripts/reconcile_nav.py` | Compares bookkeeping NAV to actual CLOB balance (see [NAV reconciliation](#5-nav-reconciliation)) |

```bash
# Live dashboard, one-shot or auto-refreshing
uv run --script scripts/monitor_turtlequant.py
uv run --script scripts/monitor_turtlequant.py --live --interval 15
uv run --script scripts/monitor_turtlequant.py --state-dir /opt/turtlequant/state/live-state

# Calibration check before raising live risk or after a probability_engine change
uv run python scripts/calibrate_turtlequant.py --asset btc --years 3
uv run python scripts/calibrate_turtlequant.py --asset eth --years 5 --plot
```

Deploy threshold for calibration: Brier score < 0.25 **and** calibration RMSE < 0.05.

## Public performance page

`scripts/generate_performance_page.py` renders <https://billybitcoin.cloud/turtlequant/>
(equity curve, drawdown, return distribution, trade metrics, open positions, trade log)
from the shadow state and writes it straight into the site's web root. It runs hourly
from `billy`'s crontab, out of the same `/opt/turtlequant-app` checkout the bot runs from:

```bash
cd /opt/turtlequant-app
crontab -l 2>/dev/null | grep -v generate_performance_page | cat - scripts/performance_page.cron | crontab -

# one-off run / preview elsewhere
uv run python scripts/generate_performance_page.py
uv run python scripts/generate_performance_page.py --output /tmp/turtlequant/index.html
```

Output goes to `/opt/turtlequant-app/performance-page.log`, outside the web root
(`/opt/turtlequant/state` is root-owned, so the log cannot live there).

`--mode` only changes the page's badge (`shadow` / `paper` / `live`); it must match
`--state-dir`. To publish live results instead, point at `/opt/turtlequant/state/live-state`
with `--mode live`.

The page's one inline script is allowed by its sha256 hash in the site's CSP
(`deploy/nginx/snippets/security-headers.conf` in the billybitcoin.cloud repo). Data
changes never affect that hash, but **any edit to `_PAGE_SCRIPT` in
`src/turtlequant/performance_page.py` does**: regenerate the page, copy it into the site
repo as `turtlequant/index.html` (render it from an empty `--state-dir` so no numbers
are committed), run that repo's `tools/gen-csp-hashes.py`, update the snippet and
redeploy it. Otherwise the browser silently refuses the script and the charts vanish.

## Phase 1 shadow soak

Before live trading, run TurtleQuant in shadow mode long enough to cover normal market discovery, pricing, and order-book paths:

```bash
cd /opt/turtlequant-app
docker compose up -d --build turtlequant-bot turtlequant-grafana-exporter
docker compose logs -f turtlequant-bot
```

During the soak, review the `Phase 1 Shadow Soak` row in Grafana and the Prometheus alerts above. The exporter is expected to expose:

| Metric | Review target |
|--------|---------------|
| `turtlequant_shadow_quotes_total` | steadily increases while scan loop finds executable candidates |
| `turtlequant_ask_erased_edge_ratio` | stays low; high values mean executable ask removes modeled edge |
| `turtlequant_synthetic_book_ratio` | stays low; high values mean fill modeling depends on synthetic books |
| `turtlequant_parser_hit_rate` | remains high enough that discovery is not dominated by unparsed markets |
| `turtlequant_realized_vol_fallback_ratio` | remains low; high values mean Deribit IV coverage is weak |
| `turtlequant_order_book_source_total` | confirms real CLOB book usage versus synthetic fallback |
| `turtlequant_vol_source_total` | confirms Deribit IV usage versus realized-vol fallback |

Promotion gate:

1. No critical alerts firing for the soak window.
2. `shadow_quotes_total` is increasing during active scan periods.
3. Synthetic-book and realized-vol fallback ratios are understood and acceptable for the current market set.
4. Parser misses are reviewed before raising live risk.
5. Failed-order and stale-exporter alerts are quiet.

## State files and growth

| File (in the state dir) | Contents | Growth |
|-------------------------|----------|--------|
| `turtlequant-positions.json` | NAV, open positions, re-entry cooldowns | Bounded |
| `turtlequant-risk.json` | High-water mark, breaker, entry-gate state | Bounded |
| `turtlequant-history.jsonl` | Trade and ops events (`open`, `close`, `order`, `failed_order`, `entry_gate`, …), fsynced | One line per trade action |
| `turtlequant-diagnostics.jsonl[.1-.3]` | Per-scan `scan_summary`, `signal_evaluation`, `shadow_quote` | Rotated at 64 MiB × 3 backups (`DIAGNOSTICS_MAX_BYTES`, `DIAGNOSTICS_BACKUP_COUNT`) |
| `unclassified_markets.jsonl` | Each unparsed question once, for parser review | Bounded by distinct questions |
| `turtlequant-bot.log[.1-.5]` | Bot log | Rotated at 10 MiB |

The exporter tails the history and diagnostics files incrementally. It keeps running
aggregates rather than every event, and it handles partially written lines, truncation
and rotation. Its shadow-soak ratios cover the retained diagnostics window.

**One-off migration:** history written before 2026-09-25 also holds the diagnostics.
With the bot stopped, split them out:

```bash
docker compose stop turtlequant-bot
uv run python scripts/split_history.py --state-dir /opt/turtlequant/state --dry-run
uv run python scripts/split_history.py --state-dir /opt/turtlequant/state
docker compose start turtlequant-bot
```

This keeps trade events in place and gzips the old diagnostics to
`turtlequant-diagnostics-archive-<ts>.jsonl.gz` (or pass `--drop-diagnostics`). The
original stays as a `.bak-<ts>` hard link; delete it once the dashboard and performance
page look right.

## Entry gate and circuit breaker

New entries are blocked (exits always run) by, in order: a `HALT` file in the state dir,
a 15% drawdown from the high-water mark, the daily loss limit, the broker breaker, the
data gate, and stale market data.

- **Broker breaker:** counts only orders that reached the broker and failed or came
  back ambiguous. Three in a row halts entries for 30 minutes; then one retry is
  allowed, and another failure halts again. A filled order resets it. A sell with no
  bids is a liquidity miss (`[EXIT_UNFILLED]`), not a broker failure.
- **Data gate:** per-market or reprice exceptions never touch the broker counter. The
  gate closes only when at least 3 markets, and at least half the markets attempted,
  failed in the last scan. It reopens after one clean scan.
- Gate changes are logged once (`[ENTRY_HALTED]` / `[ENTRY_RESUMED]`), written as an
  `entry_gate` history event and persisted to `turtlequant-risk.json` (`entry_halt`),
  which the exporter turns into `turtlequant_entries_halted{reason}` and the
  `TurtleQuantEntriesHalted` alert. The dashboard shows it as **Entry Gate**.
- A failure to write positions, risk or history state stops the bot (fail closed).

## Healthchecks

- `turtlequant-bot`: log file mtime < 180s (`LOG_FILE` / `turtlequant-bot.log`)
- `turtlequant-grafana-exporter`: HTTP `:8004/metrics`

## Rollback

```bash
cd /opt/turtlequant-app
docker compose down

# Restore last known-good positions snapshot
cp /opt/turtlequant/state/turtlequant-positions.json.bak-YYYYMMDD \
   /opt/turtlequant/state/turtlequant-positions.json

docker compose up -d
```

Take a timestamped backup before live deploys:

```bash
cp /opt/turtlequant/state/turtlequant-positions.json \
   /opt/turtlequant/state/turtlequant-positions.json.bak-$(date -u +%Y%m%dT%H%M%SZ)
```

## Credential rotation (Deribit leak)

Plaintext Deribit keys were removed from `crypto_up_or_down/docs/turtlequant_plan.md` but exist in git history (`a8513e1`). **Rotate keys in the Deribit console**, update VPS `.env`, then optionally purge history:

```bash
# After rotation — rewrite history (coordinate with team; force-push required)
git filter-repo --path crypto_up_or_down/docs/turtlequant_plan.md --invert-paths
# or use BFG Repo-Cleaner on the leaked strings
```
