#!/usr/bin/env python3
"""TurtleQuant Bot — Probabilistic digital-option pricing on Polymarket.

Scans Polymarket for longer-term crypto prediction markets (e.g., "Will BTC
be above $75k by March 30?"), prices them as digital options using Deribit IV
or realized vol, and trades where the gap between model probability and market
price exceeds a configurable threshold.

Strategy:
  1. Scan Gamma API for active crypto price markets
  2. Parse question text → (asset, strike, expiry, option_type)
  3. Fetch current spot price from Binance
  4. Get IV from Deribit (or realized vol fallback)
  5. Compute model probability via Black-Scholes / barrier pricing
  6. If model_prob - yes_price > ENTRY_THRESHOLD and no position: buy YES tokens
  7. If holding position and model_prob < yes_price: exit (edge reversed)

Main loop: scan every 60s; reprice positions every 30s.

Usage:
    uv run python scripts/turtlequant_bot.py --paper --asset btc,eth
    uv run python scripts/turtlequant_bot.py --paper --asset btc --entry-threshold 0.07
    uv run python scripts/turtlequant_bot.py --dry-run --asset eth

Configuration (env vars or CLI):
    PAPER_TRADE          true/false — default true
    ENTRY_THRESHOLD      min edge to enter — default 0.05
    KELLY_FRACTION       fractional Kelly — default 0.25
    STARTING_NAV         starting bankroll in USD — default 1000.0
    STATE_DIR            directory for position state — default state/turtlequant
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from turtlequant.data.binance import fetch_klines
from turtlequant.market_parser import parse_market
from turtlequant.market_scanner import MarketScanner
from turtlequant.position_manager import PositionManager, make_position
from turtlequant.probability_engine import compute_probability
from turtlequant.vol_surface import VolSurface

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _setup_logging() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Always log to stderr
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
    return logging.getLogger("turtlequant_bot")


logger = _setup_logging()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ASSET_TO_SYMBOL: dict[str, str] = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}

DEFAULT_ENTRY_THRESHOLD = 0.05  # 5% minimum edge
DEFAULT_KELLY_FRACTION = 0.25
DEFAULT_STARTING_NAV = 1000.0
DEFAULT_STATE_DIR = Path("state/turtlequant")
HISTORY_FILE_NAME = "turtlequant-history.json"

SCAN_INTERVAL_SECS = 60
REPRICE_INTERVAL_SECS = 30
REENTRY_COOLDOWN_SECS = 2 * 3600  # 2 hours — prevent churn after edge-reversed close

running = True


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------


def handle_signal(sig, _frame) -> None:
    global running
    logger.info("Shutting down gracefully...")
    running = False


# ---------------------------------------------------------------------------
# Spot price fetcher
# ---------------------------------------------------------------------------


def fetch_spot(asset: str) -> float | None:
    """Fetch current spot price from Binance (latest 1m close)."""
    symbol = ASSET_TO_SYMBOL.get(asset)
    if symbol is None:
        return None
    try:
        end_ms = int(datetime.now(UTC).timestamp() * 1000)
        start_ms = end_ms - 5 * 60_000  # last 5 minutes
        df = fetch_klines(symbol, "1m", start_ms, end_ms)
        if df.empty:
            return None
        return float(df["close"].iloc[-1])
    except Exception as exc:
        logger.warning("Failed to fetch spot for %s: %s", asset.upper(), exc)
        return None


# ---------------------------------------------------------------------------
# Paper trade simulation
# ---------------------------------------------------------------------------


def paper_buy(market_id: str, yes_price: float, size_usd: float, paper_mode: bool) -> bool:
    """Simulate a YES token purchase in paper mode.

    In paper mode: assume fill at yes_price (mid). Always returns True.
    In live mode: would call CLOB client — currently raises NotImplementedError.
    """
    if paper_mode:
        logger.info(
            "[PAPER] BUY YES tokens: market=%s size=$%.2f at price=%.4f",
            market_id[:16],
            size_usd,
            yes_price,
        )
        return True
    raise NotImplementedError(
        "Live order placement not yet implemented for TurtleQuant. Use --paper for paper trading."
    )


def paper_sell(market_id: str, yes_token_id: str, size_usd: float, yes_price: float, paper_mode: bool) -> bool:
    """Simulate a YES token sale (exit) in paper mode."""
    if paper_mode:
        logger.info(
            "[PAPER] SELL YES tokens: market=%s size=$%.2f at price=%.4f",
            market_id[:16],
            size_usd,
            yes_price,
        )
        return True
    raise NotImplementedError("Live order placement not yet implemented.")


# ---------------------------------------------------------------------------
# History tracking
# ---------------------------------------------------------------------------


def append_history(state_dir: Path, entry: dict) -> None:
    """Append a trade event to turtlequant-history.json."""
    history_file = state_dir / HISTORY_FILE_NAME
    try:
        if history_file.exists():
            with history_file.open() as f:
                history: list[dict] = json.load(f)
        else:
            history = []
        history.append(entry)
        history_file.write_text(json.dumps(history, indent=2))
    except Exception as exc:
        logger.warning("Failed to append history: %s", exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    global running
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    parser = argparse.ArgumentParser(
        description="TurtleQuant — probabilistic digital-option bot for Polymarket",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--paper", action="store_true", help="Paper trading mode (safe default)")
    parser.add_argument("--live", action="store_true", help="Live trading (NOT YET IMPLEMENTED)")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate signals only; no orders")
    parser.add_argument(
        "--asset",
        default=os.getenv("ASSET", "btc,eth"),
        help="Comma-separated assets to scan (btc,eth,sol,xrp)",
    )
    parser.add_argument(
        "--entry-threshold",
        type=float,
        default=float(os.getenv("ENTRY_THRESHOLD", str(DEFAULT_ENTRY_THRESHOLD))),
        metavar="FLOAT",
        help="Minimum edge (model_prob - yes_price) to enter",
    )
    parser.add_argument(
        "--kelly-fraction",
        type=float,
        default=float(os.getenv("KELLY_FRACTION", str(DEFAULT_KELLY_FRACTION))),
        metavar="FLOAT",
        help="Fractional Kelly multiplier",
    )
    parser.add_argument(
        "--starting-nav",
        type=float,
        default=float(os.getenv("STARTING_NAV", str(DEFAULT_STARTING_NAV))),
        metavar="USD",
        help="Starting bankroll in USD",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.getenv("STATE_DIR", str(DEFAULT_STATE_DIR))),
        help="Directory for position state files",
    )
    parser.add_argument(
        "--min-liquidity",
        type=float,
        default=float(os.getenv("MIN_LIQUIDITY", "5000")),
        metavar="USD",
        help="Minimum market liquidity to consider",
    )
    parser.add_argument(
        "--max-spread-pct",
        type=float,
        default=float(os.getenv("MAX_SPREAD_PCT", "0.03")),
        metavar="FLOAT",
        help="Max bid-ask spread (as fraction of price)",
    )
    args = parser.parse_args()

    # Validate mode
    if args.live:
        logger.error("Live trading is not yet implemented. Use --paper.")
        sys.exit(1)
    paper_mode = True  # always paper until live is implemented

    # Parse assets
    assets = [a.strip().lower() for a in args.asset.split(",") if a.strip()]
    for a in assets:
        if a not in ASSET_TO_SYMBOL:
            logger.error("Unknown asset: %s. Valid: btc,eth,sol,xrp", a)
            sys.exit(1)

    # State directory
    state_dir = args.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    # Components
    scanner = MarketScanner(
        min_liquidity=args.min_liquidity,
        max_spread_pct=args.max_spread_pct,
        assets=assets,
    )

    vol_surfaces: dict[str, VolSurface] = {a: VolSurface(asset=a) for a in assets}

    pos_mgr = PositionManager(
        starting_nav=args.starting_nav,
        kelly_fraction=args.kelly_fraction,
        positions_file=state_dir / "turtlequant-positions.json",
    )

    logger.info("=== TurtleQuant Bot ===")
    logger.info("Mode        : %s%s", "PAPER", " (dry-run)" if args.dry_run else "")
    logger.info("Assets      : %s", ", ".join(a.upper() for a in assets))
    logger.info("Entry thresh: %.3f (%.1f%%)", args.entry_threshold, args.entry_threshold * 100)
    logger.info("Kelly frac  : %.2f", args.kelly_fraction)
    logger.info("Starting NAV: $%.2f", args.starting_nav)
    logger.info("State dir   : %s", state_dir)
    logger.info("")

    last_scan_time = 0.0
    last_reprice_time = 0.0
    recently_closed: dict[str, datetime] = {}  # market_id → close time (cooldown tracker)

    while running:
        now = time.time()

        # ── Reprice open positions every 30s ─────────────────────────────────
        if now - last_reprice_time >= REPRICE_INTERVAL_SECS:
            for pos in pos_mgr.all_positions():
                try:
                    # Auto-close positions whose expiry has passed
                    if datetime.now(UTC) >= pos.expiry:
                        # Fetch resolved settlement price (1.0=YES, 0.0=NO)
                        resolved_price = scanner.fetch_market_price(pos.market_id)
                        if resolved_price is None:
                            logger.warning(
                                "[EXPIRED] Could not fetch resolved price for %s — using entry price (no P&L)",
                                pos.market_id[:16],
                            )
                            resolved_price = pos.entry_price
                        logger.info(
                            "[EXPIRED] %s K=%.0f exp=%s resolved=%.4f — closing",
                            pos.asset.upper(),
                            pos.strike,
                            pos.expiry_iso[:10],
                            resolved_price,
                        )
                        if not args.dry_run:
                            paper_sell(pos.market_id, pos.yes_token_id, pos.size_usd, resolved_price, paper_mode)
                        _pos, pnl = pos_mgr.close_position(pos.market_id, exit_price=resolved_price, reason="expired")
                        recently_closed[pos.market_id] = datetime.now(UTC)
                        append_history(
                            state_dir,
                            {
                                "event": "close",
                                "market_id": pos.market_id,
                                "asset": pos.asset,
                                "strike": pos.strike,
                                "reason": "expired",
                                "exit_price": resolved_price,
                                "pnl": pnl,
                                "ts": datetime.now(UTC).isoformat(),
                            },
                        )
                        continue

                    spot = fetch_spot(pos.asset)
                    if spot is None:
                        continue
                    vs = vol_surfaces.get(pos.asset)
                    if vs is None:
                        continue
                    from turtlequant.market_parser import MarketParams, OptionType

                    params = MarketParams(
                        asset=pos.asset,
                        strike=pos.strike,
                        expiry=pos.expiry,
                        option_type=OptionType(pos.option_type),
                    )
                    sigma = vs.get_iv(spot, pos.strike, pos.expiry)
                    model_prob = compute_probability(params, spot, sigma)

                    # Fetch current YES price from scanner (rough: use gamma API)
                    # For simplicity, we can't easily get yes_price here without
                    # refetching the specific market. Use stored entry price as proxy.
                    # A future enhancement would cache the latest scanner prices.
                    yes_price = pos.entry_price  # proxy — scanner will refresh on next scan

                    if pos_mgr.should_exit(pos.market_id, model_prob, yes_price):
                        if not args.dry_run:
                            paper_sell(pos.market_id, pos.yes_token_id, pos.size_usd, yes_price, paper_mode)
                        _pos, pnl = pos_mgr.close_position(pos.market_id, exit_price=yes_price, reason="edge_reversed")
                        recently_closed[pos.market_id] = datetime.now(UTC)
                        append_history(
                            state_dir,
                            {
                                "event": "close",
                                "market_id": pos.market_id,
                                "asset": pos.asset,
                                "strike": pos.strike,
                                "reason": "edge_reversed",
                                "model_prob": model_prob,
                                "yes_price": yes_price,
                                "pnl": pnl,
                                "ts": datetime.now(UTC).isoformat(),
                            },
                        )
                    else:
                        logger.info(
                            "[HOLD] %s K=%.0f exp=%s model_p=%.4f entry_p=%.4f edge=%.4f",
                            pos.asset.upper(),
                            pos.strike,
                            pos.expiry_iso[:10],
                            model_prob,
                            pos.entry_price,
                            model_prob - pos.entry_price,
                        )
                except Exception as exc:
                    logger.warning("Reprice failed for %s: %s", pos.market_id[:16], exc)

            last_reprice_time = now

        # ── Full market scan every 60s ────────────────────────────────────────
        if now - last_scan_time >= SCAN_INTERVAL_SECS:
            last_scan_time = now
            try:
                markets = scanner.get_active_markets()
                logger.info("Scan: %d markets found", len(markets))
            except Exception as exc:
                logger.warning("Market scan failed: %s", exc)
                time.sleep(5)
                continue

            # Fetch spot prices once per scan
            spots: dict[str, float | None] = {a: fetch_spot(a) for a in assets}

            for market in markets:
                if not running:
                    break
                try:
                    params = parse_market(market.question, market.resolution_time)
                    if params is None:
                        continue
                    if params.asset not in assets:
                        continue

                    spot = spots.get(params.asset)
                    if spot is None or spot <= 0:
                        continue

                    vs = vol_surfaces[params.asset]
                    sigma = vs.get_iv(spot, params.strike, params.expiry)
                    model_prob = compute_probability(params, spot, sigma)
                    yes_price = market.yes_price
                    edge = model_prob - yes_price

                    logger.debug(
                        "%s K=%.0f exp=%s model_p=%.4f mkt_p=%.4f edge=%+.4f σ=%.3f",
                        params.asset.upper(),
                        params.strike,
                        params.expiry.strftime("%Y-%m-%d"),
                        model_prob,
                        yes_price,
                        edge,
                        sigma,
                    )

                    # ── Check exit for existing positions ─────────────────
                    if pos_mgr.has_position(market.market_id):
                        if pos_mgr.should_exit(market.market_id, model_prob, yes_price):
                            if not args.dry_run:
                                pos = pos_mgr.get_position(market.market_id)
                                if pos:
                                    paper_sell(market.market_id, pos.yes_token_id, pos.size_usd, yes_price, paper_mode)
                            _pos, pnl = pos_mgr.close_position(
                                market.market_id, exit_price=yes_price, reason="edge_reversed"
                            )
                            recently_closed[market.market_id] = datetime.now(UTC)
                            append_history(
                                state_dir,
                                {
                                    "event": "close",
                                    "market_id": market.market_id,
                                    "asset": params.asset,
                                    "strike": params.strike,
                                    "reason": "edge_reversed",
                                    "model_prob": model_prob,
                                    "yes_price": yes_price,
                                    "pnl": pnl,
                                    "ts": datetime.now(UTC).isoformat(),
                                },
                            )
                        continue

                    # ── Check entry ───────────────────────────────────────
                    # Skip if this market was recently closed (re-entry cooldown)
                    closed_at = recently_closed.get(market.market_id)
                    if closed_at and (datetime.now(UTC) - closed_at).total_seconds() < REENTRY_COOLDOWN_SECS:
                        logger.debug("Cooldown active for %s — skip re-entry", market.market_id[:16])
                        continue

                    if edge < args.entry_threshold:
                        continue
                    if yes_price <= 0.02 or yes_price >= 0.98:
                        continue  # near-certain markets — skip

                    size_usd = pos_mgr.kelly_size(edge, model_prob, yes_price)
                    if size_usd < 1.0:
                        logger.debug("Size too small ($%.2f) for %s — skip", size_usd, market.market_id[:16])
                        continue

                    # Per-expiry exposure check
                    if not pos_mgr.has_expiry_headroom(params.expiry, size_usd):
                        logger.info(
                            "Per-expiry cap reached for %s — skip",
                            params.expiry.strftime("%Y-%m-%d"),
                        )
                        continue

                    logger.info(
                        "[SIGNAL] %s %s K=%.0f exp=%s model_p=%.4f mkt_p=%.4f edge=+%.4f size=$%.2f σ=%.3f",
                        params.asset.upper(),
                        params.option_type.value,
                        params.strike,
                        params.expiry.strftime("%Y-%m-%d"),
                        model_prob,
                        yes_price,
                        edge,
                        size_usd,
                        sigma,
                    )

                    if args.dry_run:
                        continue

                    # Place order
                    filled = paper_buy(market.market_id, yes_price, size_usd, paper_mode)
                    if not filled:
                        continue

                    pos = make_position(
                        market_id=market.market_id,
                        question=market.question,
                        asset=params.asset,
                        strike=params.strike,
                        expiry=params.expiry,
                        option_type=params.option_type.value,
                        yes_token_id=market.yes_token_id,
                        yes_price=yes_price,
                        size_usd=size_usd,
                        model_prob=model_prob,
                    )
                    pos_mgr.open_position(pos)
                    append_history(
                        state_dir,
                        {
                            "event": "open",
                            "market_id": market.market_id,
                            "question": market.question[:120],
                            "asset": params.asset,
                            "strike": params.strike,
                            "expiry": params.expiry.isoformat(),
                            "option_type": params.option_type.value,
                            "model_prob": model_prob,
                            "yes_price": yes_price,
                            "edge": edge,
                            "size_usd": size_usd,
                            "sigma": sigma,
                            "ts": datetime.now(UTC).isoformat(),
                        },
                    )

                except Exception as exc:
                    logger.warning("Market processing error (%s): %s", market.market_id[:16], exc)

        # Sleep until next event
        time.sleep(5)

    logger.info("TurtleQuant bot stopped.")


if __name__ == "__main__":
    main()
