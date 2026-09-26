<img src="data/images/turtlequant.png" align="right" height="64"/>

# TurtleQuant

TurtleQuant trades crypto price markets on [Polymarket](https://polymarket.com) by pricing them as options.

A market like _"Will BTC be above $75,000 on March 30?"_ is a digital option. TurtleQuant prices it with Black-Scholes and Deribit implied volatility. When the model's probability beats the executable market price by enough, it trades the difference.

It covers BTC and ETH threshold markets, from daily expiries out to year-end. That includes "above X on date", "reach / dip to X in month" and "by December 31" markets. By default it runs in Docker in **shadow mode**, where fills are simulated against real order books.

---

## Quickstart

```bash
./scripts/setup-vps.sh     # create /opt/turtlequant/{state,data} and monitoring_net
cp .env.example .env       # add secrets (never commit)
docker compose up -d       # start in shadow mode
```

For secrets, monitoring, alerts, healthchecks and rollback, see [docs/OPS.md](docs/OPS.md).

---

## How It Works

```
Gamma API → Scanner → Parser → Probability Engine → Execution → Position Manager
                                      ↑
                          Vol Surface (Deribit, Binance fallback)
```

### 1. Discover markets
Every 60 seconds, the scanner reads Polymarket's "Crypto Prices" events and skips "Up or Down" events. That is about 1,400 open markets in two requests. It keeps markets that meet all of these:

- More than 4 hours to expiry
- Over $5k of liquidity
- A bid-ask spread of 3¢ or less

The `scanner_funnel` field in each scan's log shows how many markets each filter removed.

### 2. Parse questions
The parser turns each market's question into a contract: `(asset, strike, expiry, type)`.

| Type | Example |
|------|---------|
| European | "Will BTC be above $75k on March 30?" |
| Barrier up | "Will BTC reach $100k before March?" |
| Barrier down | "Will ETH fall to $2,000 before expiry?" |

### 3. Build the vol surface
The bot takes mark IV from Deribit and interpolates it across moneyness (log-linear) and expiry (√T-linear). If Deribit has no matching instruments, it falls back to 30-day realized vol from Binance.

### 4. Price
**Default (`legacy`) model:**
```
d₂ = (ln(S₀/K) + (r − σ²/2)T) / (σ√T)
P(digital) = N(d₂)
P(barrier) = N(d₊) + (K/S₀)^(2μ/σ²) · N(d₋)      [reflection principle]
```

**Smile model (`--pricing-model smile`):**
- Uses Deribit's futures price as a zero-drift forward.
- Reads IV from the sticky-strike smile.
- Adds a skew correction to digitals:
  ```
  P(S_T > K) = N(d₂) − vega · ∂σ/∂K        vega = F·φ(d₁)·√T
  ```
- Prices touch markets with the reflection formula, then applies the same skew correction as an approximation.
- Skips markets that have no smile or no forward.

Every `signal_evaluation` log line records both models' prices, whichever one is trading.

`--max-iv-age-secs N` blocks new entries when Deribit IV is more than N seconds old.

### 5. Enter and size
```
edge = model_probability − executable_price
```
The bot enters when the edge clears the threshold after crossing the ask. It sizes with fractional Kelly, capped by the NAV limits below.

| Setting | Default | Flag |
|---------|---------|------|
| Entry edge | 5% | `--entry-threshold` |
| Kelly fraction | 25% | `--kelly-fraction` |
| Per-market cap | 10% of NAV | `--max-per-market-pct` |
| Per-expiry cap | 15% of NAV | `--max-per-expiry-pct` |
| Total exposure | 40% of NAV | `--max-total-exposure-pct` |
| Net delta per asset | off in code, **20% in Compose** | `--max-asset-delta-pct` |
| Gross exposure per asset | off | `--max-asset-exposure-pct` |
| Entry price band | 0.02–0.98 | `--min-entry-price`, `--max-entry-price` |
| Re-entry cooldown | 2h | `--reentry-cooldown-hours` |
| Kelly shrink toward mid | 1.0 (raw model) | `--kelly-shrink` |

**How the delta cap is measured:**
- Net dollar delta is `Σ shares · ∂p/∂S · S`, found by bumping spot ±1% under the active model. Divide it by 100 to get P&L per 1% spot move.
- Near-the-money, short-dated digitals carry large delta. On 2026-09-25, $53 of BTC positions held −$1,765 of delta.
- Each scan writes per-asset gross and delta to `turtlequant-risk.json`, exported as `turtlequant_asset_delta_usd{asset}`.

**NO side (`--sides yes,no`, off by default):** When the model is below the market, the bot buys the NO token. It prices NO as `1 − P(YES)` against NO's own order book. Positions, fees, exits and reports are all tracked in the held token's terms.

Every flag can also be set with an upper-case environment variable of the same name, such as `MAX_PER_EXPIRY_PCT`. To list them all:
```bash
python scripts/turtlequant_bot.py --help
```

### 6. Exit
Positions are saved to JSON and survive restarts. By default, a position closes when any of these happens:

- **Edge reversed:** the model probability drops below the market price.
- **Edge decayed:** the edge falls below 40% of the entry edge (`--edge-decay-ratio`).
- **Time cleanup:** 6h or less remain (`--cleanup-hours`) and the edge is 5% or less (`--cleanup-edge`).
- **Resolved:** paper and shadow positions settle at the market's payout.

**EV exit (`--exit-rule ev`):** replaces the first three rules. The bot sells only when the bid, after the taker fee, beats the model value by `--exit-margin` (default 1pp). Otherwise it holds to resolution. It also holds whenever vol comes from anything other than live Deribit IV.

`scripts/exit_counterfactual.py` compares each past exit with what holding would have paid.

If a held market drops out of the scan, the bot keeps using the last quote it saw for exit decisions instead of the entry price.

### 7. Execute

| Mode | Behavior |
|------|----------|
| `--dry-run` | Logs what it would buy or exit. Writes no files. |
| `--paper` | Simulates fills against real bid/ask depth. |
| `--shadow` | Paper mode plus a `shadow_quote` record for every candidate. **This is the deployed mode.** |
| `--live --i-accept-live-risk` | **Disabled in code** until live order handling is tested under supervision. Once enabled, it sends fill-and-kill (FAK) orders through `py_clob_client_v2` and records and reconciles actual and partial fills. |

Paper and shadow modes use only public endpoints and never load a wallet key.

Live mode needs these environment variables:
- `POLYMARKET_PRIVATE_KEY`
- `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`
- `POLYMARKET_SIGNATURE_TYPE` and `POLYMARKET_FUNDER` (optional, for proxy wallets)

---

## Data Sources

| Source | Used for |
|--------|----------|
| Polymarket Gamma API and CLOB | Market discovery, prices and order books |
| Deribit | Implied volatility and futures forwards |
| Binance | Spot price and realized vol |

If Binance returns a geo-block (HTTP 451), the bot falls back to OKX. To use a single exchange instead, set `DATA_SOURCE=okx|bybit|gateio`.

---

## State and Logs

Runtime state lives outside the repo:

| Path | Contents |
|------|----------|
| `/opt/turtlequant-app` | Checkout of this repo, including the Compose files |
| `/opt/turtlequant/state` | Shadow-mode positions, history and the bot log |
| `/opt/turtlequant/state/live-state` | Live-mode state, used with `docker-compose.live.yml` |

Key files:
- **`turtlequant-history.jsonl`** is the trade ledger: `open`, `close`, `order` and `failed_order` events with fills and fees.
- **`turtlequant-diagnostics.jsonl`** holds per-scan diagnostics: `scan_summary`, `signal_evaluation` and `shadow_quote`. It rotates when it reaches a size limit.

See [docs/OPS.md](docs/OPS.md#state-files-and-growth) for file growth and retention.

---

## Architecture

`src/turtlequant/trader.py` runs two passes on each tick:
1. **Reprice:** settle resolved positions and apply exit rules.
2. **Scan:** update held positions and make new entries.

All of its dependencies are injected. `scripts/turtlequant_bot.py` only parses arguments, wires up components and runs the loop. `tests/test_trader.py` tests the loop against a fake scanner, CLOB and spot feed.

---

## Monitoring

The Grafana exporter publishes shadow-soak quality metrics:

| Metric |
|--------|
| `turtlequant_ask_erased_edge_ratio` |
| `turtlequant_synthetic_book_ratio` |
| `turtlequant_parser_hit_rate` |
| `turtlequant_realized_vol_fallback_ratio` |
| `turtlequant_shadow_quotes_total` |
| `turtlequant_order_book_source_total` |
| `turtlequant_vol_source_total` |

Before enabling live trading, run a shadow soak. Then review the **Phase 1 Shadow Soak** row in Grafana and the Prometheus alerts.

---

## Calibration

A 5-year backtest on BTC and ETH gives a Brier score of **0.178–0.199**. Lower is better, and 0.25 is a coin flip.

**Caveat:** the backtest (`scripts/calibrate_turtlequant.py`) uses 30-day realized vol, but the live bot uses Deribit implied vol. The score therefore describes a related model, not the one that trades. It also doesn't show whether trades that pass the edge threshold actually win.

`scripts/evaluate_models.py` gives a closer test. It scores both models against real resolved markets, using quote snapshots the bot takes every 15 minutes. See review item 7 in [docs/REVIEW-2026-09-24.md](docs/REVIEW-2026-09-24.md).
