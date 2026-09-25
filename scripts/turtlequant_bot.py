#!/usr/bin/env python3
"""TurtleQuant Bot — Probabilistic digital-option pricing on Polymarket.

Scans Polymarket's crypto price-threshold markets (daily to year-end, e.g.
"Will BTC be above $75k on March 30?", "Will ETH dip to $2,000 by December
31?"), prices them as digital or barrier options using Deribit IV or realized
vol, and trades where the gap between model probability and market price
exceeds a configurable threshold.

Strategy:
  1. Scan Gamma API for active crypto price markets
  2. Parse question text → (asset, strike, expiry, option_type)
  3. Fetch current spot price from Binance
  4. Get IV from Deribit (or realized vol fallback)
  5. Compute model probability via Black-Scholes / barrier pricing
  6. If model_prob - yes_price > ENTRY_THRESHOLD and no position: buy YES tokens
     (with --sides yes,no, buy NO when yes_price - model_prob clears it instead)
  7. If holding and the held token's bid exceeds its model value: exit (edge reversed)

Main loop: scan every 60s; reprice positions every 30s.

Usage:
    uv run python scripts/turtlequant_bot.py --paper --asset btc,eth
    uv run python scripts/turtlequant_bot.py --paper --asset btc --entry-threshold 0.07
    uv run python scripts/turtlequant_bot.py --dry-run --asset eth

Configuration (env vars or CLI):
    Mode flags           --shadow | --paper | --live --i-accept-live-risk | --dry-run
    ENTRY_THRESHOLD      min edge to enter — default 0.05
    KELLY_FRACTION       fractional Kelly — default 0.25
    STARTING_NAV         starting bankroll in USD — default 1000.0
    STATE_DIR            directory for position state — default state/turtlequant
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from turtlequant.clob_execution import ExecutionClient
from turtlequant.data.binance import ASSET_TO_SYMBOL, fetch_klines
from turtlequant.discord_trades import DiscordTrades
from turtlequant.market_parser import set_corpus_file
from turtlequant.market_scanner import MarketScanner
from turtlequant.notifications import NotificationQueue
from turtlequant.order_intents import OrderIntentLedger
from turtlequant.order_reconciliation import ReconciliationError, reconcile_outstanding
from turtlequant.position_manager import PositionManager
from turtlequant.risk_controls import RiskControls
from turtlequant.trader import Trader, TraderConfig
from turtlequant.vol_surface import VolSurface

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _setup_logging() -> logging.Logger:
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
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
        max_bytes = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
        backup_count = int(os.getenv("LOG_BACKUP_COUNT", "5"))
        fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    return logging.getLogger("turtlequant_bot")


logger = _setup_logging()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ENTRY_THRESHOLD = 0.05  # 5% minimum edge
DEFAULT_KELLY_FRACTION = 0.25
DEFAULT_STARTING_NAV = 1000.0
DEFAULT_CALIBRATION_RMSE = 0.05
DEFAULT_STATE_DIR = Path("state/turtlequant")

SCAN_INTERVAL_SECS = 60
REPRICE_INTERVAL_SECS = 30

running = True


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------


def handle_signal(sig, _frame) -> None:
    global running
    logger.info("Shutting down gracefully...")
    running = False


# ---------------------------------------------------------------------------
# Discord notifications
# ---------------------------------------------------------------------------


def trade_chart(
    discord: DiscordTrades,
    pos,
    entry_ms: int,
    exit_ms: int | None = None,
    *,
    model_prob: float | None = None,
    sigma: float | None = None,
    exit_price: float | None = None,
    pnl: float | None = None,
) -> bytes | None:
    interval = os.getenv("DISCORD_CHART_INTERVAL", "1h")
    end_ms = exit_ms or int(time.time() * 1000)
    start_ms = end_ms - (90 if interval == "1d" else 30) * 86_400_000
    try:
        frame = fetch_klines(ASSET_TO_SYMBOL[pos.asset], interval, start_ms, end_ms)
        return discord.chart(
            frame,
            f"{pos.asset.upper()} {interval} spot vs strike",
            entry_ms,
            exit_ms,
            strike=pos.strike,
            model_prob=model_prob or pos.model_prob_at_entry,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            edge=pos.edge_at_entry,
            sigma=sigma,
            pnl=pnl,
            expiry=pos.expiry_iso,
            yes_above_strike=pos.option_type not in {"barrier_down", "european_put"},
            bought_side=pos.outcome,
        )
    except Exception:
        return None


def notify_entry(
    discord: DiscordTrades,
    pos,
    *,
    model_prob: float,
    bid: float,
    ask: float,
    sigma: float,
) -> None:
    discord.send(
        pos.market_id,
        (
            f"🐢 **TURTLEQUANT ENTERED** `{pos.asset.upper()} {pos.outcome}`\n"
            f"> {pos.question[:180]}\n"
            f"> Fill: **{pos.entry_price:.3f}** | Bid/Ask: {bid:.3f}/{ask:.3f}\n"
            f"> Size: **${pos.size_usd:.2f}** | Shares: {pos.token_size:.4f}\n"
            f"> Model: {model_prob:.1%} | Edge: {pos.edge_at_entry:+.1%} | IV: {sigma:.1%}\n"
            f"> Strike: ${pos.strike:,.0f} | Expiry: {pos.expiry_iso}"
        ),
        remember=True,
    )


def notify_exit(
    discord: DiscordTrades, pos, exit_price: float, pnl: float, reason: str
) -> None:
    entry_ms = int(datetime.fromisoformat(pos.opened_at).timestamp() * 1000)
    exit_ms = int(time.time() * 1000)
    held = (exit_ms - entry_ms) / 3_600_000
    pnl_pct = pnl / pos.size_usd if pos.size_usd else 0.0
    discord.send(
        pos.market_id,
        (
            f"{'✅' if pnl >= 0 else '❌'} **TURTLEQUANT EXITED** `{pos.asset.upper()} {pos.outcome}`\n"
            f"> {pos.question[:180]}\n"
            f"> Entry: {pos.entry_price:.3f} → Exit: **{exit_price:.3f}**\n"
            f"> P&L: **${pnl:+.2f}** ({pnl_pct:+.1%}) | Fees included\n"
            f"> Held: {held:.1f}h | Reason: `{reason}`"
        ),
        trade_chart(discord, pos, entry_ms, exit_ms, exit_price=exit_price, pnl=pnl),
    )


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
    parser.add_argument(
        "--paper", action="store_true", help="Paper trading mode (safe default)"
    )
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="Paper trade while recording executable CLOB bid/ask",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Live CLOB trading with fill and partial-fill handling",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Evaluate signals only; no orders"
    )
    parser.add_argument(
        "--i-accept-live-risk",
        action="store_true",
        help="Required with --live before real CLOB orders are sent",
    )
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
        "--calibration-rmse",
        type=float,
        default=float(os.getenv("CALIBRATION_RMSE", str(DEFAULT_CALIBRATION_RMSE))),
        metavar="FLOAT",
        help="One-sigma model calibration error deducted before entry",
    )
    parser.add_argument(
        "--max-daily-loss",
        type=float,
        default=float(os.getenv("MAX_DAILY_LOSS", "50")),
        metavar="USD",
        help="Block new entries after this much realised UTC-day loss",
    )
    parser.add_argument(
        "--max-market-data-age-secs",
        type=float,
        default=float(os.getenv("MAX_MARKET_DATA_AGE_SECS", "90")),
        metavar="SECONDS",
        help="Block entries when scan data is older than this",
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
        "--max-spread",
        type=float,
        default=float(os.getenv("MAX_SPREAD", "0.03")),
        metavar="PRICE",
        help="Max absolute bid-ask spread in price units (0.03 = 3 cents)",
    )
    parser.add_argument(
        "--pricing-model",
        choices=("legacy", "smile"),
        default=os.getenv("PRICING_MODEL", "legacy"),
        help="legacy: N(d2)/reflection at the strike IV with spot and 5%% drift; "
        "smile: Deribit forward, zero drift, plus the smile's skew term",
    )
    parser.add_argument(
        "--sides",
        default=os.getenv("SIDES", "yes"),
        help="Outcome tokens to buy: 'yes' (default) or 'yes,no' to also buy NO when the model is below the market",
    )
    parser.add_argument(
        "--max-iv-age-secs",
        type=float,
        default=float(os.getenv("MAX_IV_AGE_SECS", "0")),
        metavar="SECONDS",
        help="Ignore Deribit IV older than this and block entries (0 = no limit)",
    )
    parser.add_argument(
        "--marks-interval-secs",
        type=float,
        default=float(os.getenv("MARKS_INTERVAL_SECS", "900")),
        metavar="SECONDS",
        help="Snapshot every priced market's quote and model probabilities this often (0 = off)",
    )
    # Strategy knobs; defaults are the values that used to be hard-coded.
    for flag, env, default, help_text in (
        ("--min-entry-price", "MIN_ENTRY_PRICE", 0.02, "Skip entries whose side's mid is at or below this"),
        ("--max-entry-price", "MAX_ENTRY_PRICE", 0.98, "Skip entries whose side's mid is at or above this"),
        ("--reentry-cooldown-hours", "REENTRY_COOLDOWN_HOURS", 2.0, "No re-entry this soon after a full close"),
        ("--edge-decay-ratio", "EDGE_DECAY_RATIO", 0.4, "Exit when edge falls to this fraction of entry edge"),
        ("--cleanup-hours", "CLEANUP_HOURS", 6.0, "Time-cleanup window before expiry"),
        ("--cleanup-edge", "CLEANUP_EDGE", 0.05, "Time-cleanup exits when edge is at or below this"),
        ("--max-per-market-pct", "MAX_PER_MARKET_PCT", 0.10, "Per-market cap as a fraction of NAV"),
        ("--max-per-expiry-pct", "MAX_PER_EXPIRY_PCT", 0.15, "Per-expiry-date cap as a fraction of NAV"),
        ("--max-total-exposure-pct", "MAX_TOTAL_EXPOSURE_PCT", 0.40, "Total exposure cap as a fraction of NAV"),
        ("--max-asset-exposure-pct", "MAX_ASSET_EXPOSURE_PCT", 0.0, "Gross USD cap per asset as a fraction of NAV (0 = off)"),
        ("--max-asset-delta-pct", "MAX_ASSET_DELTA_PCT", 0.0,
         "Net dollar-delta cap per asset (shares * dp/dS * S) as a fraction of NAV (0 = off)"),
        ("--kelly-shrink", "KELLY_SHRINK", 1.0, "Kelly sizes on w*model + (1-w)*mid (1 = raw model)"),
    ):
        parser.add_argument(
            flag, type=float, default=float(os.getenv(env, str(default))), metavar="FLOAT", help=help_text
        )
    args = parser.parse_args()

    # Validate mode
    if (
        sum(
            1
            for enabled in (args.paper, args.shadow, args.live, args.dry_run)
            if enabled
        )
        > 1
    ):
        logger.error("Choose only one of --paper, --shadow, --live, or --dry-run")
        sys.exit(1)
    if args.live and not args.i_accept_live_risk:
        logger.error("Live trading requires --i-accept-live-risk")
        sys.exit(1)
    if args.live:
        logger.error("Live trading is disabled pending supervised broker acceptance")
        sys.exit(1)
    execution_mode = "live" if args.live else "shadow" if args.shadow else "paper"

    # Parse assets
    assets = [a.strip().lower() for a in args.asset.split(",") if a.strip()]
    for a in assets:
        if a not in ASSET_TO_SYMBOL:
            logger.error("Unknown asset: %s. Valid: btc,eth,sol,xrp", a)
            sys.exit(1)

    sides = tuple(s.strip().upper() for s in args.sides.split(",") if s.strip())
    if not sides or any(s not in ("YES", "NO") for s in sides):
        logger.error("--sides must be 'yes' or 'yes,no' (got %r)", args.sides)
        sys.exit(1)

    # State directory
    state_dir = args.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    # Components
    scanner = MarketScanner(
        min_liquidity=args.min_liquidity,
        max_spread=args.max_spread,
        assets=assets,
    )
    vol_surfaces: dict[str, VolSurface] = {
        a: VolSurface(asset=a, max_age_secs=args.max_iv_age_secs) for a in assets
    }

    # Dry-run evaluates signals against the real state but never writes it:
    # no position/NAV/risk saves, no history events, no intent journal.
    persist = not args.dry_run
    pos_mgr = PositionManager(
        starting_nav=args.starting_nav,
        kelly_fraction=args.kelly_fraction,
        max_per_market_pct=args.max_per_market_pct,
        max_per_expiry_pct=args.max_per_expiry_pct,
        max_total_exposure_pct=args.max_total_exposure_pct,
        edge_decay_ratio=args.edge_decay_ratio,
        cleanup_hours=args.cleanup_hours,
        cleanup_edge=args.cleanup_edge,
        positions_file=state_dir / "turtlequant-positions.json",
        persist=persist,
    )
    risk_controls = RiskControls.load(state_dir, pos_mgr.current_nav, persist=persist)
    executor = ExecutionClient.from_env(
        mode=execution_mode, allow_live=args.i_accept_live_risk
    )
    set_corpus_file(state_dir / "unclassified_markets.jsonl" if persist else None)

    intent_ledger = (
        OrderIntentLedger(state_dir / "turtlequant-order-intents.sqlite3")
        if execution_mode == "live"
        else None
    )
    if intent_ledger is not None and intent_ledger.outstanding():
        try:
            reconcile_outstanding(intent_ledger, executor, pos_mgr)
        except ReconciliationError as exc:
            logger.error(
                "Live trading blocked: unresolved order intent: %s. Check the order at the "
                "broker, then resolve it with scripts/order_intents.py.",
                exc,
            )
            sys.exit(1)
    discord = DiscordTrades(state_dir, execution_mode)
    notifier = NotificationQueue()

    trader = Trader(
        TraderConfig(
            execution_mode=execution_mode,
            assets=tuple(assets),
            entry_threshold=args.entry_threshold,
            calibration_rmse=args.calibration_rmse,
            max_daily_loss=args.max_daily_loss,
            max_market_data_age_secs=args.max_market_data_age_secs,
            dry_run=args.dry_run,
            min_entry_price=args.min_entry_price,
            max_entry_price=args.max_entry_price,
            reentry_cooldown_secs=args.reentry_cooldown_hours * 3600,
            pricing_model=args.pricing_model,
            marks_interval_secs=args.marks_interval_secs,
            max_asset_exposure_pct=args.max_asset_exposure_pct,
            max_asset_delta_pct=args.max_asset_delta_pct,
            kelly_shrink=args.kelly_shrink,
            sides=sides,
        ),
        state_dir=state_dir,
        scanner=scanner,
        vol_surfaces=vol_surfaces,
        positions=pos_mgr,
        risk=risk_controls,
        executor=executor,
        intents=intent_ledger,
        notify_entry=lambda pos, **kw: notifier.submit(notify_entry, discord, pos, **kw),
        notify_exit=lambda pos, price, pnl, reason: notifier.submit(
            notify_exit, discord, pos, price, pnl, reason
        ),
        running=lambda: running,
    )

    logger.info("=== TurtleQuant Bot ===")
    logger.info(
        "Mode        : %s%s",
        execution_mode.upper(),
        " (dry-run)" if args.dry_run else "",
    )
    logger.info("Assets      : %s", ", ".join(a.upper() for a in assets))
    logger.info(
        "Entry thresh: %.3f (%.1f%%)", args.entry_threshold, args.entry_threshold * 100
    )
    logger.info("Kelly frac  : %.2f", args.kelly_fraction)
    logger.info("Pricing     : %s", args.pricing_model)
    logger.info("Sides       : %s", ",".join(sides))
    logger.info("Starting NAV: $%.2f", args.starting_nav)
    logger.info("State dir   : %s", state_dir)
    logger.info("")

    last_scan_time = 0.0
    last_reprice_time = 0.0
    while running:
        now = time.time()
        if now - last_reprice_time >= REPRICE_INTERVAL_SECS:
            trader.reprice_positions()
            last_reprice_time = now
        if now - last_scan_time >= SCAN_INTERVAL_SECS:
            last_scan_time = now
            trader.scan()
        time.sleep(5)

    notifier.close()
    logger.info("TurtleQuant bot stopped.")

if __name__ == "__main__":
    main()
