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

The shadow bot's state is always in `/opt/turtlequant/state`. The live bot, when deployed, keeps its own in `/opt/turtlequant/state/live-state`.

**Trading mode is set only on the command line:** `--shadow` (the Compose default), `--paper` or `--live --i-accept-live-risk`. There is no `PAPER_TRADE` environment variable.

---

## Secrets

Secrets live in `.env` on the VPS only. Never commit it.

Compose reads only the variables named in `docker-compose.yml`. **Don't use `env_file:`** with a shared `.env`, or unrelated secrets will leak into the container.

| Variable | Needed for |
|----------|------------|
| `DERIBIT_CLIENT_ID`, `DERIBIT_CLIENT_SECRET` | Optional. Public IV works without them. They are sent in a POST body, never in a URL |
| `POLYMARKET_PRIVATE_KEY` | `--live` only. Passed by `docker-compose.live.yml` |
| `POLYMARKET_API_*` | `--live` only. Derived from the private key at startup if not set |
| `POLYMARKET_SIGNATURE_TYPE`, `POLYMARKET_FUNDER` | Proxy wallets (`SIGNATURE_TYPE=1`) |

The live service and `migrate_pusd_v2.py` read only the `POLYMARKET_*` names. The shared `.env` on helios also holds the old `crypto_up_or_down` bot's `PRIVATE_KEY`, `CLOB_API_*` and `FUNDER_ADDRESS`, and accepting those aliases would let live sign with that wallet. Read-only helpers such as `reconcile_nav.py` still fall back to them, so set `POLYMARKET_PRIVATE_KEY` before running them against the live wallet.

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

### Live vs Shadow row

Below **Account**, this row always shows both bots and ignores the **Strategy** selector. Live is an execution test, so the question is whether live's decisions and fills match shadow's, not whether it makes money.

| Panel | Watch for |
|-------|-----------|
| Return Since Start | The two lines drifting apart. Both are marked equity over starting capital, so $100 and $1,000 compare directly |
| Wallet vs Books (Live) | The wallet (`turtlequant_wallet_collateral_usd`, read by the live bot each scan) tracking NAV minus open cost. Value awaiting redemption isn't spendable until redeemed by hand |
| Decision Funnel per Hour | Shadow solid, live dashed, one color per stage. Up to *executable* the two should agree; a gap means live decided differently |
| Size-Floor Outcomes per Hour | Why live skipped or resized entries shadow took: rounded up to the 5-share minimum, below it, or capped by asset or wallet |
| Order Integrity | Anything above zero: outstanding order intents, failed orders, broker failures |
| Live Fills vs Shadow | Each recent live fill beside shadow's fill on the same market and side within 24h: entry prices, price and slippage gaps, and size as a share of each bot's NAV |

The funnel counts come from `scan_summary` (`turtlequant_scan_outcomes_total{outcome}`, a fixed set of counters). They're per scan, so a market that stays a candidate counts on every scan.

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
| Exporter unreachable | `up{job="turtlequant"} == 0` for 5m: the exporter is dead or hung |
| Exporter restarted | The exporter process restarted in the last 15m |
| Failed orders | More than 3 failed orders in 15m |
| Entries halted | `entries_halted == 1` for 15m. The `reason` label is one of `broker_failures`, `data_errors`, `stale_data`, `drawdown`, `daily_loss` or `halt_file`. Drawdown gating uses marked equity (realized NAV plus open positions at the executable bid) |
| NAV drawdown | Closed-trade/realized equity-curve drawdown above 15% for 15m; this intentionally excludes open positions |

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
| `turtlequant-grafana-exporter` | `:8004/metrics` answers 200 |

Both containers run with `init: true` so a healthcheck killed at its timeout is reaped, and both checks use the shell rather than starting python. `tests/test_compose.py` enforces this. On 2026-10-05 helios CPU steal jumped to ~90%. The python healthcheck then timed out every 30s, used nearly all of the exporter container's CPU, and left 1,122 zombies behind python running as PID 1.

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
| `turtlequant_synthetic_book_ratio` | Low. A high value means many candidates had no real CLOB book. No mode fills on a synthetic book, so those entries are skipped (`synthetic_book_skipped` in `scan_summary`) and those exits hold (`synthetic_book_exits_held`) |
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
2. Marked-equity drawdown of 15% from the high-water mark. After tripping, it reopens only below 14% drawdown to prevent threshold flapping
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
- The exact gate inputs are exported as `turtlequant_marked_equity_usd`, `turtlequant_risk_high_water_usd`, and `turtlequant_risk_drawdown_pct`

