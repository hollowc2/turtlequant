#!/usr/bin/env python3
"""Prometheus exporter for the TurtleQuant trading bot.

Reads JSON state files from STATE_DIR and exposes per-strategy metrics:

Portfolio gauges (from *-positions.json):
  turtlequant_nav_usd                   — current NAV in USD
  turtlequant_total_pnl_usd             — total realized P&L from positions file
  turtlequant_open_positions_count      — number of open positions
  turtlequant_total_exposure_usd        — sum of open position sizes in USD
  turtlequant_open_unrealized_pnl_usd   — mark-to-bid unrealized P&L on open positions
  turtlequant_avg_entry_slippage        — average open-entry slippage versus signal mid
  turtlequant_avg_fill_ratio            — average order fill ratio
  turtlequant_failed_orders_total       — failed-order events recorded by the bot

Trade statistics (from *-history.json close events):
  turtlequant_closed_trades_total       — all-time count of closed trades
  turtlequant_winning_trades_total      — count of trades with pnl > 0
  turtlequant_win_rate                  — fraction won (0–1)
  turtlequant_avg_pnl_per_trade_usd     — mean P&L per closed trade
  turtlequant_avg_edge_at_entry         — mean edge at entry from open events
  turtlequant_last_trade_age_sec        — seconds since last close event
  turtlequant_exit_reason_count         — count per exit reason (labeled by reason)

Per active position (labeled strategy, market_id, asset, option_type):
  turtlequant_position_size_usd
  turtlequant_position_edge_at_entry
  turtlequant_position_age_hours
  turtlequant_position_model_prob_at_entry

Recent closed trades — last 20 (labeled strategy, market_id, asset, reason):
  turtlequant_closed_position_pnl_usd

Labels: strategy ("turtlequant")

Usage:
    python grafana_exporter.py [--state-dir DIR] [--port PORT]
"""

import argparse
import json
import logging
import os
import statistics
import time
from datetime import datetime, timezone
from http.server import HTTPServer

from prometheus_client import REGISTRY, MetricsHandler
from prometheus_client.core import GaugeMetricFamily

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

STATE_DIR = os.environ.get("STATE_DIR", "/opt/turtlequant/state")
PORT = int(os.environ.get("EXPORTER_PORT", "8004"))

# Strategy -> (positions file, history file) relative to STATE_DIR
STRATEGIES = {
    "turtlequant": (
        "turtlequant-positions.json",
        "turtlequant-history.json",
    ),
}

BOT_LOG_FILES = {
    "turtlequant": "turtlequant-bot.log",
}

# Keep this aligned with turtlequant.position_manager.TAKER_FEE_RATE. The
# exporter uses it only to normalize legacy history rows that recorded flat
# closes as zero before fee-adjusted P&L was persisted.
TAKER_FEE_RATE = 0.003


def _load_json(path: str) -> object:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read %s: %s", path, e)
        return None


