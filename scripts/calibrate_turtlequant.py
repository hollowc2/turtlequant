#!/usr/bin/env python3
"""TurtleQuant calibration — validate option-model probabilities on historical OHLCV.

Simulates 100+ (strike, expiry) combinations as if they were Polymarket markets
and measures whether the model probability is well-calibrated against actual outcomes.
This is model calibration, not evidence of executable Polymarket alpha.

Calibration test:
  - Pull historical daily OHLCV (configurable lookback, default 3 years)
  - For each simulated entry: compute model_probability using digital_probability()
    with 30d realized vol at that time
  - Record non-overlapping expiry, up-touch, and down-touch outcomes
  - Group by probability bucket (0.0–0.1, 0.1–0.2, …, 0.9–1.0)
  - Report: calibration RMSE, Brier score

Deploy threshold:
  Brier score < 0.25 AND calibration RMSE < 0.05

Usage:
    uv run python scripts/calibrate_turtlequant.py --asset btc --years 3
    uv run python scripts/calibrate_turtlequant.py --asset eth --years 5
    uv run python scripts/calibrate_turtlequant.py --asset btc --plot
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from turtlequant.data.binance import fetch_klines
from turtlequant.probability_engine import (
    barrier_down_probability,
    barrier_probability,
    digital_probability,
)

ASSET_TO_SYMBOL = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}

# Simulated market parameters
DAYS_TO_EXPIRY = [7, 14, 30, 60, 90]  # multiple horizons
STRIKE_MONEYNESS = [0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20]  # K/S0
REALIZED_VOL_LOOKBACK_DAYS = 30
RISK_FREE_RATE = 0.05

BRIER_THRESHOLD = 0.25
RMSE_THRESHOLD = 0.05

BUCKET_EDGES = np.linspace(0.0, 1.0, 11)  # 10 buckets: [0,0.1), [0.1,0.2), ...


def fetch_daily_prices(symbol: str, years: int) -> pd.DataFrame:
    end_ms = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = end_ms - years * 365 * 86_400_000
    df = fetch_klines(symbol, "1d", start_ms, end_ms)
    if df.empty:
        raise RuntimeError(f"No data returned for {symbol}")
    df = df.set_index("open_time").sort_index()
    prices = df[["close", "high", "low"]].astype(float)
    print(f"Fetched {len(prices)} daily OHLCV rows for {symbol} ({prices.index[0].date()} – {prices.index[-1].date()})")
    return prices


def compute_realized_vol(prices: pd.DataFrame, idx: int, lookback: int = REALIZED_VOL_LOOKBACK_DAYS) -> float:
    start = max(0, idx - lookback)
    window = prices["close"].iloc[start : idx + 1].values
    if len(window) < 5:
        return 0.80
    log_returns = np.log(window[1:] / window[:-1])
    return float(np.std(log_returns) * np.sqrt(365))


def simulate_entries(prices: pd.DataFrame) -> list[dict]:
    """Generate non-overlapping simulated European and barrier contracts."""
    dates = prices.index
    n = len(prices)
    rows: list[dict] = []

    for days_to_exp in DAYS_TO_EXPIRY:
        for entry_i in range(REALIZED_VOL_LOOKBACK_DAYS, n - days_to_exp, days_to_exp):
            S0 = float(prices["close"].iloc[entry_i])
            sigma = compute_realized_vol(prices, entry_i)
            expiry_i = min(entry_i + days_to_exp, n - 1)
            future = prices.iloc[entry_i + 1 : expiry_i + 1]
            S_T = float(future["close"].iloc[-1])
            T = days_to_exp / 365.0

            for moneyness in STRIKE_MONEYNESS:
                K = S0 * moneyness
                base = {
                    "entry_date": dates[entry_i], "expiry_date": dates[expiry_i],
                    "days_to_exp": days_to_exp, "moneyness": moneyness,
                    "S0": S0, "K": K, "S_T": S_T, "sigma": sigma,
                }
                rows.append({**base, "contract_type": "european", "model_p": digital_probability(S0, K, T, sigma, RISK_FREE_RATE), "outcome": int(S_T > K)})
                if K > S0:
                    rows.append({**base, "contract_type": "barrier_up", "model_p": barrier_probability(S0, K, T, sigma, RISK_FREE_RATE), "outcome": int(future["high"].max() >= K)})
                elif K < S0:
                    rows.append({**base, "contract_type": "barrier_down", "model_p": barrier_down_probability(S0, K, T, sigma, RISK_FREE_RATE), "outcome": int(future["low"].min() <= K)})

    return rows


def calibration_metrics(rows: list[dict]) -> tuple[float, float, pd.DataFrame]:
    """Compute Brier score, calibration RMSE, and per-bucket stats.

    Returns:
        (brier_score, calibration_rmse, bucket_df)
    """
    df = pd.DataFrame(rows)

    # Brier score: mean((model_p - outcome)^2)
    brier = float(((df["model_p"] - df["outcome"]) ** 2).mean())

    # Bucket calibration
    df["bucket"] = pd.cut(df["model_p"], bins=BUCKET_EDGES, labels=False, include_lowest=True)
    bucket_stats = (
        df.groupby("bucket")
        .agg(
            count=("outcome", "count"),
            mean_model_p=("model_p", "mean"),
            actual_win_rate=("outcome", "mean"),
        )
        .reset_index()
    )
    bucket_stats["calib_error"] = bucket_stats["mean_model_p"] - bucket_stats["actual_win_rate"]

    # RMSE across buckets (weighted by count)
    weights = bucket_stats["count"] / bucket_stats["count"].sum()
    rmse = float(np.sqrt((bucket_stats["calib_error"] ** 2 * weights).sum()))

    return brier, rmse, bucket_stats


def print_results(asset: str, brier: float, rmse: float, bucket_df: pd.DataFrame, n_trades: int) -> None:
    print(f"\n{'=' * 60}")
    print(f"TurtleQuant Calibration — {asset.upper()}")
    print(f"{'=' * 60}")
    print(f"Total simulated contracts: {n_trades:,}")
    print(f"Brier score:            {brier:.4f}  (threshold: < {BRIER_THRESHOLD})")
    print(f"Calibration RMSE:       {rmse:.4f}  (threshold: < {RMSE_THRESHOLD})")
    brier_ok = brier < BRIER_THRESHOLD
    rmse_ok = rmse < RMSE_THRESHOLD
    verdict = "PASS" if (brier_ok and rmse_ok) else "FAIL"
    print(f"Model-calibration verdict: {verdict}")
    print()

    print(f"{'Bucket':<10} {'Count':>7} {'Model P':>9} {'Actual WR':>10} {'Error':>8}")
    print("-" * 50)
    for _, row in bucket_df.iterrows():
        b_lo = BUCKET_EDGES[int(row["bucket"])]
        b_hi = BUCKET_EDGES[int(row["bucket"]) + 1]
        print(
            f"[{b_lo:.1f},{b_hi:.1f})  "
            f"{int(row['count']):>7,}  "
            f"{row['mean_model_p']:>9.4f}  "
            f"{row['actual_win_rate']:>10.4f}  "
            f"{row['calib_error']:>+8.4f}"
        )
    print()


def plot_calibration(asset: str, bucket_df: pd.DataFrame) -> None:
    try:
        import matplotlib.pyplot as plt  # type: ignore[import-untyped]
    except ImportError:
        print("matplotlib not available — skipping plot")
        return

    x = bucket_df["mean_model_p"].values
    y = bucket_df["actual_win_rate"].values
    counts = bucket_df["count"].values

    fig, ax = plt.subplots(figsize=(7, 7))
    ax.plot([0, 1], [0, 1], "k--", alpha=0.5, label="Perfect calibration")
    ax.scatter(x, y, s=np.sqrt(counts) * 3, alpha=0.8, c="steelblue", zorder=3)
    ax.set_xlabel("Model probability")
    ax.set_ylabel("Actual win rate")
    ax.set_title(f"TurtleQuant calibration — {asset.upper()}")
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    out_path = Path(f"calibration_{asset}.png")
    fig.savefig(out_path)
    print(f"Calibration plot saved: {out_path}")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="TurtleQuant calibration validator")
    parser.add_argument("--asset", choices=["btc", "eth", "sol", "xrp"], default="btc")
    parser.add_argument("--years", type=int, default=3, help="Years of historical data to use")
    parser.add_argument("--plot", action="store_true", help="Save calibration curve PNG")
    args = parser.parse_args()

    symbol = ASSET_TO_SYMBOL[args.asset]
    print(f"Fetching {args.years} years of {symbol} daily OHLCV...")
    prices = fetch_daily_prices(symbol, args.years)

    print(f"Simulating entries (horizons={DAYS_TO_EXPIRY}, strikes={len(STRIKE_MONEYNESS)})...")
    rows = simulate_entries(prices)
    print(f"Generated {len(rows):,} simulated contracts")

    brier, rmse, bucket_df = calibration_metrics(rows)
    print_results(args.asset, brier, rmse, bucket_df, len(rows))

    if args.plot:
        plot_calibration(args.asset, bucket_df)

    # Exit non-zero if calibration fails
    if brier >= BRIER_THRESHOLD or rmse >= RMSE_THRESHOLD:
        print("WARNING: Model calibration did not pass thresholds.")
        sys.exit(1)
    else:
        print("Model calibration passed; this does not establish executable trading alpha.")
        sys.exit(0)


if __name__ == "__main__":
    main()
