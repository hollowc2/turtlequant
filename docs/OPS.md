# TurtleQuant — Phase 0 Operations

## VPS layout

```bash
# From this repo directory (/opt/polymarket/app/turtlequant)
./scripts/setup-vps.sh
cp .env.example .env   # fill secrets on the VPS only
docker compose up -d --build
```

| Path | Purpose |
|------|---------|
| `/opt/turtlequant/state` | Positions, history, bot log (bind-mounted) |
| `/opt/turtlequant/state/live-state` | Separate live positions, history, bot log |
| `/opt/turtlequant/data` | Calibration / auxiliary data |
| `/opt/polymarket/app/turtlequant` | Deploy source (compose file lives here) |

`monitoring_net` must exist before `docker compose up` (created by `/opt/monitoring` stack or `setup-vps.sh`).

## Secrets

Copy `.env.example` → `.env` on the VPS. Never commit `.env`.

Compose interpolates only the variables listed in `docker-compose.yml` from `.env` — do **not** use `env_file:` with a shared monorepo `.env` or unrelated secrets will leak into the container.

| Variable | Required when |
|----------|----------------|
| `DERIBIT_CLIENT_ID`, `DERIBIT_CLIENT_SECRET` | Always recommended (rate limits) |
| `POLYMARKET_PRIVATE_KEY` (or `PRIVATE_KEY`) | `--live` / `--shadow` with authenticated CLOB |
| `POLYMARKET_API_*` | Optional — derived from private key at runtime if omitted |
| `POLYMARKET_SIGNATURE_TYPE`, `POLYMARKET_FUNDER` | Proxy wallet setups (`SIGNATURE_TYPE=1`) |

Legacy aliases from `crypto_up_or_down` are supported: `PRIVATE_KEY`, `CLOB_API_*`, `FUNDER_ADDRESS`, `SIGNATURE_TYPE`.

Trading mode is **CLI only**: `--shadow` (compose default), `--paper`, or `--live --i-accept-live-risk`. There is no `PAPER_TRADE` env var on the bot.

## Live trading prep (CLOB v2)

TurtleQuant uses `py-clob-client-v2` (pUSD collateral). Wallet USDC.e + V1 exchange approvals are **not** sufficient.

### 1. Fund wallet

- Polygon EOA with USDC.e (and MATIC for gas).
- For signing EOA wallets: set `POLYMARKET_SIGNATURE_TYPE=0` and leave `POLYMARKET_FUNDER` unset/empty.
- For proxy/Magic wallets: set `POLYMARKET_SIGNATURE_TYPE=1` and `POLYMARKET_FUNDER` to the address that holds funds.

### 2. Migrate collateral (USDC.e → pUSD)

```bash
cd /opt/polymarket/app/turtlequant
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
| NAV drawdown | `current_drawdown_pct > 15%` for 15m |
| Exporter down | `exporter_scrape_success == 0` for 5m |
| No shadow quotes | no `shadow_quote` events for 10m |
| Ask erases edge | `ask_erased_edge_ratio > 25%` for 10m |
| Synthetic books | `synthetic_book_ratio > 20%` for 15m |
| Parser hit rate low | `parser_hit_rate < 75%` for 15m |
| Realized-vol fallback high | `realized_vol_fallback_ratio > 30%` for 15m |

## Phase 1 shadow soak

Before live trading, run TurtleQuant in shadow mode long enough to cover normal market discovery, pricing, and order-book paths:

```bash
cd /opt/polymarket/app/turtlequant
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

## Healthchecks

- `turtlequant-bot`: log file mtime < 180s (`LOG_FILE` / `turtlequant-bot.log`)
- `turtlequant-grafana-exporter`: HTTP `:8004/metrics`

## Rollback

```bash
cd /opt/polymarket/app/turtlequant
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
