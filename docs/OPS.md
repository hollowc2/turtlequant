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
| `/opt/turtlequant/data` | Calibration / auxiliary data |
| `/opt/polymarket/app/turtlequant` | Deploy source (compose file lives here) |

`monitoring_net` must exist before `docker compose up` (created by `/opt/monitoring` stack or `setup-vps.sh`).

## Secrets

Copy `.env.example` → `.env` on the VPS. Never commit `.env`.

Compose interpolates only the variables listed in `docker-compose.yml` from `.env` — do **not** use `env_file:` with a shared monorepo `.env` or unrelated secrets will leak into the container.

| Variable | Required when |
|----------|----------------|
| `DERIBIT_CLIENT_ID`, `DERIBIT_CLIENT_SECRET` | Always recommended (rate limits) |
| `POLYMARKET_PRIVATE_KEY` + API trio | `--live` only |
| `POLYMARKET_SIGNATURE_TYPE`, `POLYMARKET_FUNDER` | Proxy wallet setups |

Trading mode is **CLI only**: `--shadow` (compose default), `--paper`, or `--live --i-accept-live-risk`. There is no `PAPER_TRADE` env var on the bot.

## Observability

- **Grafana dashboard**: provisioned from `grafana/dashboards/turtlequant.json` via `/opt/monitoring` (provider `turtlequant`).
- **Prometheus scrape**: job `turtlequant` → `turtlequant-grafana-exporter:8004` on `monitoring_net` (already configured in `/opt/monitoring/prometheus.yml`).
- **Alerts**: copy or symlink rules into the monitoring stack:

  ```bash
  sudo cp monitoring/prometheus-alerts.yml /opt/monitoring/prometheus-alerts/turtlequant.yml
  # Ensure prometheus.yml includes:
  #   rule_files:
  #     - /etc/prometheus/alerts/*.yml
  docker compose -f /opt/monitoring/docker-compose.yml exec prometheus kill -HUP 1
  ```

  Alertmanager + Discord bridge run in `/opt/monitoring` (`alertmanager`, `alertmanager-discord`).
  Set `DISCORD_WEBHOOK_URL` in `/opt/monitoring/.env` (see `.env.example`), then:

  ```bash
  cd /opt/monitoring && docker compose up -d alertmanager alertmanager-discord
  docker compose restart prometheus
  ```

  TurtleQuant alerts route to the shared `#ops` Discord channel (same webhook as butterflyguy).

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