**Fail closed.** If the bot can't write position, risk or history state, it stops.

---

## State Files and Growth

All of these are in the state directory.

| File | Contents | Growth |
|------|----------|--------|
| `turtlequant-positions.json` | NAV, open positions, re-entry cooldowns | Bounded |
| `turtlequant-risk.json` | High-water mark, breaker, entry-gate state; live adds the last CLOB pUSD balance it read (`wallet_collateral_usd`, `wallet_balance_at`) | Bounded |
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
| `evaluate_models.py` | Scores the legacy model, the smile model and the market mid against resolved markets, using `market_marks` snapshots. Reports Brier score, reliability, and net P&L per share for trades that clear the threshold. `--option-types european_put,range` scores only the "less than" and "between" markets; `checkpoint_report.py` keeps its decision tables on `european,barrier,barrier_down` |
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

`scripts/generate_performance_page.py` builds two pages and writes them straight into the site's web root.

<https://billybitcoin.cloud/turtlequant/> shows:
- **Live portfolio** (when `<state-dir>/live-state` has a positions file): marked NAV, realized and unrealized P&L, wallet cash from `turtlequant-risk.json`, entry-halt status, open positions and live trades. `--live-state-dir` points elsewhere; `--no-live` leaves it off.
- **Shadow track record**: equity curve, drawdown, return distribution, trade metrics, open positions and the trade log. It covers the current run only. Runs archived by a state reset are not published.

<https://billybitcoin.cloud/turtlequant/strategy/> (`--strategy-dir`, default `<output dir>/strategy`) explains the strategy and has a pricing lab. Its rules come from the newest startup banner in the live bot's log (rotations included; the bot's defaults if none is found). The lab's markets come from the last `market_marks` record in shadow's `turtlequant-marks.jsonl`. All its behavior is in `strategy/strategy.js`, an external file, so editing it needs no CSP change.

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

**CSP hash.** The main page's one inline script is allowed by its sha256 hash in the site's CSP, which lives in `deploy/nginx/snippets/security-headers.conf` in the billybitcoin.cloud repo. Data changes don't affect the hash, but **any edit to `_PAGE_SCRIPT` in `src/turtlequant/performance_page.py` does.** After such an edit:

1. Regenerate the page from an empty `--state-dir`, so no numbers get committed.
2. Copy it into the site repo as `turtlequant/index.html`.
3. Run that repo's `tools/gen-csp-hashes.py`.
4. Update the snippet and redeploy.

If you skip this, the browser silently blocks the script and the charts disappear.

---

## Live Trading (CLOB v2)

> **Before the first live order:**
> - `--live` needs `--i-accept-live-risk` and `--max-order-usd` (`MAX_ORDER_USD`, 10 in `docker-compose.live.yml`), or the bot exits.
> - Each buy is capped at 98% of the wallet's CLOB pUSD balance. If the balance can't be read, the entry is skipped.
> - Resolved positions aren't redeemed on-chain automatically. Live positions stay `pending_redemption` until you redeem them by hand, and that money isn't spendable until then.
> - The live bot (`docker-compose.live.yml`) stops after 3 failed starts instead of crash-looping.
> - Every limit scales with NAV, including the daily loss halt (`MAX_DAILY_LOSS_PCT`, 5% of the high-water mark by default). The exception is `MAX_ORDER_USD`, which is in dollars: change it whenever you change `STARTING_NAV`.
> - Polymarket rejects orders under the market's minimum (5 shares on crypto price markets). A buy Kelly sizes below the minimum is rounded up to it if Kelly wants at least half of it, and skipped otherwise; every NAV cap still applies after rounding. A position under the minimum is never sold: it is held to resolution (`[EXIT_BELOW_MIN]` in the log). Shadow follows the same rules.

TurtleQuant uses `py-clob-client-v2` with pUSD collateral. USDC.e in the wallet plus V1 exchange approvals is **not** enough.

### 1. Fund the wallet

Use a **dedicated signing EOA** that holds nothing else, with `POLYMARKET_SIGNATURE_TYPE=0` and no `POLYMARKET_FUNDER`. It needs:
- USDC.e for trading
- A few dollars of POL for gas (wrapping, approvals and manual redemptions; CLOB orders themselves are gasless)

Proxy and Magic wallets (`SIGNATURE_TYPE=1`) aren't supported yet. `migrate_pusd_v2.py` refuses them, because it would sign as the EOA and leave the proxy unwrapped and unapproved.

### 2. Migrate collateral (USDC.e → pUSD) and set approvals

