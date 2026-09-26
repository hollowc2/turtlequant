# TurtleQuant Operations

How to deploy, monitor and recover TurtleQuant on the VPS.

**Contents:**
[Deploy](#deploy) · [Secrets](#secrets) · [Monitoring](#monitoring) · [Shadow soak](#phase-1-shadow-soak) · [Entry gate](#entry-gate-and-circuit-breaker) · [State files](#state-files-and-growth) · [Rollback](#backup-and-rollback) · [Scripts](#ops-scripts) · [Performance page](#public-performance-page) · [Live trading](#live-trading-clob-v2) · [Credential rotation](#credential-rotation-deribit-leak)

---

## Deploy

```bash
# First time
git clone https://github.com/hollowc2/turtlequant.git /opt/turtlequant-app
cd /opt/turtlequant-app
./scripts/setup-vps.sh
cp .env.example .env   # fill in secrets on the VPS only
docker compose up -d --build

# Update
cd /opt/turtlequant-app && git pull --ff-only && docker compose up -d --build
```

`monitoring_net` must exist before `docker compose up`. Either the `/opt/monitoring` stack or `setup-vps.sh` creates it.

| Path | Contents |
|------|----------|
| `/opt/turtlequant-app` | Deployed checkout: bot, exporter, performance-page cron and Compose files |
| `/opt/turtlequant/state` | Shadow positions, history and bot log (bind-mounted) |
| `/opt/turtlequant/state/live-state` | Live positions, history and bot log (kept separate) |
| `/opt/turtlequant/data` | Calibration and auxiliary data |
| `/opt/polymarket/state` | Other Polymarket bots. Not TurtleQuant state |

**Retired paths:**
- `/opt/polymarket/app/turtlequant` is the old monorepo copy. The bot ran from it until 2026-09-24 and it is no longer deployed.
- `/opt/polymarket/app/crypto_up_or_down/state/turtlequant` holds old samples, not live state.

The running bot's state is always in `/opt/turtlequant/state`, unless the live override is in use.

**Trading mode is set only on the command line:** `--shadow` (the Compose default), `--paper` or `--live --i-accept-live-risk`. There is no `PAPER_TRADE` environment variable.

---

## Secrets

Secrets live in `.env` on the VPS only. Never commit it.

Compose reads only the variables named in `docker-compose.yml`. **Don't use `env_file:`** with a shared `.env`, or unrelated secrets will leak into the container.

| Variable | Needed for |
|----------|------------|
| `DERIBIT_CLIENT_ID`, `DERIBIT_CLIENT_SECRET` | Optional. Public IV works without them. They are sent in a POST body, never in a URL |
| `POLYMARKET_PRIVATE_KEY` (or `PRIVATE_KEY`) | `--live` only. Passed by `docker-compose.live.yml` |
| `POLYMARKET_API_*` | `--live` only. Derived from the private key at startup if not set |
| `POLYMARKET_SIGNATURE_TYPE`, `POLYMARKET_FUNDER` | Proxy wallets (`SIGNATURE_TYPE=1`) |

The legacy `crypto_up_or_down` names also work: `PRIVATE_KEY`, `CLOB_API_*`, `FUNDER_ADDRESS` and `SIGNATURE_TYPE`.

**Shadow and paper modes never read the wallet key.** Order books, market info and fee rates all come from public CLOB endpoints, so the base `docker-compose.yml` passes no Polymarket secrets.

The exporter is built from the same lockfile as the bot. It mounts only `src/` and `scripts/`, both read-only, so `.env` never enters a container.

---

## Monitoring

| Component | Details |
|-----------|---------|
| Grafana dashboard | `grafana/dashboards/turtlequant.json`, provisioned by `/opt/monitoring` (provider `turtlequant`) |
| Prometheus scrape | Job `turtlequant` → `turtlequant-grafana-exporter:8004` on `monitoring_net` |
| Alert rules | `/opt/monitoring/prometheus-alerts/turtlequant.yml` (repo copy: `monitoring/prometheus-alerts.yml`) |
| Notifications | Alertmanager in `/opt/monitoring` sends operational alerts to Discord. Shadow-soak alerts appear only in Prometheus and Grafana |

To validate the alert rules:
```bash
docker compose -f /opt/monitoring/docker-compose.yml exec prometheus \
  promtool check rules /etc/prometheus/alerts/turtlequant.yml
```

### Alerts

**Operational:**

| Alert | Fires when |
|-------|------------|
| Bot stale | Bot log older than 180s for 3m |
| No scans | Bot log older than 120s for 5m |
| Exporter down | `exporter_scrape_success == 0` for 5m |
| Exporter restarted | The exporter process restarted in the last 15m |
| Failed orders | More than 3 failed orders in 15m |
| Entries halted | `entries_halted == 1` for 15m. The `reason` label is one of `broker_failures`, `data_errors`, `stale_data`, `drawdown`, `daily_loss` or `halt_file` |
| NAV drawdown | Drawdown above 15% for 15m |

**Shadow-soak quality:**

| Alert | Fires when |
|-------|------------|
| No shadow quotes | Candidates were found but no `shadow_quote` was produced for 10m |
| Ask erases edge | Recent `ask_erased_edge_ratio` above 25% for 10m |
| Synthetic books high | `synthetic_book_ratio` above 20% for 15m |
| Only synthetic books | No real CLOB books were used for 15m |
| Parser hit rate low | Recent `parser_hit_rate` below 75% for 15m |
| Realized-vol fallback high | `realized_vol_fallback_ratio` above 30% for 15m |
| Only realized vol | No Deribit IV was used for 15m |

### Healthchecks

| Container | Healthy when |
|-----------|--------------|
| `turtlequant-bot` | The log file (`LOG_FILE`, default `turtlequant-bot.log`) was modified in the last 180s |
| `turtlequant-grafana-exporter` | `:8004/metrics` responds |

---

## Phase 1 Shadow Soak

Run in shadow mode long enough to cover market discovery, pricing and order-book handling before going live.

```bash
cd /opt/turtlequant-app
docker compose up -d --build turtlequant-bot turtlequant-grafana-exporter
docker compose logs -f turtlequant-bot
```

Watch the **Phase 1 Shadow Soak** row in Grafana:

| Metric | What healthy looks like |
|--------|-------------------------|
| `turtlequant_shadow_quotes_total` | Rises steadily while scans find candidates |
| `turtlequant_ask_erased_edge_ratio` | Low. A high value means crossing the ask wipes out the edge |
| `turtlequant_synthetic_book_ratio` | Low. A high value means fills are modeled on synthetic books |
| `turtlequant_parser_hit_rate` | High. Most discovered markets parse |
| `turtlequant_realized_vol_fallback_ratio` | Low. A high value means Deribit IV coverage is weak |
| `turtlequant_order_book_source_total` | Mostly real CLOB books |
| `turtlequant_vol_source_total` | Mostly Deribit IV |

**Promotion gate.** All of these must hold:
1. No critical alerts fired during the soak.
2. `shadow_quotes_total` rises during active scans.
3. The synthetic-book and realized-vol fallback ratios are understood and acceptable.
4. Parser misses have been reviewed.
5. Failed-order and stale-exporter alerts are quiet.

---

## Entry Gate and Circuit Breaker

These checks block **new entries** but never exits. They run in this order:

1. A `HALT` file in the state directory
2. Drawdown of 15% from the high-water mark
3. The daily loss limit
4. The broker breaker
5. The data gate
6. Stale market data

**Broker breaker:**
- It counts only orders that reached the broker and then failed or returned an ambiguous result.
- Three in a row halt entries for 30 minutes. After that one retry is allowed, and another failure halts again.
- A filled order resets the count.
- A sell with no bids is a liquidity miss (`[EXIT_UNFILLED]`), not a broker failure.

**Data gate:**
- Per-market and reprice errors never count toward the broker breaker.
- The gate closes only when the last scan had at least 3 failed markets and at least half of the markets it tried failed.
- One clean scan reopens it.

**Reporting.** Each gate change is recorded in four places:
- Logged once as `[ENTRY_HALTED]` or `[ENTRY_RESUMED]`
- Written as an `entry_gate` history event
- Saved in `turtlequant-risk.json` (`entry_halt`)
- Exported as `turtlequant_entries_halted{reason}`, which drives the Entries halted alert and the dashboard's **Entry Gate** panel

**Fail closed.** If the bot can't write position, risk or history state, it stops.

---

## State Files and Growth

All of these are in the state directory.

| File | Contents | Growth |
|------|----------|--------|
| `turtlequant-positions.json` | NAV, open positions, re-entry cooldowns | Bounded |
| `turtlequant-risk.json` | High-water mark, breaker, entry-gate state | Bounded |
| `turtlequant-history.jsonl` | Trade and ops events (`open`, `close`, `order`, `failed_order`, `entry_gate`, …), fsynced | One line per trade action |
| `turtlequant-diagnostics.jsonl[.1-.3]` | Per-scan `scan_summary`, `signal_evaluation`, `shadow_quote` | Rotated at 64 MiB, 3 backups (`DIAGNOSTICS_MAX_BYTES`, `DIAGNOSTICS_BACKUP_COUNT`) |
| `turtlequant-marks.jsonl[.1-.4]` | Every 15 min: each priced market's bid/ask plus legacy and smile probabilities (`market_marks`), used by `evaluate_models.py` | Rotated at 64 MiB, 4 backups, about 70 days at ~4.5 MB/day (`MARKS_MAX_BYTES`, `MARKS_BACKUP_COUNT`) |
| `unclassified_markets.jsonl` | Each unparsed question, once, for parser review | Bounded by distinct questions |
| `turtlequant-bot.log[.1-.5]` | Bot log | Rotated at 10 MiB |

The exporter reads the history and diagnostics files incrementally.
- It keeps running totals, not every event.
- It copes with partly written lines, truncation and rotation.
- Its shadow-soak ratios cover only the diagnostics that are still kept.

### One-off migration: split old history

History written before 2026-09-25 also contains diagnostics. To split them out, stop the bot first:

```bash
docker compose stop turtlequant-bot
uv run python scripts/split_history.py --state-dir /opt/turtlequant/state --dry-run
uv run python scripts/split_history.py --state-dir /opt/turtlequant/state
docker compose start turtlequant-bot
```

Trade events stay in place. The old diagnostics are gzipped to `turtlequant-diagnostics-archive-<ts>.jsonl.gz`, or deleted if you pass `--drop-diagnostics`.

The original file is kept as a `.bak-<ts>` hard link. Delete it once the dashboard and performance page look right.

---

## Backup and Rollback

Before any live deploy, take a timestamped backup:

```bash
cp /opt/turtlequant/state/turtlequant-positions.json \
   /opt/turtlequant/state/turtlequant-positions.json.bak-$(date -u +%Y%m%dT%H%M%SZ)
```

To roll back:

```bash
cd /opt/turtlequant-app
docker compose down

# Restore the last known-good positions snapshot
cp /opt/turtlequant/state/turtlequant-positions.json.bak-YYYYMMDD \
   /opt/turtlequant/state/turtlequant-positions.json

docker compose up -d
```

---

## Ops Scripts

| Script | Purpose |
|--------|---------|
| `monitor_turtlequant.py` | Terminal dashboard: open positions with current edge, recent events, closed-position summary |
| `evaluate_models.py` | Scores the legacy model, the smile model and the market mid against resolved markets, using `market_marks` snapshots. Reports Brier score, reliability, and net P&L per share for trades that clear the threshold |
| `calibrate_turtlequant.py` | Checks `probability_engine` calibration on simulated contracts using historical OHLCV and realized vol. **It does not test the model that trades** |
| `exit_counterfactual.py` | Compares each past exit with what holding to resolution would have paid |
| `order_intents.py` | Lists and resolves live order intents (see [Order-intent journal](#order-intent-journal)) |
| `split_history.py` | One-off split of diagnostics out of history (see [above](#one-off-migration-split-old-history)) |
| `migrate_pusd_v2.py` | One-time USDC.e → pUSD migration (see [Live trading](#live-trading-clob-v2)) |
| `derive_clob_api_creds.py` | Derives CLOB API credentials from the wallet key |
| `reconcile_nav.py` | Compares bookkeeping NAV with the actual CLOB balance |

```bash
# Dashboard: one-shot, auto-refresh, or live state
uv run --script scripts/monitor_turtlequant.py
uv run --script scripts/monitor_turtlequant.py --live --interval 15
uv run --script scripts/monitor_turtlequant.py --state-dir /opt/turtlequant/state/live-state

# Calibration
uv run python scripts/calibrate_turtlequant.py --asset btc --years 3
uv run python scripts/calibrate_turtlequant.py --asset eth --years 5 --plot
```

**Use `evaluate_models.py` to judge the model, not the calibration gate.** The realized-vol gate (Brier < 0.25, RMSE < 0.05) says little about the model that actually trades. Before switching `--pricing-model` or raising risk, wait until a few hundred snapshotted markets have resolved, then run:

```bash
uv run python scripts/evaluate_models.py --state-dir /opt/turtlequant/state
```

Check two things:
- Each model's Brier score beats the market mid's.
- Its edge trades are net positive after fees.

---

## Public Performance Page

`scripts/generate_performance_page.py` builds <https://billybitcoin.cloud/turtlequant/> from the shadow state and writes it straight into the site's web root. The page shows:
- Equity curve and drawdown
- Return distribution and trade metrics
- Open positions and the trade log

It runs hourly from `billy`'s crontab, out of `/opt/turtlequant-app`.

```bash
cd /opt/turtlequant-app
crontab -l 2>/dev/null | grep -v generate_performance_page | cat - scripts/performance_page.cron | crontab -

# One-off run, or preview elsewhere
uv run python scripts/generate_performance_page.py
uv run python scripts/generate_performance_page.py --output /tmp/turtlequant/index.html
```

**Log location.** The log is `/opt/turtlequant-app/performance-page.log`. It lives there because `/opt/turtlequant/state` is owned by root.

**`--mode`** changes only the page badge (`shadow`, `paper` or `live`), and it must match `--state-dir`. To publish live results, use `--state-dir /opt/turtlequant/state/live-state --mode live`.

**CSP hash.** The page's one inline script is allowed by its sha256 hash in the site's CSP, which lives in `deploy/nginx/snippets/security-headers.conf` in the billybitcoin.cloud repo. Data changes don't affect the hash, but **any edit to `_PAGE_SCRIPT` in `src/turtlequant/performance_page.py` does.** After such an edit:

1. Regenerate the page from an empty `--state-dir`, so no numbers get committed.
2. Copy it into the site repo as `turtlequant/index.html`.
3. Run that repo's `tools/gen-csp-hashes.py`.
4. Update the snippet and redeploy.

If you skip this, the browser silently blocks the script and the charts disappear.

---

## Live Trading (CLOB v2)

> **Live trading is disabled in code.** `scripts/turtlequant_bot.py` exits with "Live trading is disabled pending supervised broker acceptance". The steps below apply once that gate is removed. Until then:
> - None of these steps can be run.
> - The live Compose override stops after 3 failed starts instead of crash-looping.
>
> Resolved positions aren't redeemed on-chain automatically. Live positions stay `pending_redemption` until you redeem them by hand.

TurtleQuant uses `py-clob-client-v2` with pUSD collateral. USDC.e in the wallet plus V1 exchange approvals is **not** enough.

### 1. Fund the wallet

You need a Polygon wallet holding USDC.e, plus MATIC for gas. Then set:

| Wallet type | `POLYMARKET_SIGNATURE_TYPE` | `POLYMARKET_FUNDER` |
|-------------|-----------------------------|---------------------|
| Signing EOA | `0` | unset or empty |
| Proxy or Magic wallet | `1` | Address that holds the funds |

### 2. Migrate collateral (USDC.e → pUSD)

```bash
cd /opt/turtlequant-app
set -a && source .env && set +a

uv run scripts/migrate_pusd_v2.py --dry-run          # review the plan
uv run scripts/migrate_pusd_v2.py                    # wrap all USDC.e and set v2 approvals
# uv run scripts/migrate_pusd_v2.py --wrap-usd 50    # partial wrap
```

Afterwards, the CLOB balance should match the wrapped pUSD shown in the script's output.

### 3. Set up API credentials

If only the private key is set, credentials are derived at startup. To save them explicitly:

```bash
uv run scripts/derive_clob_api_creds.py >> .env
```

### 4. Smoke test

Back up shadow state, create a fresh live state, then run a small one-shot:

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

Then confirm three things:
- The order appears in `turtlequant-bot.log`.
- The position has fill fields.
- The test position exits.

### 5. Run the live container

Use the live override so it doesn't inherit shadow bookkeeping from `/opt/turtlequant/state`:

```bash
docker compose -f docker-compose.yml -f docker-compose.live.yml up -d --build turtlequant-bot turtlequant-grafana-exporter
```

### 6. Reconcile NAV

The `nav` in `turtlequant-positions.json` is bookkeeping, not your wallet balance. This compares it with the CLOB pUSD balance plus open positions marked at the bid:

```bash
uv run scripts/reconcile_nav.py
```

Investigate if the difference is more than 5% of NAV.

### Order-intent journal

Every live order is written to `turtlequant-order-intents.sqlite3` before it's sent. Each intent moves through these states:

```
pending → submitted → reconciled | failed | cancelled
```

- An order that never reached the broker is marked `failed` immediately.
- An ambiguous result, such as a timeout or unconfirmed status, stays **outstanding**.

While any intent is outstanding:
- New entries halt, with gate reason `unreconciled_orders`.
- The affected market gets no more orders.
- Reconciliation retries every 30s and applies any confirmed fill.
- **The bot won't start.**

To clear one, first check the order at the broker, then:

```bash
uv run python scripts/order_intents.py --state-dir /opt/turtlequant/state/live-state list
uv run python scripts/order_intents.py --state-dir /opt/turtlequant/state/live-state resolve <id> failed "no order at broker"
```

Mark an intent `failed` or `cancelled` only if nothing filled. If it did fill, restart the bot and let reconciliation apply the fill.

---

## Credential Rotation (Deribit Leak)

Plaintext Deribit keys were once committed to the monorepo in `crypto_up_or_down/docs/turtlequant_plan.md`. History was checked on 2026-09-24.

**This repo** (`hollowc2/turtlequant`, linked from the public performance page) is clean:
- No revision contains `turtlequant_plan.md`.
- A scan of every revision for credential patterns found only test placeholders.

**The monorepo's history appears to have been rewritten:**
- The file's history now shows `REDACTED_…` placeholders.
- The previously cited commit `a8513e1` no longer exists.
- Forks or clones made before the rewrite may still hold the real keys.

**Rotating the keys in the Deribit console is the only real fix,** since anyone may have copied them. The bot doesn't need them, because public IV data works without authentication. If you keep the keys, they are only ever sent in a POST body.