def _parse_ts(ts_str: str) -> float | None:
    """Parse ISO 8601 timestamp string to epoch seconds."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _history_source(event: dict) -> str | None:
    quote = event.get("quote")
    if not isinstance(quote, dict):
        return None
    source = quote.get("source")
    if source is None:
        return None
    return str(source) or "unknown"


def _is_fallback_source(source: str) -> bool:
    normalized = source.lower()
    return "fallback" in normalized or normalized in {"default", "proxy", "realized", "unknown"}


def _is_synthetic_book_source(source: str) -> bool:
    return source.lower() in {"synthetic", "gamma"}


def _effective_close_events(history_events: list[dict]) -> list[dict]:
    """Return close events with fee-adjusted P&L for legacy zero-P&L rows."""
    open_queues: dict[str, list[dict]] = {}
    closes: list[dict] = []

    for event in history_events:
        market_id = str(event.get("market_id", ""))
        if event.get("event") == "open":
            open_queues.setdefault(market_id, []).append(event)
            continue
        if event.get("event") != "close":
            continue

        close_event = dict(event)
        recorded_pnl = _safe_float(close_event.get("pnl"))
        matching_open = None
        queue = open_queues.get(market_id)
        if queue:
            matching_open = queue.pop(0)
            close_event["_opened_ts"] = matching_open.get("ts")

        if matching_open is not None and recorded_pnl == 0.0:
            entry_price = _safe_float(matching_open.get("yes_price"))
            exit_price = _safe_float(close_event.get("yes_price", close_event.get("exit_price")))
            size_usd = _safe_float(matching_open.get("size_usd"))
            if entry_price > 0 and exit_price >= 0 and size_usd > 0:
                tokens = size_usd / entry_price
                entry_fee = size_usd * TAKER_FEE_RATE
                exit_fee = tokens * exit_price * TAKER_FEE_RATE
                close_event["_effective_pnl"] = (exit_price - entry_price) * tokens - entry_fee - exit_fee
            else:
                close_event["_effective_pnl"] = recorded_pnl
        else:
            close_event["_effective_pnl"] = recorded_pnl

        closes.append(close_event)

    return closes


def _file_age_sec(path: str) -> float | None:
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def _equity_points(close_events: list[dict], nav: float | None, total_pnl: float | None) -> list[tuple[float, float]]:
    """Build a realized-equity series from close events."""
    start_nav = (float(nav) - float(total_pnl)) if nav is not None and total_pnl is not None else 1000.0
    points: list[tuple[float, float]] = []
    equity = start_nav
    for event in close_events:
        ts = _parse_ts(event.get("ts"))
        if ts is None:
            continue
        if not points:
            points.append((ts - 1.0, equity))
        equity += _safe_float(event.get("_effective_pnl", event.get("pnl")))
        points.append((ts, equity))
    return points or [(time.time(), start_nav)]


def _drawdown_stats(points: list[tuple[float, float]]) -> dict[str, float]:
    peak = points[0][1]
    peak_ts = points[0][0]
    current_dd_usd = 0.0
    current_dd_pct = 0.0
    max_dd_usd = 0.0
    max_dd_pct = 0.0
    longest_dd = 0.0
    max_recovery = 0.0
    dd_start_ts: float | None = None
    max_dd_peak_ts: float | None = None
    max_dd_recovered = False

    for ts, equity in points:
        if equity >= peak:
            if dd_start_ts is not None:
                longest_dd = max(longest_dd, ts - dd_start_ts)
                if max_dd_peak_ts is not None and not max_dd_recovered:
                    max_recovery = ts - max_dd_peak_ts
                    max_dd_recovered = True
                dd_start_ts = None
            peak = equity
            peak_ts = ts
            continue

        if dd_start_ts is None:
            dd_start_ts = peak_ts
        dd_usd = peak - equity
        dd_pct = dd_usd / peak if peak > 0 else 0.0
        current_dd_usd = dd_usd
        current_dd_pct = dd_pct
        if dd_usd > max_dd_usd:
            max_dd_usd = dd_usd
            max_dd_pct = dd_pct
            max_dd_peak_ts = peak_ts
            max_dd_recovered = False

    if dd_start_ts is not None:
        longest_dd = max(longest_dd, points[-1][0] - dd_start_ts)

    return {
        "current_usd": current_dd_usd,
        "current_pct": current_dd_pct,
        "max_usd": max_dd_usd,
        "max_pct": max_dd_pct,
        "longest_sec": longest_dd,
        "max_recovery_sec": max_recovery,
    }


class TurtleQuantCollector:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir

    def collect(self):
        # --- Portfolio gauges ---
        nav_g = GaugeMetricFamily(
            "turtlequant_nav_usd",
            "Current NAV in USD",
            labels=["strategy"],
        )
        total_pnl_g = GaugeMetricFamily(
            "turtlequant_total_pnl_usd",
            "Total realized P&L in USD (from positions file)",
            labels=["strategy"],
        )
        open_pos_g = GaugeMetricFamily(
            "turtlequant_open_positions_count",
            "Number of open positions",
            labels=["strategy"],
        )
        exposure_g = GaugeMetricFamily(
            "turtlequant_total_exposure_usd",
            "Sum of open position sizes in USD",
            labels=["strategy"],
        )
        exposure_by_asset_g = GaugeMetricFamily(
            "turtlequant_open_exposure_by_asset_usd",
            "Sum of open position sizes in USD by asset",
            labels=["strategy", "asset"],
        )
        open_unrealized_pnl_g = GaugeMetricFamily(
            "turtlequant_open_unrealized_pnl_usd",
            "Open unrealized P&L marked to last executable bid",
            labels=["strategy"],
        )
        open_unrealized_pnl_by_asset_g = GaugeMetricFamily(
            "turtlequant_open_unrealized_pnl_by_asset_usd",
            "Open unrealized P&L marked to last executable bid by asset",
            labels=["strategy", "asset"],
        )
        avg_entry_slippage_g = GaugeMetricFamily(
            "turtlequant_avg_entry_slippage",
            "Average open-entry slippage versus signal mid price",
            labels=["strategy"],
        )
        avg_fill_ratio_g = GaugeMetricFamily(
            "turtlequant_avg_fill_ratio",
            "Average order fill ratio across recorded order events",
            labels=["strategy"],
        )
        failed_orders_g = GaugeMetricFamily(
            "turtlequant_failed_orders_total",
            "Count of failed order events",
            labels=["strategy", "side"],
        )
        order_count_g = GaugeMetricFamily(
            "turtlequant_orders_total",
            "Count of order events",
            labels=["strategy", "side", "status"],
        )
        largest_position_pct_nav_g = GaugeMetricFamily(
            "turtlequant_largest_position_pct_nav",
            "Largest open position as a fraction of NAV",
            labels=["strategy"],
        )
        current_drawdown_usd_g = GaugeMetricFamily(
            "turtlequant_current_drawdown_usd",
            "Current realized drawdown in USD",
            labels=["strategy"],
        )
        current_drawdown_pct_g = GaugeMetricFamily(
            "turtlequant_current_drawdown_pct",
            "Current realized drawdown as a fraction of peak NAV",
            labels=["strategy"],
        )
        max_drawdown_usd_g = GaugeMetricFamily(
            "turtlequant_max_drawdown_usd",
            "Maximum realized drawdown in USD",
            labels=["strategy"],
        )
        max_drawdown_pct_g = GaugeMetricFamily(
            "turtlequant_max_drawdown_pct",
            "Maximum realized drawdown as a fraction of peak NAV",
            labels=["strategy"],
        )
        longest_drawdown_g = GaugeMetricFamily(
            "turtlequant_longest_drawdown_duration_sec",
            "Longest realized drawdown duration in seconds",
            labels=["strategy"],
        )
        max_drawdown_recovery_g = GaugeMetricFamily(
            "turtlequant_max_drawdown_recovery_sec",
            "Recovery time from the maximum realized drawdown in seconds; 0 if unrecovered or no drawdown",
            labels=["strategy"],
        )

        # --- Trade statistics ---
        closed_trades_g = GaugeMetricFamily(
            "turtlequant_closed_trades_total",
            "All-time count of closed trades",
            labels=["strategy"],
        )
        winning_trades_g = GaugeMetricFamily(
            "turtlequant_winning_trades_total",
            "Count of closed trades with positive P&L",
            labels=["strategy"],
        )
        win_rate_g = GaugeMetricFamily(
            "turtlequant_win_rate",
            "Fraction of closed trades with positive P&L (0–1)",
            labels=["strategy"],
        )
        avg_pnl_g = GaugeMetricFamily(
            "turtlequant_avg_pnl_per_trade_usd",
            "Mean P&L per closed trade in USD",
            labels=["strategy"],
        )
        profit_factor_g = GaugeMetricFamily(
            "turtlequant_profit_factor",
            "Gross winning P&L divided by absolute gross losing P&L",
            labels=["strategy"],
        )
        avg_win_g = GaugeMetricFamily(
            "turtlequant_avg_win_usd",
            "Mean P&L of winning closed trades in USD",
            labels=["strategy"],
        )
        avg_loss_g = GaugeMetricFamily(
            "turtlequant_avg_loss_usd",
            "Mean P&L of losing closed trades in USD",
            labels=["strategy"],
        )
        expectancy_g = GaugeMetricFamily(
            "turtlequant_expectancy_usd",
            "Expected P&L per trade in USD",
            labels=["strategy"],
        )
        best_trade_g = GaugeMetricFamily(
            "turtlequant_best_trade_pnl_usd",
            "Best closed-trade P&L in USD",
            labels=["strategy"],
        )
        worst_trade_g = GaugeMetricFamily(
            "turtlequant_worst_trade_pnl_usd",
            "Worst closed-trade P&L in USD",
            labels=["strategy"],
        )
        median_trade_g = GaugeMetricFamily(
            "turtlequant_median_trade_pnl_usd",
            "Median closed-trade P&L in USD",
            labels=["strategy"],
        )
        pnl_by_asset_g = GaugeMetricFamily(
            "turtlequant_closed_pnl_by_asset_usd",
            "Realized closed-trade P&L by asset in USD",
            labels=["strategy", "asset"],
        )
        pnl_by_weekday_g = GaugeMetricFamily(
            "turtlequant_closed_pnl_by_weekday_usd",
            "Realized closed-trade P&L by weekday in USD",
            labels=["strategy", "weekday"],
        )
        avg_edge_g = GaugeMetricFamily(
            "turtlequant_avg_edge_at_entry",
            "Mean edge (model_prob - yes_price) at entry",
            labels=["strategy"],
        )
        last_trade_age_g = GaugeMetricFamily(
            "turtlequant_last_trade_age_sec",
            "Seconds since last close event",
            labels=["strategy"],
        )
        exit_reason_g = GaugeMetricFamily(
            "turtlequant_exit_reason_count",
            "Count of closed trades per exit reason",
            labels=["strategy", "reason"],
        )
        shadow_quote_g = GaugeMetricFamily(
            "turtlequant_shadow_quotes_total",
            "Count of shadow quote events by reason",
            labels=["strategy", "reason"],
        )
        ask_erased_edge_ratio_g = GaugeMetricFamily(
            "turtlequant_ask_erased_edge_ratio",
            "Fraction of shadow quote events where executable ask erased the model edge",
            labels=["strategy"],
        )
        order_book_source_g = GaugeMetricFamily(
            "turtlequant_order_book_source_total",
            "Count of history events by nested quote.source order book source",
            labels=["strategy", "source"],
        )
        order_book_source_ratio_g = GaugeMetricFamily(
            "turtlequant_order_book_source_ratio",
            "Fraction of history events with nested quote.source by order book source",
            labels=["strategy", "source"],
        )
        signal_evaluation_g = GaugeMetricFamily(
            "turtlequant_signal_evaluation_count",
            "Count of signal evaluation events by parser outcome",
            labels=["strategy", "parsed"],
        )
        parser_hit_rate_g = GaugeMetricFamily(
            "turtlequant_parser_hit_rate",
            "Fraction of scan-summary parse attempts that were classified",
            labels=["strategy"],
        )
        signal_book_source_g = GaugeMetricFamily(
            "turtlequant_signal_book_source_count",
            "Count of signal evaluation events by book_source field",
            labels=["strategy", "source"],
        )
        parser_scanner_vol_source_g = GaugeMetricFamily(
            "turtlequant_signal_vol_source_count",
            "Count of signal evaluation events by vol_source field",
            labels=["strategy", "source"],
        )
        vol_source_g = GaugeMetricFamily(
            "turtlequant_vol_source_total",
            "Count of history events by volatility source",
            labels=["strategy", "source"],
        )
        synthetic_book_ratio_g = GaugeMetricFamily(
            "turtlequant_synthetic_book_ratio",
            "Fraction of order-book source events that used synthetic fallback books",
            labels=["strategy"],
        )
        realized_vol_fallback_ratio_g = GaugeMetricFamily(
            "turtlequant_realized_vol_fallback_ratio",
            "Fraction of history events with vol_source that used a fallback volatility source",
            labels=["strategy"],
        )

        # --- Per active position ---
        pos_size_g = GaugeMetricFamily(
            "turtlequant_position_size_usd",
            "Open position size in USD",
            labels=["strategy", "market_id", "asset", "option_type"],
        )
        pos_edge_g = GaugeMetricFamily(
            "turtlequant_position_edge_at_entry",
            "Edge at entry for open position",
            labels=["strategy", "market_id", "asset", "option_type"],
        )
        pos_age_g = GaugeMetricFamily(
            "turtlequant_position_age_hours",
            "Hours since position was opened",
            labels=["strategy", "market_id", "asset", "option_type"],
        )
        pos_model_prob_g = GaugeMetricFamily(
            "turtlequant_position_model_prob_at_entry",
            "Model probability at entry for open position",
            labels=["strategy", "market_id", "asset", "option_type"],
        )

        # --- Recent closed trades (last 20) ---
        # idx label (0=oldest of the 20, 19=most recent) ensures unique label sets
        # even when the same market is traded multiple times.
        closed_pnl_g = GaugeMetricFamily(
            "turtlequant_closed_position_pnl_usd",
            "P&L of recent closed trade in USD",
            labels=["strategy", "idx", "market_id", "asset", "reason"],
        )
        closed_hold_g = GaugeMetricFamily(
            "turtlequant_closed_position_holding_hours",
            "Holding period for recent closed trade in hours",
            labels=["strategy", "idx", "market_id", "asset", "reason"],
        )
        state_file_age_g = GaugeMetricFamily(
            "turtlequant_state_file_age_sec",
            "Age of bot state files in seconds",
            labels=["strategy", "file"],
        )
        bot_log_age_g = GaugeMetricFamily(
            "turtlequant_bot_log_age_sec",
            "Age of bot log file in seconds (staleness indicates scan loop stopped)",
            labels=["strategy"],
        )
        scrape_success_g = GaugeMetricFamily(
            "turtlequant_exporter_scrape_success",
            "1 when both positions and history files were readable; 0 otherwise",
            labels=["strategy"],
        )

        for strategy, (pos_file, hist_file) in STRATEGIES.items():
            pos_path = os.path.join(self.state_dir, pos_file)
            hist_path = os.path.join(self.state_dir, hist_file)
            nav: float | None = None
            total_pnl: float | None = None
            positions: list[dict] = []

            # ---- Positions file ----
            pos_data = _load_json(pos_path)
            if isinstance(pos_data, dict):
                nav = _safe_float(pos_data.get("nav"))
                total_pnl = _safe_float(pos_data.get("total_pnl"))
                positions = pos_data.get("positions") or []

                if nav is not None:
                    nav_g.add_metric([strategy], nav)
                if total_pnl is not None:
                    total_pnl_g.add_metric([strategy], total_pnl)

                open_pos_g.add_metric([strategy], float(len(positions)))
                exposure = sum(p.get("size_usd", 0.0) for p in positions)
                exposure_g.add_metric([strategy], float(exposure))
                by_asset: dict[str, float] = {}
                unrealized_by_asset: dict[str, float] = {}
                unrealized_total = 0.0
                for pos in positions:
                    asset = str(pos.get("asset", "unknown"))
                    by_asset[asset] = by_asset.get(asset, 0.0) + _safe_float(pos.get("size_usd"))
                    tokens = _safe_float(pos.get("token_size"))
                    if tokens <= 0:
                        entry = _safe_float(pos.get("entry_price"))
                        size_usd = _safe_float(pos.get("size_usd"))
                        tokens = size_usd / entry if entry > 0 else 0.0
                    mark = _safe_float(pos.get("last_bid")) or _safe_float(pos.get("last_yes_price"))
                    entry = _safe_float(pos.get("entry_price"))
                    unrealized = (mark - entry) * tokens - (tokens * mark * TAKER_FEE_RATE if mark > 0 else 0.0)
                    unrealized_total += unrealized
                    unrealized_by_asset[asset] = unrealized_by_asset.get(asset, 0.0) + unrealized
                for asset, value in by_asset.items():
                    exposure_by_asset_g.add_metric([strategy, asset], value)
                open_unrealized_pnl_g.add_metric([strategy], unrealized_total)
                for asset, value in unrealized_by_asset.items():
                    open_unrealized_pnl_by_asset_g.add_metric([strategy, asset], value)
                largest = max((_safe_float(p.get("size_usd")) for p in positions), default=0.0)
                largest_position_pct_nav_g.add_metric([strategy], largest / nav if nav and nav > 0 else 0.0)

                now = time.time()
                for pos in positions:
                    mid = str(pos.get("market_id", ""))
                    asset = str(pos.get("asset", ""))
                    opt_type = str(pos.get("option_type", ""))
                    pos_labels = [strategy, mid, asset, opt_type]

                    size = pos.get("size_usd")
                    if size is not None:
                        pos_size_g.add_metric(pos_labels, float(size))

                    edge = pos.get("edge_at_entry")
                    if edge is not None:
                        pos_edge_g.add_metric(pos_labels, float(edge))

                    model_prob = pos.get("model_prob_at_entry")
                    if model_prob is not None:
                        pos_model_prob_g.add_metric(pos_labels, float(model_prob))

                    opened_at = _parse_ts(pos.get("opened_at"))
                    if opened_at is not None:
                        age_hours = (now - opened_at) / 3600.0
                        pos_age_g.add_metric(pos_labels, age_hours)
            pos_age = _file_age_sec(pos_path)
            if pos_age is not None:
                state_file_age_g.add_metric([strategy, "positions"], pos_age)

            log_name = BOT_LOG_FILES.get(strategy)
            if log_name:
                log_age = _file_age_sec(os.path.join(self.state_dir, log_name))
                if log_age is not None:
                    bot_log_age_g.add_metric([strategy], log_age)

            # ---- History file ----
            hist_data = _load_json(hist_path)
            if isinstance(hist_data, list):
                close_events = [e for e in hist_data if e.get("event") == "close"]
                open_events = [e for e in hist_data if e.get("event") == "open"]
                order_events = [e for e in hist_data if e.get("event") == "order"]
                failed_events = [e for e in hist_data if e.get("event") == "failed_order"]
                shadow_quote_events = [e for e in hist_data if e.get("event") == "shadow_quote"]
                signal_evaluation_events = [e for e in hist_data if e.get("event") == "signal_evaluation"]
                scan_summary_events = [e for e in hist_data if e.get("event") == "scan_summary"]
                effective_close_events = _effective_close_events(hist_data)
                hist_age = _file_age_sec(hist_path)
                if hist_age is not None:
                    state_file_age_g.add_metric([strategy, "history"], hist_age)
                scrape_success_g.add_metric([strategy], 1.0 if isinstance(pos_data, dict) else 0.0)

                equity_points = _equity_points(effective_close_events, nav, total_pnl)
                drawdown = _drawdown_stats(equity_points)
                current_drawdown_usd_g.add_metric([strategy], drawdown["current_usd"])
                current_drawdown_pct_g.add_metric([strategy], drawdown["current_pct"])
                max_drawdown_usd_g.add_metric([strategy], drawdown["max_usd"])
                max_drawdown_pct_g.add_metric([strategy], drawdown["max_pct"])
                longest_drawdown_g.add_metric([strategy], drawdown["longest_sec"])
                max_drawdown_recovery_g.add_metric([strategy], drawdown["max_recovery_sec"])

                # Trade statistics
                n_closed = len(close_events)
                closed_trades_g.add_metric([strategy], float(n_closed))

                if n_closed > 0:
                    pnls = [_safe_float(e.get("_effective_pnl", e.get("pnl"))) for e in effective_close_events]
                    wins = sum(1 for p in pnls if p > 0)
                    winning_pnls = [p for p in pnls if p > 0]
                    losing_pnls = [p for p in pnls if p < 0]
                    winning_trades_g.add_metric([strategy], float(wins))
                    win_rate_g.add_metric([strategy], wins / n_closed)
                    avg_pnl_g.add_metric([strategy], sum(pnls) / n_closed)
                    expectancy_g.add_metric([strategy], sum(pnls) / n_closed)
                    best_trade_g.add_metric([strategy], max(pnls))
                    worst_trade_g.add_metric([strategy], min(pnls))
                    median_trade_g.add_metric([strategy], statistics.median(pnls))
                    if winning_pnls:
                        avg_win_g.add_metric([strategy], sum(winning_pnls) / len(winning_pnls))
                    if losing_pnls:
                        avg_loss_g.add_metric([strategy], sum(losing_pnls) / len(losing_pnls))
                    gross_wins = sum(winning_pnls)
                    gross_losses = abs(sum(losing_pnls))
                    if gross_losses > 0:
                        profit_factor_g.add_metric([strategy], gross_wins / gross_losses)
                    elif gross_wins > 0:
                        profit_factor_g.add_metric([strategy], gross_wins)

                    # Last trade age
                    last_ts = _parse_ts(close_events[-1].get("ts"))
                    if last_ts is not None:
                        last_trade_age_g.add_metric([strategy], time.time() - last_ts)

                    # Exit reasons
                    reason_counts: dict[str, int] = {}
                    for e in close_events:
                        reason = str(e.get("reason", "unknown"))
                        reason_counts[reason] = reason_counts.get(reason, 0) + 1
                    for reason, count in reason_counts.items():
                        exit_reason_g.add_metric([strategy, reason], float(count))

                    pnl_by_asset: dict[str, float] = {}
                    pnl_by_weekday: dict[str, float] = {}
                    for e in effective_close_events:
                        pnl = _safe_float(e.get("_effective_pnl", e.get("pnl")))
                        asset = str(e.get("asset", "unknown"))
                        pnl_by_asset[asset] = pnl_by_asset.get(asset, 0.0) + pnl
                        ts = _parse_ts(e.get("ts"))
                        if ts is not None:
                            weekday = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a")
                            pnl_by_weekday[weekday] = pnl_by_weekday.get(weekday, 0.0) + pnl
                    for asset, pnl in pnl_by_asset.items():
                        pnl_by_asset_g.add_metric([strategy, asset], pnl)
                    for weekday, pnl in pnl_by_weekday.items():
                        pnl_by_weekday_g.add_metric([strategy, weekday], pnl)

                # Avg edge at entry
                edges = [e.get("edge") for e in open_events if e.get("edge") is not None]
                if edges:
                    numeric_edges = [_safe_float(e) for e in edges]
                    avg_edge_g.add_metric([strategy], sum(numeric_edges) / len(numeric_edges))

                slippages = [_safe_float(e.get("slippage")) for e in open_events if e.get("slippage") is not None]
                if slippages:
                    avg_entry_slippage_g.add_metric([strategy], sum(slippages) / len(slippages))

                fill_ratios: list[float] = []
                order_counts: dict[tuple[str, str], int] = {}
                for event in order_events:
                    side = str(event.get("side", "unknown"))
                    status = str(event.get("status", "unknown"))
                    order_counts[(side, status)] = order_counts.get((side, status), 0) + 1
                    requested_usd = _safe_float(event.get("requested_usd"))
                    requested_shares = _safe_float(event.get("requested_shares"))
                    filled_usd = _safe_float(event.get("filled_usd"))
                    filled_shares = _safe_float(event.get("filled_shares"))
                    if requested_usd > 0:
                        fill_ratios.append(min(1.0, filled_usd / requested_usd))
                    elif requested_shares > 0:
                        fill_ratios.append(min(1.0, filled_shares / requested_shares))
                if fill_ratios:
                    avg_fill_ratio_g.add_metric([strategy], sum(fill_ratios) / len(fill_ratios))
                for (side, status), count in order_counts.items():
                    order_count_g.add_metric([strategy, side, status], float(count))

                failed_counts: dict[str, int] = {}
                for event in failed_events:
                    side = str(event.get("side", "unknown"))
                    failed_counts[side] = failed_counts.get(side, 0) + 1
                for side, count in failed_counts.items():
                    failed_orders_g.add_metric([strategy, side], float(count))

                shadow_counts: dict[str, int] = {}
                for event in shadow_quote_events:
                    reason = str(event.get("reason", "unknown"))
                    shadow_counts[reason] = shadow_counts.get(reason, 0) + 1
                for reason, count in shadow_counts.items():
                    shadow_quote_g.add_metric([strategy, reason], float(count))
                if shadow_quote_events:
                    erased = shadow_counts.get("ask_erased_edge", 0)
                    ask_erased_edge_ratio_g.add_metric([strategy], erased / len(shadow_quote_events))

                book_source_counts: dict[str, int] = {}
                for event in hist_data:
                    source = _history_source(event)
                    if source is None and event.get("book_source") is not None:
                        source = str(event.get("book_source")) or "unknown"
                    if source is None:
                        continue
                    book_source_counts[source] = book_source_counts.get(source, 0) + 1
                for event in scan_summary_events:
                    sources = event.get("book_sources")
                    if not isinstance(sources, dict):
                        continue
                    for source, count in sources.items():
                        key = str(source) or "unknown"
                        book_source_counts[key] = book_source_counts.get(key, 0) + int(_safe_float(count))
                book_source_total = sum(book_source_counts.values())
                for source, count in book_source_counts.items():
                    order_book_source_g.add_metric([strategy, source], float(count))
                    if book_source_total > 0:
                        order_book_source_ratio_g.add_metric([strategy, source], count / book_source_total)
                if book_source_total > 0:
                    synthetic_count = sum(count for source, count in book_source_counts.items() if _is_synthetic_book_source(source))
                    synthetic_book_ratio_g.add_metric([strategy], synthetic_count / book_source_total)

                parsed_counts: dict[str, int] = {}
                signal_book_counts: dict[str, int] = {}
                signal_vol_counts: dict[str, int] = {}
                parse_attempted = 0
                parsed_markets = 0
                for event in scan_summary_events:
                    parse_attempted += int(_safe_float(event.get("parse_attempted")))
                    parsed_markets += int(_safe_float(event.get("parsed_markets")))
                for event in signal_evaluation_events:
                    if "parsed" in event:
                        parsed = "true" if bool(event.get("parsed")) else "false"
                        parsed_counts[parsed] = parsed_counts.get(parsed, 0) + 1
                    book_source = event.get("book_source")
                    if book_source is not None:
                        source = str(book_source) or "unknown"
                        signal_book_counts[source] = signal_book_counts.get(source, 0) + 1
                    vol_source = event.get("vol_source")
                    if vol_source is not None:
                        source = str(vol_source) or "unknown"
                        signal_vol_counts[source] = signal_vol_counts.get(source, 0) + 1
                for parsed, count in parsed_counts.items():
                    signal_evaluation_g.add_metric([strategy, parsed], float(count))
                if parse_attempted > 0:
                    parser_hit_rate_g.add_metric([strategy], parsed_markets / parse_attempted)
                for source, count in signal_book_counts.items():
                    signal_book_source_g.add_metric([strategy, source], float(count))
                for source, count in signal_vol_counts.items():
                    parser_scanner_vol_source_g.add_metric([strategy, source], float(count))

                vol_source_counts: dict[str, int] = {}
                for event in hist_data:
                    vol_source = event.get("vol_source")
                    if vol_source is None:
                        continue
                    source = str(vol_source) or "unknown"
                    vol_source_counts[source] = vol_source_counts.get(source, 0) + 1
                for event in scan_summary_events:
                    sources = event.get("vol_sources")
                    if not isinstance(sources, dict):
                        continue
                    for source, count in sources.items():
                        key = str(source) or "unknown"
                        vol_source_counts[key] = vol_source_counts.get(key, 0) + int(_safe_float(count))
                vol_source_total = sum(vol_source_counts.values())
                for source, count in vol_source_counts.items():
                    vol_source_g.add_metric([strategy, source], float(count))
                if vol_source_total > 0:
                    fallback_count = sum(count for source, count in vol_source_counts.items() if _is_fallback_source(source))
                    realized_vol_fallback_ratio_g.add_metric([strategy], fallback_count / vol_source_total)

                # Recent closed trades (last 20)
                # Use sequential idx as the unique label so the same market traded
                # multiple times doesn't produce duplicate label sets.
                for idx, e in enumerate(effective_close_events[-20:]):
                    market_id = str(e.get("market_id", ""))
                    asset = str(e.get("asset", ""))
                    reason = str(e.get("reason", "unknown"))
                    labels = [strategy, str(idx), market_id, asset, reason]
                    pnl = _safe_float(e.get("_effective_pnl", e.get("pnl")))
                    closed_pnl_g.add_metric(labels, pnl)
                    opened_ts = _parse_ts(e.get("_opened_ts"))
                    closed_ts = _parse_ts(e.get("ts"))
                    if opened_ts is not None and closed_ts is not None and closed_ts >= opened_ts:
                        closed_hold_g.add_metric(labels, (closed_ts - opened_ts) / 3600.0)
            else:
                scrape_success_g.add_metric([strategy], 0.0)

        yield nav_g
        yield total_pnl_g
        yield open_pos_g
        yield exposure_g
        yield exposure_by_asset_g
        yield open_unrealized_pnl_g
        yield open_unrealized_pnl_by_asset_g
        yield avg_entry_slippage_g
        yield avg_fill_ratio_g
        yield failed_orders_g
        yield order_count_g
        yield largest_position_pct_nav_g
        yield current_drawdown_usd_g
        yield current_drawdown_pct_g
        yield max_drawdown_usd_g
        yield max_drawdown_pct_g
        yield longest_drawdown_g
        yield max_drawdown_recovery_g
        yield closed_trades_g
        yield winning_trades_g
        yield win_rate_g
        yield avg_pnl_g
        yield profit_factor_g
        yield avg_win_g
        yield avg_loss_g
        yield expectancy_g
        yield best_trade_g
        yield worst_trade_g
        yield median_trade_g
        yield pnl_by_asset_g
        yield pnl_by_weekday_g
        yield avg_edge_g
        yield last_trade_age_g
        yield exit_reason_g
        yield shadow_quote_g
        yield ask_erased_edge_ratio_g
        yield order_book_source_g
        yield order_book_source_ratio_g
        yield signal_evaluation_g
        yield parser_hit_rate_g
        yield signal_book_source_g
        yield parser_scanner_vol_source_g
        yield vol_source_g
        yield synthetic_book_ratio_g
        yield realized_vol_fallback_ratio_g
        yield pos_size_g
        yield pos_edge_g
        yield pos_age_g
        yield pos_model_prob_g
        yield closed_pnl_g
        yield closed_hold_g
        yield state_file_age_g
        yield bot_log_age_g
        yield scrape_success_g


def main():
    parser = argparse.ArgumentParser(description="TurtleQuant Prometheus metrics exporter")
    parser.add_argument("--state-dir", default=STATE_DIR)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    REGISTRY.register(TurtleQuantCollector(args.state_dir))

    server = HTTPServer(("0.0.0.0", args.port), MetricsHandler)
    log.info("TurtleQuant Prometheus exporter listening on :%d", args.port)
    log.info("State dir: %s", args.state_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