```bash
cd /opt/turtlequant-app
set -a && source .env && set +a

uv run scripts/migrate_pusd_v2.py --dry-run          # review the plan
uv run scripts/migrate_pusd_v2.py                    # wrap all USDC.e and set v2 approvals
# uv run scripts/migrate_pusd_v2.py --wrap-usd 50    # partial wrap
```

The script sets two approvals for each of the CTF Exchange, the Neg Risk CTF Exchange and the Neg Risk Adapter:
- **pUSD `approve`**, so buys can pay.
- **ConditionalTokens `setApprovalForAll`**, so sells can deliver outcome tokens. Without it every SELL is rejected.

Afterwards, the CLOB balance should match the wrapped pUSD shown in the script's output, and a re-run should show every approval as ✓.

The default RPC is `polygon-bor-rpc.publicnode.com` (`polygon-rpc.com` now answers 401). Set `POLYGON_RPC_URL` to use another.

### 3. Set up API credentials

If only the private key is set, credentials are derived at startup. To save them explicitly:

```bash
uv run scripts/derive_clob_api_creds.py >> .env
```

### 4. Smoke test

Run the live service in the foreground with a $2 order cap. It uses exactly the deployed strategy settings, so the first order is one the real bot would place:

```bash
mkdir -p /opt/turtlequant/state/live-state
docker compose -f docker-compose.live.yml build
docker compose -f docker-compose.live.yml run --rm -e MAX_ORDER_USD=2 turtlequant-bot-live
```

The live state starts at `STARTING_NAV` (100) when it has no positions file, and the bot writes that file at startup so the exporter can read it before the first fill. Entries are rare (a few a week in shadow), so leave it running until one fills, then press Ctrl-C. Then confirm:
- `[WALLET_CAP]` or `[ENTRY` lines show the wallet balance was read.
- The `order` event in `turtlequant-history.jsonl` has `status: matched`, an `order_id` and fill amounts.
- `uv run python scripts/order_intents.py --state-dir /opt/turtlequant/state/live-state list` shows nothing outstanding.
- `uv run scripts/reconcile_nav.py --positions /opt/turtlequant/state/live-state/turtlequant-positions.json` agrees with the wallet.

With `EXIT_RULE=ev`, the position usually holds to resolution, so the smoke test doesn't exercise a sell. Watch the first live exit.

### 5. Run the live container

`docker-compose.live.yml` is its own Compose project, `turtlequant-live`. It runs beside shadow, which keeps going as the comparison for live fills. **Use it alone, never as an override on `docker-compose.yml`.**

```bash
docker compose -f docker-compose.live.yml up -d --build
```

| Container | Reads | Metrics |
|-----------|-------|---------|
| `turtlequant-bot-live` | `/opt/turtlequant/state/live-state` | — |
| `turtlequant-grafana-exporter-live` | `live-state`, and shadow's state at `/opt/turtlequant/shadow-state`, both read-only | `strategy="turtlequant-live"`, host port 8005 |

Both extend their shadow services, so they share the image, init, logging and healthchecks. Live changes only its state, wallet secrets, `STARTING_NAV` (100) and metrics label. `STARTING_NAV` applies only at the first start in an empty `live-state`: the bot then writes the positions file, and NAV comes from it afterwards.

The live exporter reads shadow's history (`PAIR_STATE_DIR`) only to pair each live fill with shadow's for the **Live vs Shadow** row. It gets no wallet secrets and never calls the wallet. The wallet balance it exports is the one the live bot reads once per scan and saves in `turtlequant-risk.json`. If a read fails, the last good balance stays and `turtlequant_wallet_balance_age_sec` grows.

Then add a scrape job to `/opt/monitoring/prometheus.yml` and reload Prometheus:

```yaml
  - job_name: "turtlequant-live"
    static_configs:
      - targets: ["turtlequant-grafana-exporter-live:8004"]
```

Add it only when live is deployed: the operational alerts cover both jobs, so a job with no exporter fires TurtleQuantExporterUnreachable. Shadow-soak quality alerts watch shadow only. In Grafana, the **Strategy** selector switches the dashboard between Shadow and Live, except for the [Live vs Shadow row](#live-vs-shadow-row), which always shows both.

To stop live without touching shadow:

```bash
docker compose -f docker-compose.live.yml down
```

### 6. Reconcile NAV

The `nav` in `turtlequant-positions.json` is bookkeeping, not your wallet balance. This compares it with the CLOB pUSD balance plus open positions marked at the bid:

```bash
uv run scripts/reconcile_nav.py --positions /opt/turtlequant/state/live-state/turtlequant-positions.json
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
