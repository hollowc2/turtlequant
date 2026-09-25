<img src="data/images/turtlequant.png" align="right" height="64"/>

# TurtleQuant

![TurtleQuant](data/images/turtlequant.png)

A probabilistic trading system for cryptocurrency prediction markets on [Polymarket](https://polymarket.com). **TurtleQuant** continuously scans markets, prices them using options math, and trades the edge between model probability and market price.

---

## What It Does

Polymarket offers binary outcome markets like _"Will BTC be above $75,000 by March 30?"_ These are structurally equivalent to digital options. TurtleQuant prices them using standard options theory and trades when the market is mispriced.

**TurtleQuant** — BTC/ETH price-threshold markets, from daily expiries out to year-end ("above X on <date>", "reach / dip to X in <month>", "… by December 31"). Black-Scholes digital and barrier pricing with Deribit implied volatility.

It runs as a Docker service in shadow mode by default: orders stay simulated, but each signal records the executable bid/ask snapshot used for fill modeling.

Phase 1 shadow-soak monitoring expects exporter metrics for quote/source quality:
`turtlequant_ask_erased_edge_ratio`, `turtlequant_synthetic_book_ratio`,
`turtlequant_parser_hit_rate`, `turtlequant_realized_vol_fallback_ratio`,
`turtlequant_shadow_quotes_total`, `turtlequant_order_book_source_total`, and
`turtlequant_vol_source_total`.

---

## How It Works

### 1. Market Discovery
Every scan reads the Gamma API's `/events` tagged "Crypto Prices" (tag 1312), excluding "Up or Down" events (tag 102127). That is about 150 events and 1,400 open markets in two requests. Markets are then filtered by time to expiry (>4h), asset, liquidity (>$5k) and an absolute bid-ask spread (≤3¢, `--max-spread`). Each scan's `scan_summary` records how many markets every filter rejected (`scanner_funnel`). Which expiries exist depends on what Polymarket lists: daily and weekly series are always open, and monthly and year-end series appear as they are created.

### 2. Market Parsing
Classifies each question using regex into a structured contract: `(asset, strike, expiry, type)`.

| Type | Example Question |
|------|-----------------|
| European | "Will BTC be above $75k by March 30?" |
| Barrier | "Will BTC reach $100k before March?" |
| Barrier Down | "Will ETH fall to $2,000 before expiry?" |

### 3. Volatility Surface
Fetches mark IV from Deribit, interpolates across moneyness (log-linear) and expiry (√T-linear). Falls back to 30-day realized vol from Binance if Deribit has no matching instruments.

### 4. Pricing

**TurtleQuant — Black-Scholes:**
```
d₂ = (ln(S₀/K) + (r − σ²/2)T) / (σ√T)
P(digital) = N(d₂)
P(barrier) = N(d₊) + (K/S₀)^(2μ/σ²) × N(d₋)   [reflection principle]
```

**Smile model (`--pricing-model smile`, off by default):** the forward is Deribit's futures price for the expiry, with zero drift. IV is looked up on the forward-moneyness smile (sticky strike). Digitals add the skew term the flat formula omits:
```
P(S_T > K) = N(d₂) − vega · ∂σ/∂K        vega = F·φ(d₁)·√T
```
Touch markets use the flat reflection price with the forward's drift, scaled by the same skew correction of the matching terminal probability (an approximation). Every `signal_evaluation` logs both models (`model_prob_legacy`, `model_prob_smile`), whichever one trades. `--max-iv-age-secs N` (off by default) ignores Deribit IV older than N seconds and blocks entries until it refreshes. In smile mode, markets without a smile or forward are not entered.

### 5. Edge Detection & Sizing
```
edge = model_probability − executable_yes_price
```
Enter when `edge > threshold` after crossing the executable ask. Size via fractional Kelly (25% default in code and compose) capped by per-market, per-expiry, and total NAV limits.

| Entry edge | ≥5% (`--entry-threshold`) |
| Per-market NAV | 10% (`--max-per-market-pct`) |
| Per-expiry NAV | 15% (`--max-per-expiry-pct`) |
| Total exposure | 40% (`--max-total-exposure-pct`) |
| Entry price band | 0.02–0.98 (`--min/max-entry-price`) |
| Re-entry cooldown | 2h (`--reentry-cooldown-hours`) |
| Scan interval | 60s |

NO side (`--sides yes,no`, off by default): when the model is below the market, the bot buys the NO token, using its own CLOB book (which mirrors YES, with NO bid = 1 − YES ask) and P(NO) = 1 − P(YES). Positions, exits, settlement, fees, the intent journal, delta, the exporter (`outcome` label) and the performance page (Side column) all work in the held token's terms.

Portfolio caps (all off by default): `--max-asset-exposure-pct` limits gross USD per asset. `--max-asset-delta-pct` limits net dollar delta per asset, Σ shares·∂p/∂S·S from a ±1% spot bump under the active pricing model. Divide dollar delta by 100 to get the P&L per 1% spot move. Short-dated near-the-money digitals carry a lot of it: on 2026-09-25, $53 of BTC positions held −$1,765. `--kelly-shrink w` sizes on `w·model + (1−w)·mid` while still gating on the raw model's edge. Each scan persists per-asset gross and delta to `turtlequant-risk.json`, exported as `turtlequant_asset_delta_usd{asset}`, so you can choose cap values from real numbers before turning them on.

Every knob also reads an env var of the same name in upper case (e.g. `MAX_PER_EXPIRY_PCT`). `python scripts/turtlequant_bot.py --help` lists them all.

### 6. Position Management & Exit
State persists to JSON across restarts. Positions close on three triggers:
- **Edge reversed** — model prob < market price
- **Edge decayed**: current edge drops below 40% of entry edge (`--edge-decay-ratio`)
- **Time cleanup**: <= 6h remaining (`--cleanup-hours`) and edge <= 5% (`--cleanup-edge`)
- **Resolved**: paper/shadow positions settle at the Gamma payout once the market resolves

`--exit-rule ev` (off by default) replaces the first three triggers. It sells only when the bid, net of the taker fee, beats the model's value by `--exit-margin` (default 1pp); otherwise it holds to resolution. It also holds while the vol source is degraded (anything but live Deribit IV). `scripts/exit_counterfactual.py` compares every past exit with what holding to resolution would have paid.

The bot also persists the last observed YES quote per open position so exits do not fall back to entry price if a market drops out of the active scan set.

### 7. Execution
TurtleQuant supports four runtime modes:

| Mode | Behavior |
|------|----------|
| `--dry-run` | Evaluates entries and exits against the current state and logs `[DRY_RUN]` would-buy / would-exit lines. Writes nothing: no position, risk, history or corpus files. |
| `--paper` | Simulated fills using executable bid/ask depth. |
| `--shadow` | Same simulated fills, plus a `shadow_quote` diagnostic for every candidate, which feeds the shadow-soak metrics and alerts. This is the deployed mode. |
| `--live --i-accept-live-risk` | **Currently hard-disabled in code** pending supervised broker acceptance. When enabled: FAK market orders through `py_clob_client_v2`, with actual/partial fills journaled and reconciled. |

Paper and shadow mode read only public CLOB endpoints and never load a wallet key. Live mode expects `POLYMARKET_PRIVATE_KEY` plus API credentials (`POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`) in the environment. Optional `POLYMARKET_SIGNATURE_TYPE` and `POLYMARKET_FUNDER` are passed through for proxy-wallet setups.

---

## Architecture

```
Gamma API → MarketScanner → MarketParser → ProbabilityEngine → ExecutionClient → PositionManager
                                              ↑           ↑
                                         VolSurface
                                          (Deribit)
                                              ↑
                                         Binance OHLCV
```

`src/turtlequant/trader.py` runs one reprice pass (settle, exit) and one scan pass (update held positions, enter) with every dependency injected. `scripts/turtlequant_bot.py` only parses arguments, wires components and calls the two passes on a timer. `tests/test_trader.py` drives the loop against a fake scanner, CLOB and spot source.

---

## Data Sources

| Source | Use |
|--------|-----|
| Polymarket Gamma API | Market prices & discovery |
| Deribit Options API | Implied volatility surface |
| Binance (OKX on geo-block) | Spot price and realized vol |

Spot and realized vol come from Binance. Only a 451 geo-block response falls back, and only to OKX. `DATA_SOURCE=okx|bybit|gateio` pins a single exchange instead.

---

## Quickstart

```bash
./scripts/setup-vps.sh        # create /opt/turtlequant/{state,data} + monitoring_net
cp .env.example .env          # configure secrets on the VPS (never commit)
docker compose up -d          # runs TurtleQuant in shadow mode (--shadow)
```

Runtime state is outside this repo:

| Path | Meaning |
|------|---------|
| `/opt/turtlequant-app` | Source and compose files (standalone checkout of this repo) |
| `/opt/turtlequant/state` | Active shadow-mode positions, history, and bot log |
| `/opt/turtlequant/state/live-state` | Separate live-mode state when `docker-compose.live.yml` is used |
| `/opt/polymarket/state` | Other `crypto_up_or_down` bot state, not TurtleQuant runtime state |

State files persist in `/opt/turtlequant/state`. `turtlequant-history.jsonl` is the trade ledger (`open`, `close`, `order`, `failed_order` events with fills, fees and partial-fill fields). Per-scan diagnostics (`scan_summary`, `signal_evaluation` with bid/ask depth, `shadow_quote`) go to the size-rotated `turtlequant-diagnostics.jsonl`. See [docs/OPS.md](docs/OPS.md#state-files-and-growth).

See [docs/OPS.md](docs/OPS.md) for secrets, Grafana/Prometheus wiring, alerts, healthchecks, and rollback.

For Phase 1 promotion, run a shadow soak first and review the Grafana `Phase 1 Shadow Soak` row plus Prometheus alerts before enabling live execution.

---

## Calibration

Backtested on 5 years of BTC and ETH data. Brier loss: **0.178–0.199** (lower is better; 0.25 = random).

Caveat: `scripts/evaluate_models.py` scores the legacy and smile models against real resolved markets and prices, from snapshots the bot takes every 15 minutes. `scripts/calibrate_turtlequant.py` prices with 30-day realized vol, while the live bot prices with Deribit implied vol. The score therefore describes a related model, not the one that trades, and it does not test whether "model − market ≥ threshold" trades win. See review item 7 in [docs/REVIEW-2026-09-24.md](docs/REVIEW-2026-09-24.md).
