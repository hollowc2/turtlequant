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
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from turtlequant.discord_trades import DiscordTrades
from turtlequant.data.binance import fetch_klines, fetch_latest_closes
from turtlequant.clob_execution import ExecutionClient, estimate_buy_fill, taker_fee
from turtlequant.market_parser import parse_market
from turtlequant.market_scanner import MarketScanner
from turtlequant.notifications import NotificationQueue
from turtlequant.order_intents import OrderIntentLedger
from turtlequant.order_reconciliation import ReconciliationError, reconcile_outstanding
from turtlequant.position_manager import PositionManager, make_position
from turtlequant.probability_engine import compute_probability
from turtlequant.history import append_history
from turtlequant.risk_controls import RiskControls
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

ASSET_TO_SYMBOL: dict[str, str] = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}

DEFAULT_ENTRY_THRESHOLD = 0.05  # 5% minimum edge
DEFAULT_KELLY_FRACTION = 0.25
DEFAULT_STARTING_NAV = 1000.0
DEFAULT_CALIBRATION_RMSE = 0.05
DEFAULT_STATE_DIR = Path("state/turtlequant")

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
# History tracking
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
    interval = os.getenv("DISCORD_CHART_INTERVAL", "4h")
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
            bought_side="YES",
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
            f"🐢 **TURTLEQUANT ENTERED** `{pos.asset.upper()} YES`\n"
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
            f"{'✅' if pnl >= 0 else '❌'} **TURTLEQUANT EXITED** `{pos.asset.upper()} YES`\n"
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
        "--max-spread-pct",
        type=float,
        default=float(os.getenv("MAX_SPREAD_PCT", "0.03")),
        metavar="FLOAT",
        help="Max bid-ask spread (as fraction of price)",
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
    risk_controls = RiskControls.load(state_dir, pos_mgr.current_nav)
    executor = ExecutionClient.from_env(
        mode=execution_mode, allow_live=args.i_accept_live_risk
    )
    intent_ledger = OrderIntentLedger(state_dir / "turtlequant-order-intents.sqlite3")
    if execution_mode == "live" and intent_ledger.outstanding():
        try:
            reconcile_outstanding(intent_ledger, executor, pos_mgr)
        except ReconciliationError as exc:
            logger.error("Live trading blocked: unresolved order intent: %s", exc)
            sys.exit(1)
    discord = DiscordTrades(state_dir, execution_mode)
    notifier = NotificationQueue()

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
    logger.info("Starting NAV: $%.2f", args.starting_nav)
    logger.info("State dir   : %s", state_dir)
    logger.info("")

    last_scan_time = 0.0
    last_reprice_time = 0.0
    recently_closed: dict[
        str, datetime
    ] = {}  # market_id → close time (cooldown tracker)

    while running:
        now = time.time()

        # ── Reprice open positions every 30s ─────────────────────────────────
        if now - last_reprice_time >= REPRICE_INTERVAL_SECS:
            latest_spots = fetch_latest_closes(
                [ASSET_TO_SYMBOL[pos.asset] for pos in pos_mgr.all_positions()]
            )
            position_spots = {
                asset: latest_spots.get(symbol)
                for asset, symbol in ASSET_TO_SYMBOL.items()
            }
            for pos in pos_mgr.all_positions():
                try:
                    # Auto-close positions whose expiry has passed
                    if datetime.now(UTC) >= pos.expiry:
                        # Expiry is not settlement. Keep the claim accounted for
                        # until Gamma explicitly confirms its final resolution.
                        resolved_price = scanner.fetch_resolution(pos.market_id)
                        if resolved_price is None:
                            logger.warning(
                                "[EXPIRED_PENDING] Awaiting confirmed resolution for %s",
                                pos.market_id[:16],
                            )
                            continue
                        logger.info(
                            "[PENDING_REDEMPTION] %s K=%.0f exp=%s resolved=%.4f",
                            pos.asset.upper(),
                            pos.strike,
                            pos.expiry_iso[:10],
                            resolved_price,
                        )
                        pos_mgr.mark_pending_redemption(pos.market_id, resolved_price)
                        append_history(
                            state_dir,
                            {
                                "event": "pending_redemption",
                                "market_id": pos.market_id,
                                "asset": pos.asset,
                                "strike": pos.strike,
                                "resolution_price": resolved_price,
                                "ts": datetime.now(UTC).isoformat(),
                            },
                        )
                        continue

                    spot = position_spots.get(pos.asset)
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

                    yes_price = scanner.fetch_market_price(pos.market_id)
                    book = executor.get_order_book(
                        pos.yes_token_id,
                        fallback_bid=yes_price or pos.last_bid or pos.last_yes_price,
                        fallback_ask=yes_price or pos.last_ask or pos.last_yes_price,
                    )
                    if yes_price is None:
                        yes_price = (
                            pos.last_yes_price
                            if pos.last_yes_price > 0
                            else pos.entry_price
                        )
                    else:
                        pos_mgr.record_market_data(
                            pos.market_id,
                            yes_price=yes_price,
                            bid=book.best_bid,
                            ask=book.best_ask,
                            observed_at=datetime.now(UTC),
                        )

                    executable_exit_price = book.best_bid or yes_price
                    decision = pos_mgr.exit_decision(
                        pos.market_id,
                        model_prob,
                        executable_exit_price,
                        now=datetime.now(UTC),
                    )
                    if decision.should_exit:
                        exit_result = None
                        if not args.dry_run:
                            shares = (
                                pos.token_size
                                if pos.token_size > 0
                                else pos.size_usd / pos.entry_price
                            )
                            exit_intent_id = (
                                intent_ledger.pending(pos.market_id, pos.yes_token_id, "SELL", shares)
                                if execution_mode == "live" else None
                            )
                            exit_result = executor.sell_yes(
                                pos.yes_token_id, shares, book
                            )
                            if exit_intent_id is not None:
                                intent_ledger.submitted(exit_intent_id, exit_result.order_id, exit_result.raw)
                            append_history(
                                state_dir,
                                {
                                    "event": "order",
                                    "market_id": pos.market_id,
                                    "asset": pos.asset,
                                    "reason": decision.reason or "edge_reversed",
                                    **exit_result.to_history(),
                                    "ts": datetime.now(UTC).isoformat(),
                                },
                            )
                            if not exit_result.success:
                                risk_controls.record_failure(
                                    exit_result.error or exit_result.status
                                )
                                append_history(
                                    state_dir,
                                    {
                                        "event": "failed_order",
                                        "market_id": pos.market_id,
                                        "asset": pos.asset,
                                        "side": "SELL",
                                        "reason": decision.reason or "edge_reversed",
                                        "remaining_shares": shares,
                                        "remaining_size_usd": pos.size_usd,
                                        "unhedged_exposure": True,
                                        "error": exit_result.error
                                        or exit_result.status,
                                        "ts": datetime.now(UTC).isoformat(),
                                    },
                                )
                                continue
                        filled_price = (
                            exit_result.avg_price
                            if exit_result and exit_result.avg_price > 0
                            else executable_exit_price
                        )
                        filled_shares = (
                            exit_result.filled_shares if exit_result else None
                        )
                        _pos, pnl = pos_mgr.close_position(
                            pos.market_id,
                            exit_price=filled_price,
                            reason=decision.reason or "edge_reversed",
                            filled_shares=filled_shares,
                        )
                        if exit_result is not None and execution_mode == "live":
                            intent_ledger.reconcile(exit_intent_id)
                        risk_controls.record_realized_pnl(pnl)
                        risk_controls.record_success(pos_mgr.marked_equity())
                        if exit_result is None or exit_result.complete:
                            recently_closed[pos.market_id] = datetime.now(UTC)
                        append_history(
                            state_dir,
                            {
                                "event": "close"
                                if exit_result is None or exit_result.complete
                                else "partial_close",
                                "market_id": pos.market_id,
                                "asset": pos.asset,
                                "strike": pos.strike,
                                "reason": decision.reason or "edge_reversed",
                                "model_prob": model_prob,
                                "yes_price": filled_price,
                                "bid": book.best_bid,
                                "ask": book.best_ask,
                                "current_edge": decision.current_edge,
                                "entry_edge": decision.entry_edge,
                                "hours_to_expiry": decision.hours_to_expiry,
                                "filled_shares": filled_shares,
                                "remaining_shares": (
                                    max(0.0, shares - filled_shares)
                                    if filled_shares is not None
                                    else 0.0
                                ),
                                "complete": True
                                if exit_result is None
                                else exit_result.complete,
                                "pnl": pnl,
                                "ts": datetime.now(UTC).isoformat(),
                            },
                        )
                        if (
                            _pos
                            and _pos.fill_confirmed
                            and (exit_result is None or exit_result.complete)
                        ):
                            notifier.submit(
                                notify_exit,
                                discord,
                                _pos,
                                filled_price,
                                pnl,
                                decision.reason or "edge_reversed",
                            )
                    else:
                        logger.info(
                            "[HOLD] %s K=%.0f exp=%s model_p=%.4f mkt_p=%.4f edge=%.4f entry_edge=%.4f ttl=%.1fh",
                            pos.asset.upper(),
                            pos.strike,
                            pos.expiry_iso[:10],
                            model_prob,
                            executable_exit_price,
                            decision.current_edge,
                            decision.entry_edge,
                            decision.hours_to_expiry or 0.0,
                        )
                except Exception as exc:
                    logger.warning("Reprice failed for %s: %s", pos.market_id[:16], exc)
                    risk_controls.record_failure(str(exc))

            last_reprice_time = now

        # ── Full market scan every 60s ────────────────────────────────────────
        if now - last_scan_time >= SCAN_INTERVAL_SECS:
            last_scan_time = now
            try:
                markets = scanner.get_active_markets()
                logger.info("Scan: %d markets found", len(markets))
            except Exception as exc:
                logger.warning("Market scan failed: %s", exc)
                risk_controls.record_failure(str(exc))
                time.sleep(5)
                continue

            scan_started_at = datetime.now(UTC)
            scan_stats: dict[str, object] = {
                "event": "scan_summary",
                "markets_passed_filters": len(markets),
                "parse_attempted": 0,
                "parsed_markets": 0,
                "unclassified_markets": 0,
                "asset_skipped": 0,
                "spot_missing": 0,
                "vol_sources": {},
                "mid_edge_candidates": 0,
                "executable_edge_candidates": 0,
                "ask_erased_edge": 0,
                "book_sources": {},
                "ts": scan_started_at.isoformat(),
            }

            def _inc_scan_stat(key: str, subkey: str | None = None) -> None:
                if subkey is None:
                    scan_stats[key] = int(scan_stats.get(key, 0)) + 1
                    return
                bucket = scan_stats.setdefault(key, {})
                if isinstance(bucket, dict):
                    bucket[subkey] = int(bucket.get(subkey, 0)) + 1

            # Independent asset marks are bounded and fetched concurrently.
            latest_spots = fetch_latest_closes(ASSET_TO_SYMBOL[a] for a in assets)
            spots = {asset: latest_spots.get(ASSET_TO_SYMBOL[asset]) for asset in assets}

            for market in markets:
                if not running:
                    break
                try:
                    _inc_scan_stat("parse_attempted")
                    params = parse_market(market.question, market.resolution_time)
                    if params is None:
                        _inc_scan_stat("unclassified_markets")
                        continue
                    _inc_scan_stat("parsed_markets")
                    if params.asset not in assets:
                        _inc_scan_stat("asset_skipped")
                        continue

                    spot = spots.get(params.asset)
                    if spot is None or spot <= 0:
                        _inc_scan_stat("spot_missing")
                        continue

                    vs = vol_surfaces[params.asset]
                    sigma = vs.get_iv(spot, params.strike, params.expiry)
                    vol_source = vs.last_source
                    _inc_scan_stat("vol_sources", vol_source)
                    model_prob = compute_probability(params, spot, sigma)
                    yes_price = market.yes_price
                    edge = model_prob - yes_price

                    if pos_mgr.has_position(market.market_id):
                        pos_mgr.record_market_data(
                            market.market_id,
                            yes_token_id=market.yes_token_id,
                            yes_price=yes_price,
                            bid=market.bid,
                            ask=market.ask,
                            observed_at=datetime.now(UTC),
                        )

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
                        book = executor.get_order_book(
                            market.yes_token_id,
                            fallback_bid=market.bid,
                            fallback_ask=market.ask,
                        )
                        executable_exit_price = book.best_bid or market.bid or yes_price
                        decision = pos_mgr.exit_decision(
                            market.market_id,
                            model_prob,
                            executable_exit_price,
                            now=datetime.now(UTC),
                        )
                        if decision.should_exit:
                            exit_result = None
                            if not args.dry_run:
                                pos = pos_mgr.get_position(market.market_id)
                                if pos:
                                    token_id = pos.yes_token_id or market.yes_token_id
                                    shares = (
                                        pos.token_size
                                        if pos.token_size > 0
                                        else pos.size_usd / pos.entry_price
                                    )
                                    exit_intent_id = (
                                        intent_ledger.pending(market.market_id, token_id, "SELL", shares)
                                        if execution_mode == "live" else None
                                    )
                                    exit_result = executor.sell_yes(
                                        token_id, shares, book
                                    )
                                    if exit_intent_id is not None:
                                        intent_ledger.submitted(exit_intent_id, exit_result.order_id, exit_result.raw)
                                    append_history(
                                        state_dir,
                                        {
                                            "event": "order",
                                            "market_id": market.market_id,
                                            "asset": params.asset,
                                            "reason": decision.reason
                                            or "edge_reversed",
                                            **exit_result.to_history(),
                                            "ts": datetime.now(UTC).isoformat(),
                                        },
                                    )
                                    if not exit_result.success:
                                        risk_controls.record_failure(
                                            exit_result.error or exit_result.status
                                        )
                                        append_history(
                                            state_dir,
                                            {
                                                "event": "failed_order",
                                                "market_id": market.market_id,
                                                "asset": params.asset,
                                                "side": "SELL",
                                                "reason": decision.reason
                                                or "edge_reversed",
                                                "remaining_shares": shares,
                                                "remaining_size_usd": pos.size_usd,
                                                "unhedged_exposure": True,
                                                "error": exit_result.error
                                                or exit_result.status,
                                                "ts": datetime.now(UTC).isoformat(),
                                            },
                                        )
                                        continue
                            filled_price = (
                                exit_result.avg_price
                                if exit_result and exit_result.avg_price > 0
                                else executable_exit_price
                            )
                            filled_shares = (
                                exit_result.filled_shares if exit_result else None
                            )
                            _pos, pnl = pos_mgr.close_position(
                                market.market_id,
                                exit_price=filled_price,
                                reason=decision.reason or "edge_reversed",
                                filled_shares=filled_shares,
                            )
                            if exit_result is not None and execution_mode == "live":
                                intent_ledger.reconcile(exit_intent_id)
                            risk_controls.record_realized_pnl(pnl)
                            risk_controls.record_success(pos_mgr.marked_equity())
                            if exit_result is None or exit_result.complete:
                                recently_closed[market.market_id] = datetime.now(UTC)
                            append_history(
                                state_dir,
                                {
                                    "event": "close"
                                    if exit_result is None or exit_result.complete
                                    else "partial_close",
                                    "market_id": market.market_id,
                                    "asset": params.asset,
                                    "strike": params.strike,
                                    "reason": decision.reason or "edge_reversed",
                                    "model_prob": model_prob,
                                    "yes_price": filled_price,
                                    "bid": book.best_bid,
                                    "ask": book.best_ask,
                                    "current_edge": decision.current_edge,
                                    "entry_edge": decision.entry_edge,
                                    "hours_to_expiry": decision.hours_to_expiry,
                                    "filled_shares": filled_shares,
                                    "remaining_shares": (
                                        max(0.0, shares - filled_shares)
                                        if filled_shares is not None
                                        else 0.0
                                    ),
                                    "complete": True
                                    if exit_result is None
                                    else exit_result.complete,
                                    "pnl": pnl,
                                    "ts": datetime.now(UTC).isoformat(),
                                },
                            )
                            if (
                                _pos
                                and _pos.fill_confirmed
                                and (exit_result is None or exit_result.complete)
                            ):
                                notifier.submit(
                                    notify_exit,
                                    discord,
                                    _pos,
                                    filled_price,
                                    pnl,
                                    decision.reason or "edge_reversed",
                                )
                        continue

                    # ── Check entry ───────────────────────────────────────
                    # Skip if this market was recently closed (re-entry cooldown)
                    closed_at = recently_closed.get(market.market_id)
                    if (
                        closed_at
                        and (datetime.now(UTC) - closed_at).total_seconds()
                        < REENTRY_COOLDOWN_SECS
                    ):
                        logger.debug(
                            "Cooldown active for %s — skip re-entry",
                            market.market_id[:16],
                        )
                        continue

                    entries_allowed, halt_reason = risk_controls.entries_allowed(
                        pos_mgr.marked_equity(),
                        max_daily_loss=args.max_daily_loss,
                        market_data_at=scan_started_at,
                        max_market_data_age_secs=args.max_market_data_age_secs,
                    )
                    if not entries_allowed:
                        logger.warning("[ENTRY_HALTED] %s", halt_reason)
                        continue

                    if edge < args.entry_threshold:
                        continue
                    _inc_scan_stat("mid_edge_candidates")
                    if yes_price <= 0.02 or yes_price >= 0.98:
                        continue  # near-certain markets — skip

                    if args.dry_run:
                        continue

                    # Place order
                    book = executor.get_order_book(
                        market.yes_token_id,
                        fallback_bid=market.bid,
                        fallback_ask=market.ask,
                    )
                    _inc_scan_stat("book_sources", book.source)
                    preliminary_size = pos_mgr.kelly_size(edge, model_prob, book.best_ask)
                    fill_estimate = estimate_buy_fill(book, preliminary_size)
                    fee_rate = executor.get_market_fee_rate(market.condition_id)
                    if execution_mode == "live" and fee_rate is None:
                        logger.warning("[ENTRY_REJECTED] Missing CLOB fee rate for %s", market.market_id[:16])
                        continue
                    estimated_fee = taker_fee(
                        fill_estimate.filled_shares,
                        fill_estimate.avg_price,
                        fee_rate or 0.0,
                    )
                    executable_entry_price = (
                        (fill_estimate.filled_usd + estimated_fee) / fill_estimate.filled_shares
                        if fill_estimate.filled_shares > 0
                        else 0.0
                    )
                    conservative_prob = max(
                        0.0, model_prob - 1.645 * args.calibration_rmse
                    )
                    entry_edge = conservative_prob - executable_entry_price
                    size_usd = pos_mgr.kelly_size(
                        entry_edge, conservative_prob, executable_entry_price
                    )
                    fill_estimate = estimate_buy_fill(book, size_usd)
                    if size_usd < 1.0 or not fill_estimate.complete:
                        continue
                    if not pos_mgr.has_expiry_headroom(params.expiry, size_usd):
                        logger.info("Per-expiry cap reached for %s — skip", params.expiry.strftime("%Y-%m-%d"))
                        continue
                    fill_ratio = (
                        min(1.0, fill_estimate.filled_usd / size_usd)
                        if size_usd > 0
                        else 0.0
                    )
                    append_history(
                        state_dir,
                        {
                            "event": "signal_evaluation",
                            "parsed": True,
                            "market_id": market.market_id,
                            "asset": params.asset,
                            "strike": params.strike,
                            "expiry": params.expiry.isoformat(),
                            "option_type": params.option_type.value,
                            "model_prob": model_prob,
                            "conservative_prob": conservative_prob,
                            "mid_price": yes_price,
                            "executable_price": executable_entry_price,
                            "mid_edge": edge,
                            "ask_edge": entry_edge,
                            "entry_threshold": args.entry_threshold,
                            "ask_erased_edge": entry_edge < args.entry_threshold,
                            "requested_size_usd": size_usd,
                            "estimated_fill_ratio": fill_ratio,
                            "estimated_avg_price": fill_estimate.avg_price,
                            "estimated_slippage": fill_estimate.avg_price - yes_price
                            if fill_estimate.avg_price > 0
                            else 0.0,
                            "estimated_complete": fill_estimate.complete,
                            "fee_rate": fee_rate,
                            "estimated_fee": estimated_fee,
                            "vol_source": vol_source,
                            "sigma": sigma,
                            "book_source": book.source,
                            "quote": book.to_dict(),
                            "ts": datetime.now(UTC).isoformat(),
                        },
                    )
                    quote_reason = (
                        "ask_erased_edge"
                        if entry_edge < args.entry_threshold
                        else "executable_edge"
                    )
                    append_history(
                        state_dir,
                        {
                            "event": "shadow_quote",
                            "market_id": market.market_id,
                            "asset": params.asset,
                            "model_prob": model_prob,
                            "mid_price": yes_price,
                            "bid": book.best_bid,
                            "ask": book.best_ask,
                            "edge": entry_edge,
                            "reason": quote_reason,
                            "book_source": book.source,
                            "vol_source": vol_source,
                            "quote": book.to_dict(),
                            "ts": datetime.now(UTC).isoformat(),
                        },
                    )
                    if entry_edge < args.entry_threshold:
                        _inc_scan_stat("ask_erased_edge")
                        continue
                    _inc_scan_stat("executable_edge_candidates")
                    intent_id = (
                        intent_ledger.pending(
                            market.market_id, market.yes_token_id, "BUY", size_usd,
                            {
                                "question": market.question, "asset": params.asset,
                                "strike": params.strike, "expiry_iso": params.expiry.isoformat(),
                                "option_type": params.option_type.value, "model_prob": model_prob,
                            },
                        )
                        if execution_mode == "live"
                        else None
                    )
                    entry_result = executor.buy_yes(
                        market.yes_token_id,
                        size_usd,
                        book,
                        max_price=min(0.99, model_prob - args.entry_threshold),
                    )
                    if intent_id is not None:
                        intent_ledger.submitted(
                            intent_id, entry_result.order_id, entry_result.raw
                        )
                    append_history(
                        state_dir,
                        {
                            "event": "order",
                            "market_id": market.market_id,
                            "asset": params.asset,
                            **entry_result.to_history(),
                            "ts": datetime.now(UTC).isoformat(),
                        },
                    )
                    if not entry_result.success:
                        risk_controls.record_failure(
                            entry_result.error or entry_result.status
                        )
                        append_history(
                            state_dir,
                            {
                                "event": "failed_order",
                                "market_id": market.market_id,
                                "asset": params.asset,
                                "side": "BUY",
                                "error": entry_result.error or entry_result.status,
                                "ts": datetime.now(UTC).isoformat(),
                            },
                        )
                        continue

                    pos = make_position(
                        market_id=market.market_id,
                        question=market.question,
                        asset=params.asset,
                        strike=params.strike,
                        expiry=params.expiry,
                        option_type=params.option_type.value,
                        yes_token_id=market.yes_token_id,
                        yes_price=entry_result.avg_price,
                        size_usd=entry_result.filled_usd,
                        model_prob=model_prob,
                        token_size=entry_result.filled_shares,
                    )
                    pos_mgr.open_position(pos)
                    pos_mgr.confirm_fill(
                        market.market_id,
                        entry_result.avg_price,
                        yes_token_id=market.yes_token_id,
                        size_usd=entry_result.filled_usd,
                        token_size=entry_result.filled_shares,
                        bid=book.best_bid,
                        ask=book.best_ask,
                        fee_usd=entry_result.fee_usd,
                    )
                    if intent_id is not None:
                        intent_ledger.reconcile(intent_id)
                    risk_controls.record_success(pos_mgr.marked_equity())
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
                            "yes_price": entry_result.avg_price,
                            "bid": book.best_bid,
                            "ask": book.best_ask,
                            "mid_price": yes_price,
                            "edge": entry_edge,
                            "size_usd": entry_result.filled_usd,
                            "requested_size_usd": size_usd,
                            "filled_shares": entry_result.filled_shares,
                            "complete": entry_result.complete,
                            "slippage": entry_result.avg_price - yes_price,
                            "sigma": sigma,
                            "vol_source": vol_source,
                            "book_source": book.source,
                            "yes_token_id": market.yes_token_id,
                            "fill_confirmed": True,
                            "ts": datetime.now(UTC).isoformat(),
                        },
                    )
                    notifier.submit(
                        notify_entry,
                        discord,
                        pos,
                        model_prob=model_prob,
                        bid=book.best_bid,
                        ask=book.best_ask,
                        sigma=sigma,
                    )

                except Exception as exc:
                    logger.warning(
                        "Market processing error (%s): %s", market.market_id[:16], exc
                    )
                    risk_controls.record_failure(str(exc))

            append_history(state_dir, scan_stats)

        # Sleep until next event
        time.sleep(5)

    notifier.close()
    logger.info("TurtleQuant bot stopped.")


if __name__ == "__main__":
    main()
