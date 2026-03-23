#!/usr/bin/env python3
"""SlowQuant Bot — Merton jump-diffusion pricing on short-dated Polymarket price threshold markets.

Targets 1–10 day crypto price threshold markets ("Will BTC be above $X by Friday?"),
pricing them as digital options with a Monte Carlo jump-diffusion model. Trades when
the gap between model probability and market price exceeds a regime-adaptive threshold.

Strategy:
  1. Fetch Binance 1h + 5m candles for each asset
  2. Calibrate Merton jump parameters from 30d 1h log-returns
  3. Compute vol regime (vol acceleration + funding + liquidation)
  4. Scan Gamma API for active crypto price markets in the expiry window
  5. BS pre-filter: |BS edge| < 3%  →  skip MC
  6. MC simulation: estimate P(S_T > K) via Merton jump-diffusion (50k paths)
  7. Trade if MC edge > regime threshold (3–6% depending on regime)
  8. Exit when edge decays to < 40% of entry, reverses, or time-decay cleanup

Main loop: regime-adaptive — 60s (normal), 20s (elevated), 5s (spike).

Usage:
    uv run python scripts/slowquant_bot.py --paper
    uv run python scripts/slowquant_bot.py --paper --asset btc,eth --n-sims 50000
    uv run python scripts/slowquant_bot.py --dry-run --asset btc
    uv run python scripts/slowquant_bot.py --paper --edge-threshold 0.07 --max-trades 3

Configuration (env vars or CLI flags):
    PAPER_TRADE          true/false — default true
    ASSET                comma-separated (btc,eth,sol,xrp) — default btc,eth
    N_SIMS               MC simulations per trade — default 50000
    STARTING_NAV         bankroll in USD — default 1000.0
    KELLY_FRACTION       fractional Kelly — default 0.25
    ENTRY_THRESHOLD      overrides regime edge threshold — default disabled
    STATE_DIR            state file directory — default state/slowquant
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from turtlequant.slowquant.strategy_loop import SlowQuantRunner

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    # Also log to LOG_FILE if set (for monitor tail)
    log_file = os.getenv("LOG_FILE", "")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    return logging.getLogger("slowquant_bot")


logger = _setup_logging()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_ASSETS = {"btc", "eth", "sol", "xrp"}
DEFAULT_ASSETS = "btc,eth"
DEFAULT_N_SIMS = 50_000
DEFAULT_STARTING_NAV = 1000.0
DEFAULT_KELLY_FRACTION = 0.25
DEFAULT_STATE_DIR = Path("state/slowquant")
DEFAULT_MAX_TRADES = 5


# ---------------------------------------------------------------------------
# Signal handler
# ---------------------------------------------------------------------------


def _handle_signal(runner: SlowQuantRunner):
    def _handler(sig, _frame) -> None:
        logger.info("Shutdown signal received — stopping after current cycle...")
        runner.running = False

    return _handler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SlowQuant — Merton jump-diffusion bot for short-dated Polymarket price threshold markets",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--paper", action="store_true", help="Paper trading mode (safe default)")
    parser.add_argument("--live", action="store_true", help="Live trading (NOT YET IMPLEMENTED)")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate signals only; no orders placed")
    parser.add_argument(
        "--asset",
        default=os.getenv("ASSET", DEFAULT_ASSETS),
        help="Comma-separated assets to trade (btc,eth,sol,xrp)",
    )
    parser.add_argument(
        "--n-sims",
        type=int,
        default=int(os.getenv("N_SIMS", str(DEFAULT_N_SIMS))),
        metavar="INT",
        help="Monte Carlo paths per simulation",
    )
    parser.add_argument(
        "--starting-nav",
        type=float,
        default=float(os.getenv("STARTING_NAV", str(DEFAULT_STARTING_NAV))),
        metavar="USD",
        help="Starting bankroll in USD",
    )
    parser.add_argument(
        "--kelly-fraction",
        type=float,
        default=float(os.getenv("KELLY_FRACTION", str(DEFAULT_KELLY_FRACTION))),
        metavar="FLOAT",
        help="Fractional Kelly multiplier (0–1)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.getenv("STATE_DIR", str(DEFAULT_STATE_DIR))),
        help="Directory for position state and history files",
    )
    parser.add_argument(
        "--max-trades",
        type=int,
        default=int(os.getenv("MAX_TRADES", str(DEFAULT_MAX_TRADES))),
        metavar="INT",
        help="Maximum new trades to execute per scan cycle",
    )
    parser.add_argument(
        "--min-liquidity",
        type=float,
        default=float(os.getenv("MIN_LIQUIDITY", "5000")),
        metavar="USD",
        help="Minimum market liquidity in USD",
    )
    parser.add_argument(
        "--max-spread-pct",
        type=float,
        default=float(os.getenv("MAX_SPREAD_PCT", "0.05")),
        metavar="FLOAT",
        help="Maximum bid-ask spread as fraction of price",
    )
    args = parser.parse_args()

    # --- Validate ---
    if args.live:
        logger.error("Live trading not yet implemented. Use --paper.")
        sys.exit(1)

    assets = [a.strip().lower() for a in args.asset.split(",") if a.strip()]
    invalid = [a for a in assets if a not in VALID_ASSETS]
    if invalid:
        logger.error("Unknown assets: %s. Valid: btc,eth,sol,xrp", invalid)
        sys.exit(1)

    args.state_dir.mkdir(parents=True, exist_ok=True)

    # --- Build runner ---
    runner = SlowQuantRunner(
        assets=assets,
        state_dir=args.state_dir,
        starting_nav=args.starting_nav,
        kelly_fraction=args.kelly_fraction,
        n_sims=args.n_sims,
        min_liquidity=args.min_liquidity,
        max_spread_pct=args.max_spread_pct,
        max_trades_per_cycle=args.max_trades,
        paper=True,  # always paper until live is implemented
        dry_run=args.dry_run,
    )

    signal.signal(signal.SIGINT, _handle_signal(runner))
    signal.signal(signal.SIGTERM, _handle_signal(runner))

    # --- Log startup banner ---
    mode_str = "DRY-RUN" if args.dry_run else "PAPER"
    logger.info("=== SlowQuant Bot ===")
    logger.info("Mode         : %s", mode_str)
    logger.info("Assets       : %s", ", ".join(a.upper() for a in assets))
    logger.info("MC sims      : %d", args.n_sims)
    logger.info("Kelly frac   : %.2f", args.kelly_fraction)
    logger.info("Starting NAV : $%.2f", args.starting_nav)
    logger.info("Max trades   : %d/cycle", args.max_trades)
    logger.info("State dir    : %s", args.state_dir)
    logger.info("")
    logger.info("Regime thresholds (adaptive):")
    logger.info("  normal   → edge≥6%%  scan=60s  min_expiry=24h")
    logger.info("  elevated → edge≥4%%  scan=20s  min_expiry=12h")
    logger.info("  spike    → edge≥3%%  scan=5s   min_expiry=6h")
    logger.info("")

    runner.run()


if __name__ == "__main__":
    main()
