<img src="data/images/turlequant_small.png" align="right" height="64"/>

# TurtleQuant

![TurtleQuant](data/images/turtlequant.png)

A probabilistic trading system for cryptocurrency prediction markets on [Polymarket](https://polymarket.com). Two independent bots — **TurtleQuant** and **SlowQuant** — continuously scan markets, price them using options math, and trade the edge between model probability and market price.

---

## What It Does

Polymarket offers binary outcome markets like _"Will BTC be above $75,000 by March 30?"_ These are structurally equivalent to digital options. TurtleQuant prices them using standard options theory and trades when the market is mispriced.

**TurtleQuant** — Long-dated markets (weeks–months). Black-Scholes pricing with Deribit implied volatility.

**SlowQuant** — Short-dated markets (hours–days). Merton jump-diffusion Monte Carlo with regime-adaptive thresholds.

Both bots run as Docker services in paper-trading mode by default.

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

**SlowQuant — Merton Jump-Diffusion (50k Monte Carlo paths):**
```
dS/S = (r − λκ)dt + σ dW + J dN
```
Jump parameters (λ, μⱼ, σⱼ) are recalibrated every 12 cycles from 30-day hourly Binance returns.

### 5. Edge Detection & Sizing
```
edge = model_probability − yes_token_price
```
Enter when `edge > threshold`. Size via fractional Kelly (25%) capped by per-market, per-expiry, and total NAV limits.

| | TurtleQuant | SlowQuant |
|-|-------------|-----------|
| Entry edge | ≥5% | ≥3–6% (regime-adaptive) |
| Per-market NAV | 10% | 1.5% |
| Total exposure | 40% | 20% |
| Scan interval | 60s | 5–60s (regime-adaptive) |

### 6. Vol Regime (SlowQuant)
A composite signal — vol acceleration, funding rate z-score, and liquidation ratio — determines the current regime (Normal / Elevated / Spike), which adjusts edge thresholds, scan speed, and position sizing.

### 7. Position Management & Exit
State persists to JSON across restarts. Positions close on three triggers:
- **Edge reversed** — model prob < market price
- **Edge decayed** — current edge < 40% of entry edge
- **Time cleanup** — < 6h remaining and edge < 5%

---

## Architecture

```
Gamma API → MarketScanner → MarketParser → ProbabilityEngine → PositionManager
                                              ↑           ↑
                                         VolSurface   MonteCarlo
                                          (Deribit)   (SlowQuant)
                                              ↑
                                         Binance OHLCV
```

---

## Data Sources

| Source | Use |
|--------|-----|
| Polymarket Gamma API | Market prices & discovery |
| Deribit Options API | Implied volatility surface |
| Binance / Bybit / OKX / Gate.io | Spot price, realized vol, jump calibration |

Binance data falls back through Bybit → OKX → Gate.io for geo-resilience.

---

## Quickstart

```bash
cp .env.example .env          # configure parameters
docker compose up             # runs both bots in paper-trading mode
```

State files (`*-positions.json`, `*-history.json`) persist in `/opt/turtlequant/state`.

---

## Calibration

Backtested on 5 years of BTC and ETH data. Brier loss: **0.178–0.199** (lower is better; 0.25 = random).
