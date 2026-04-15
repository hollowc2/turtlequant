#!/usr/bin/env python3
"""Prometheus exporter for TurtleQuant and SlowQuant trading bots.

Reads JSON state files from STATE_DIR and exposes per-strategy metrics:

Portfolio gauges (from *-positions.json):
  turtlequant_nav_usd                   — current NAV in USD
  turtlequant_total_pnl_usd             — total realized P&L from positions file
  turtlequant_open_positions_count      — number of open positions
  turtlequant_total_exposure_usd        — sum of open position sizes in USD

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

Labels: strategy ("turtlequant" | "slowquant")

Usage:
    python grafana_exporter.py [--state-dir DIR] [--port PORT]
"""

import argparse
import json
import logging
import os
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
    "slowquant": (
        "slowquant/slowquant-positions.json",
        "slowquant/slowquant-history.json",
    ),
}


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
            labels=["strategy", "idx", "asset", "reason"],
        )

        for strategy, (pos_file, hist_file) in STRATEGIES.items():
            pos_path = os.path.join(self.state_dir, pos_file)
            hist_path = os.path.join(self.state_dir, hist_file)

            # ---- Positions file ----
            pos_data = _load_json(pos_path)
            if isinstance(pos_data, dict):
                nav = pos_data.get("nav")
                total_pnl = pos_data.get("total_pnl")
                positions = pos_data.get("positions") or []

                if nav is not None:
                    nav_g.add_metric([strategy], float(nav))
                if total_pnl is not None:
                    total_pnl_g.add_metric([strategy], float(total_pnl))

                open_pos_g.add_metric([strategy], float(len(positions)))
                exposure = sum(p.get("size_usd", 0.0) for p in positions)
                exposure_g.add_metric([strategy], float(exposure))

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

            # ---- History file ----
            hist_data = _load_json(hist_path)
            if isinstance(hist_data, list):
                close_events = [e for e in hist_data if e.get("event") == "close"]
                open_events = [e for e in hist_data if e.get("event") == "open"]

                # Trade statistics
                n_closed = len(close_events)
                closed_trades_g.add_metric([strategy], float(n_closed))

                if n_closed > 0:
                    pnls = [e.get("pnl", 0.0) for e in close_events]
                    wins = sum(1 for p in pnls if p > 0)
                    winning_trades_g.add_metric([strategy], float(wins))
                    win_rate_g.add_metric([strategy], wins / n_closed)
                    avg_pnl_g.add_metric([strategy], sum(pnls) / n_closed)

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

                # Avg edge at entry
                edges = [e.get("edge") for e in open_events if e.get("edge") is not None]
                if edges:
                    avg_edge_g.add_metric([strategy], sum(edges) / len(edges))

                # Recent closed trades (last 20)
                # Use sequential idx as the unique label so the same market traded
                # multiple times doesn't produce duplicate label sets.
                for idx, e in enumerate(close_events[-20:]):
                    asset = str(e.get("asset", ""))
                    reason = str(e.get("reason", "unknown"))
                    pnl = e.get("pnl", 0.0)
                    closed_pnl_g.add_metric([strategy, str(idx), asset, reason], float(pnl))

        yield nav_g
        yield total_pnl_g
        yield open_pos_g
        yield exposure_g
        yield closed_trades_g
        yield winning_trades_g
        yield win_rate_g
        yield avg_pnl_g
        yield avg_edge_g
        yield last_trade_age_g
        yield exit_reason_g
        yield pos_size_g
        yield pos_edge_g
        yield pos_age_g
        yield pos_model_prob_g
        yield closed_pnl_g


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
