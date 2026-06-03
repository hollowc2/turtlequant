<img src="data/images/turlequant_small.png" align="right" height="64"/>

# TurtleQuant

![TurtleQuant](data/images/turtlequant.png)

A probabilistic trading system for cryptocurrency prediction markets on [Polymarket](https://polymarket.com). **TurtleQuant** continuously scans markets, prices them using options math, and trades the edge between model probability and market price.

---

## What It Does

Polymarket offers binary outcome markets like _"Will BTC be above $75,000 by March 30?"_ These are structurally equivalent to digital options. TurtleQuant prices them using standard options theory and trades when the market is mispriced.

**TurtleQuant** — Long-dated markets (weeks–months). Black-Scholes pricing with Deribit implied volatility.

It runs as a Docker service in shadow mode by default: orders stay simulated, but each signal records the executable bid/ask snapshot used for fill modeling.

---

## How It Works

### 1. Market Discovery
Polls the Polymarket Gamma API every scan cycle. Filters ~500 markets by liquidity (>$5k), spread (<3%), and time-to-expiry (>4h).

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

### 5. Edge Detection & Sizing
```
edge = model_probability − executable_yes_price
```
Enter when `edge > threshold` after crossing the executable ask. Size via fractional Kelly (25% default in code and compose) capped by per-market, per-expiry, and total NAV limits.

| Entry edge | ≥5% |
| Per-market NAV | 10% |
| Total exposure | 40% |
| Scan interval | 60s |

### 6. Position Management & Exit
State persists to JSON across restarts. Positions close on three triggers:
- **Edge reversed** — model prob < market price
- **Edge decayed** — current edge drops below 40% of entry edge
- **Time cleanup** — <= 6h remaining and edge <= 5%

The bot also persists the last observed YES quote per open position so exits do not fall back to entry price if a market drops out of the active scan set.

### 7. Execution
TurtleQuant supports four runtime modes:

| Mode | Behavior |
|------|----------|
| `--dry-run` | Signals only; no order or position state changes. |
| `--paper` | Simulated fills using executable bid/ask depth. |
| `--shadow` | Same as paper, plus explicit CLOB/Gamma quote and order events for live-readiness review. |
| `--live --i-accept-live-risk` | Sends FAK market orders through `py_clob_client_v2` and records actual/partial fills. |

Live mode expects `POLYMARKET_PRIVATE_KEY` plus API credentials (`POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`) in the environment. Optional `POLYMARKET_SIGNATURE_TYPE` and `POLYMARKET_FUNDER` are passed through for proxy-wallet setups.

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

---

## Data Sources

| Source | Use |
|--------|-----|
| Polymarket Gamma API | Market prices & discovery |
| Deribit Options API | Implied volatility surface |
| Binance / Bybit / OKX / Gate.io | Spot price and realized vol |

Binance data falls back through Bybit → OKX → Gate.io for geo-resilience.

---

## Quickstart

```bash
./scripts/setup-vps.sh        # create /opt/turtlequant/{state,data} + monitoring_net
cp .env.example .env          # configure secrets on the VPS (never commit)
docker compose up -d          # runs TurtleQuant in shadow mode (--shadow)
```

State files (`*-positions.json`, `*-history.json`) persist in `/opt/turtlequant/state`. The history file includes `order`, `failed_order`, and `shadow_quote` events with bid/ask snapshots, slippage, fill ratio inputs, and partial-fill fields.

See [docs/OPS.md](docs/OPS.md) for secrets, Grafana/Prometheus wiring, alerts, healthchecks, and rollback.

---

## Calibration

Backtested on 5 years of BTC and ETH data. Brier loss: **0.178–0.199** (lower is better; 0.25 = random).
