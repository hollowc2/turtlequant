#!/usr/bin/env python3
"""SlowQuant calibration — validate Merton MC pricing on historical BTC/ETH data.

Simulates short-dated (3d / 7d / 10d) price threshold markets on 3 years of
hourly Binance data and measures whether the MC model probability is calibrated
against actual outcomes.

Method:
  - Fetch 3 years of 1h closes from Binance
  - For each entry point (stride = 24h):
      * Sample 30d of prior 1h returns to calibrate jump params
      * Simulate a range of moneyness (K/S0) for 3d / 7d / 10d horizons
      * Run MC model with calibrated jump params + 30d realized vol
      * Record outcome: did price exceed strike at expiry?
  - Bucket by model probability → compare mean_model_p vs actual_win_rate
  - Report Brier score and calibration RMSE

Deploy gate:
  Brier score < 0.20 AND calibration RMSE < 0.05

Usage:
    uv run python scripts/calibrate_slowquant.py --asset btc --years 3
    uv run python scripts/calibrate_slowquant.py --asset eth --years 3
    uv run python scripts/calibrate_slowquant.py --asset btc --plot
    uv run python scripts/calibrate_slowquant.py --asset btc --n-sims 5000 --years 2
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from turtlequant.data.binance import fetch_klines
from turtlequant.slowquant.monte_carlo import calibrate_jump_params
from turtlequant.slowquant.monte_carlo import simulate as mc_simulate

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ASSET_TO_SYMBOL = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}

# Simulated market parameters
DAYS_TO_EXPIRY = [3, 7, 10]
STRIKE_MONEYNESS = [0.90, 0.93, 0.96, 0.99, 1.00, 1.01, 1.04, 1.07, 1.10]

ENTRY_STRIDE_HOURS = 24  # evaluate entry every 24h (avoids autocorrelation)
REALIZED_VOL_LOOKBACK_HOURS = 30 * 24  # 30 days
JUMP_CALIB_LOOKBACK_HOURS = 30 * 24

BRIER_THRESHOLD = 0.20
RMSE_THRESHOLD = 0.05

BUCKET_EDGES = np.linspace(0.0, 1.0, 11)  # 10 buckets: [0.0, 0.1), ..., [0.9, 1.0]


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def fetch_hourly_closes(symbol: str, years: int) -> pd.Series:
    end_ms = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = end_ms - years * 365 * 86_400_000
    df = fetch_klines(symbol, "1h", start_ms, end_ms)
    if df.empty:
        raise RuntimeError(f"No 1h data returned for {symbol}")
    df = df.set_index("open_time").sort_index()
    closes = df["close"].astype(float)
    print(f"Fetched {len(closes):,} 1h closes for {symbol}  ({closes.index[0].date()} – {closes.index[-1].date()})")
    return closes


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def simulate_entries(closes: pd.Series, n_sims: int) -> list[dict]:
    """Generate all simulated (entry, horizon, strike, model_p, outcome) rows."""
    prices = closes.values
    n = len(prices)
    rows: list[dict] = []

    for entry_i in range(JUMP_CALIB_LOOKBACK_HOURS, n, ENTRY_STRIDE_HOURS):
        S0 = float(prices[entry_i])

        # Realized vol from prior 30d 1h returns
        vol_window = prices[max(0, entry_i - REALIZED_VOL_LOOKBACK_HOURS) : entry_i]
        if len(vol_window) < 5:
            continue
        log_rets_vol = np.log(vol_window[1:] / vol_window[:-1])
        sigma = float(np.std(log_rets_vol) * np.sqrt(8760))
        sigma = float(np.clip(sigma, 0.10, 2.0))

        # Calibrate jump params from prior 30d 1h returns
        log_rets_jump = np.log(
            prices[max(0, entry_i - JUMP_CALIB_LOOKBACK_HOURS) : entry_i][1:]
            / prices[max(0, entry_i - JUMP_CALIB_LOOKBACK_HOURS) : entry_i][:-1]
        )
        jump_params = calibrate_jump_params(log_rets_jump)

        for days in DAYS_TO_EXPIRY:
            expiry_i = entry_i + days * 24
            if expiry_i >= n:
                continue
            S_T = float(prices[expiry_i])
            T = days / 365.0

            for moneyness in STRIKE_MONEYNESS:
                K = S0 * moneyness
                model_p = mc_simulate(S0=S0, K=K, T=T, sigma=sigma, jump_params=jump_params, n_sims=n_sims)
                outcome = 1 if S_T > K else 0
                rows.append(
                    {
                        "entry_i": entry_i,
                        "days_to_exp": days,
                        "moneyness": moneyness,
                        "S0": S0,
                        "K": K,
                        "S_T": S_T,
                        "sigma": sigma,
                        "lambda_yr": jump_params.lambda_per_year,
                        "model_p": model_p,
                        "outcome": outcome,
                    }
                )

        # Progress indicator every 100 entries
        completed = (entry_i - JUMP_CALIB_LOOKBACK_HOURS) // ENTRY_STRIDE_HOURS
        total_est = (n - JUMP_CALIB_LOOKBACK_HOURS) // ENTRY_STRIDE_HOURS
        if completed % 50 == 0:
            pct = 100 * completed / max(total_est, 1)
            print(f"  Progress: {completed}/{total_est} entries ({pct:.0f}%)  trades so far: {len(rows):,}")

    return rows


# ---------------------------------------------------------------------------
# Calibration metrics
# ---------------------------------------------------------------------------


def calibration_metrics(rows: list[dict]) -> tuple[float, float, pd.DataFrame]:
    """Compute Brier score, calibration RMSE, and per-bucket stats.

    Returns:
        (brier_score, calibration_rmse, bucket_df)
    """
    df = pd.DataFrame(rows)

    brier = float(((df["model_p"] - df["outcome"]) ** 2).mean())

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

    weights = bucket_stats["count"] / bucket_stats["count"].sum()
    rmse = float(np.sqrt((bucket_stats["calib_error"] ** 2 * weights).sum()))

    return brier, rmse, bucket_stats


# ---------------------------------------------------------------------------
# Horizon breakdown
# ---------------------------------------------------------------------------


def horizon_metrics(rows: list[dict]) -> None:
    """Print per-horizon Brier scores for diagnostics."""
    df = pd.DataFrame(rows)
    print("\nPer-horizon Brier scores:")
    for days in DAYS_TO_EXPIRY:
        sub = df[df["days_to_exp"] == days]
        if sub.empty:
            continue
        brier = float(((sub["model_p"] - sub["outcome"]) ** 2).mean())
        print(f"  {days:2d}d horizon: Brier={brier:.4f}  n={len(sub):,}")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_results(asset: str, brier: float, rmse: float, bucket_df: pd.DataFrame, n_trades: int) -> None:
    print(f"\n{'=' * 62}")
    print(f"SlowQuant Calibration — {asset.upper()}")
    print(f"{'=' * 62}")
    print(f"Total simulated trades : {n_trades:,}")
    print(f"Brier score            : {brier:.4f}   (gate: < {BRIER_THRESHOLD})")
    print(f"Calibration RMSE       : {rmse:.4f}   (gate: < {RMSE_THRESHOLD})")
    brier_ok = brier < BRIER_THRESHOLD
    rmse_ok = rmse < RMSE_THRESHOLD
    verdict = "PASS ✓" if (brier_ok and rmse_ok) else "FAIL ✗"
    print(f"Deploy verdict         : {verdict}")
    print()
    print(f"{'Bucket':<12} {'Count':>7} {'Model P':>9} {'Actual WR':>10} {'Error':>8}")
    print("-" * 52)
    for _, row in bucket_df.iterrows():
        b_lo = BUCKET_EDGES[int(row["bucket"])]
        b_hi = BUCKET_EDGES[int(row["bucket"]) + 1]
        print(
            f"[{b_lo:.1f}, {b_hi:.1f})  "
            f"{int(row['count']):>7,}  "
            f"{row['mean_model_p']:>9.4f}  "
            f"{row['actual_win_rate']:>10.4f}  "
            f"{row['calib_error']:>+8.4f}"
        )
    print()


def plot_calibration(asset: str, bucket_df: pd.DataFrame, n_sims: int) -> None:
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
    ax.set_title(f"SlowQuant calibration — {asset.upper()} (n_sims={n_sims:,})")
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    out_path = Path(f"calibration_slowquant_{asset}.png")
    fig.savefig(out_path)
    print(f"Plot saved: {out_path}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SlowQuant MC calibration validator",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--asset", choices=list(ASSET_TO_SYMBOL.keys()), default="btc")
    parser.add_argument("--years", type=int, default=3, help="Years of historical 1h data to use")
    parser.add_argument("--n-sims", type=int, default=5_000, help="MC paths per trade (use fewer for speed)")
    parser.add_argument("--plot", action="store_true", help="Save calibration curve PNG")
    args = parser.parse_args()

    symbol = ASSET_TO_SYMBOL[args.asset]
    print(f"Fetching {args.years}y of 1h data for {symbol}...")
    closes = fetch_hourly_closes(symbol, args.years)

    n_entries_est = (len(closes) - JUMP_CALIB_LOOKBACK_HOURS) // ENTRY_STRIDE_HOURS
    n_trades_est = n_entries_est * len(DAYS_TO_EXPIRY) * len(STRIKE_MONEYNESS)
    print(f"Simulating ~{n_trades_est:,} trades  (n_sims={args.n_sims})")
    print("This may take several minutes...")

    rows = simulate_entries(closes, n_sims=args.n_sims)
    print(f"\nGenerated {len(rows):,} simulated trades")

    brier, rmse, bucket_df = calibration_metrics(rows)
    print_results(args.asset, brier, rmse, bucket_df, len(rows))
    horizon_metrics(rows)

    if args.plot:
        plot_calibration(args.asset, bucket_df, args.n_sims)

    passed = brier < BRIER_THRESHOLD and rmse < RMSE_THRESHOLD
    if not passed:
        print("WARNING: Calibration did not pass gates. Review jump params before deploying.")
        sys.exit(1)
    else:
        print("Calibration passed. Safe to run paper bot.")
        sys.exit(0)


if __name__ == "__main__":
    main()
