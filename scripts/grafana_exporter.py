#!/usr/bin/env python3
"""Prometheus exporter for the TurtleQuant trading bot.

Reads JSON state files from STATE_DIR and exposes per-strategy metrics:

Portfolio gauges (from *-positions.json):
  turtlequant_nav_usd                   — current NAV in USD
  turtlequant_total_pnl_usd             — total realized P&L from positions file
  turtlequant_open_positions_count      — number of open positions
  turtlequant_total_exposure_usd        — sum of open position sizes in USD
  turtlequant_open_unrealized_pnl_usd   — mark-to-bid unrealized P&L on open positions, net of exit fee
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

Entry gate (from *-risk.json):
  turtlequant_entries_halted            — 1 while entries are blocked (labeled by reason category)
  turtlequant_entry_halt_age_sec        — seconds the gate has been closed
  turtlequant_consecutive_broker_failures
  turtlequant_asset_delta_usd           — net dollar delta per asset (labeled by asset)

Per active position (labeled strategy, market_id, asset, option_type, outcome):
  turtlequant_position_size_usd
  turtlequant_position_edge_at_entry
  turtlequant_position_age_hours
  turtlequant_position_model_prob_at_entry
  turtlequant_position_entry_price
  turtlequant_position_mark_price         — the bid (or resolution price) unrealized P&L is marked at
  turtlequant_position_unrealized_pnl_usd — mark-to-bid P&L, net of exit fee
  turtlequant_position_strike_usd
  turtlequant_position_hours_to_expiry

Most recent 50 closed trades (labeled strategy, idx; 0 = most recent):
  turtlequant_closed_position_pnl_usd
  turtlequant_closed_position_holding_hours

Live vs shadow (the comparison row):
  turtlequant_scan_outcomes_total       — running scan_summary counts (labeled by outcome, a fixed set)
  turtlequant_entries_total             — open events (filled entries)
  turtlequant_pending_redemption_usd    — resolved claims awaiting redemption, at their payout
  turtlequant_wallet_collateral_usd     — CLOB pUSD the live bot last read (from *-risk.json)
  turtlequant_wallet_balance_age_sec    — seconds since that read
  turtlequant_outstanding_order_intents — order intents not yet reconciled, failed or cancelled

Live fills paired with shadow's (only with PAIR_STATE_DIR; labeled strategy,
idx, market_id, outcome; 0 = most recent live fill; per-fill gauges also by
fill="live"|"shadow"):
  turtlequant_paired_fill_ts            — when the live fill happened (epoch seconds)
  turtlequant_paired_fill_entry_price
  turtlequant_paired_fill_slippage      — fill price minus that bot's signal mid
  turtlequant_paired_fill_size_pct_nav  — fill size over realized NAV at entry
  turtlequant_paired_fill_price_gap     — live entry price minus shadow's
  turtlequant_paired_fill_slippage_gap  — live slippage minus shadow's
  turtlequant_paired_fill_time_gap_sec  — shadow fill time minus live's

Labels: strategy ("turtlequant" for shadow; $STRATEGY, e.g. "turtlequant-live")

Every state file is opened read-only, including the order-intent SQLite
journal; the exporter never reads wallet secrets or calls the wallet.

Usage:
    python grafana_exporter.py [--state-dir DIR] [--port PORT] [--pair-state-dir DIR]
"""

import argparse
import json
import logging
import os
import sqlite3
import statistics
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path

from prometheus_client import REGISTRY, MetricsHandler
from prometheus_client.core import GaugeMetricFamily
from turtlequant.clob_execution import DEFAULT_CRYPTO_FEE
from turtlequant.history import (
    DIAGNOSTICS_JSONL,
    HISTORY_JSONL,
    active_history_path,
    diagnostics_paths,
    effective_close_pnl,
    read_legacy_events,
)
from turtlequant.order_intents import TERMINAL_STATUSES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

STATE_DIR = os.environ.get("STATE_DIR", "/opt/turtlequant/state")
PORT = int(os.environ.get("EXPORTER_PORT", "8004"))

# Shadow and live each run an exporter; the label keeps their series apart.
STRATEGY = os.environ.get("STRATEGY", "turtlequant")
POSITIONS_FILE = "turtlequant-positions.json"
RISK_FILE = "turtlequant-risk.json"
BOT_LOG_FILE = "turtlequant-bot.log"
INTENTS_FILE = "turtlequant-order-intents.sqlite3"
# The other bot's state dir, read-only: the live exporter pairs each live fill
# with shadow's fill on the same market. Unset, nothing is paired.
PAIR_STATE_DIR = os.environ.get("PAIR_STATE_DIR", "")

# Entry-gate reasons map to a fixed label set; the raw text carries error
# messages and counts that would otherwise mint a new series per failure.
_HALT_CATEGORIES = (
    ("HALT file", "halt_file"),
    ("drawdown", "drawdown"),
    ("daily loss", "daily_loss"),
    ("broker failures", "broker_failures"),
    ("unreconciled order", "unreconciled_orders"),
    ("data errors", "data_errors"),
    ("stale market data", "stale_data"),
)

QUALITY_WINDOW_SEC = 15 * 60
RECENT_TRADES = 50  # bounded idx series for the recent-trade gauges
RECENT_FILLS = 20  # live fills shown paired with shadow's
PAIR_WINDOW_SEC = 24 * 3600  # a shadow fill further from the live one is not its pair

# scan_summary counters exported as turtlequant_scan_outcomes_total{outcome}.
# A fixed set keeps the label bounded; each is exported (0 if never seen) so
# a strategy that never hits one plots a flat zero, not a gap.
SCAN_OUTCOMES = (
    "tenor_skipped",
    "consensus_blocked",
    "mid_edge_candidates",
    "no_side_candidates",
    "score_only_blocked",
    "vol_blocked",
    "below_min_order",
    "min_order_rounded",
    "asset_capped",
    "ask_erased_edge",
    "executable_edge_candidates",
    "synthetic_book_skipped",
    "wallet_capped",
    "wallet_balance_unknown",
)


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
    return "fallback" in normalized or normalized in {
        "default", "proxy", "realized", "unknown", "stale", "smile_unavailable"
    }


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
        matching_open = None
        queue = open_queues.get(market_id)
        if queue:
            matching_open = queue.pop(0)
            close_event["_opened_ts"] = matching_open.get("ts")
            close_event["_question"] = close_event.get("question") or matching_open.get("question")

        close_event["_effective_pnl"] = effective_close_pnl(matching_open, close_event)
        closes.append(close_event)

    return closes


def halt_category(reason: str) -> str:
    """Bounded label for a free-text entry-gate reason ("" when entries are open)."""
    if not reason:
        return ""
    for needle, category in _HALT_CATEGORIES:
        if needle in reason:
            return category
    return "other"


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


class JsonlTail:
    """Incrementally read complete JSON lines from an append-only file.

    Only whole lines are consumed; a partially written last line stays
    buffered until its newline arrives. Truncation (size < offset) or the file
    being replaced by a different inode is reported so the caller can rebuild.
    When ``follow_rotation`` is set and the old inode now lives at
    ``<path>.1``, its unread tail is drained first and no reset is reported.
    """

    def __init__(self, path: Path, *, follow_rotation: bool = False) -> None:
        self.path = path
        self.follow_rotation = follow_rotation
        self.offset = 0
        self.inode: int | None = None
        self._partial = b""

    def read_new(self) -> tuple[list[dict], bool]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return [], False
        events: list[dict] = []
        reset = False
        if self.inode is not None and st.st_ino != self.inode:
            rotated = self.path.with_name(f"{self.path.name}.1")
            if self.follow_rotation and _inode(rotated) == self.inode:
                events.extend(self._drain(rotated, self._size(rotated)))
            else:
                reset = True
            self.offset, self._partial = 0, b""
        elif st.st_size < self.offset:
            reset = True
            self.offset, self._partial = 0, b""
        self.inode = st.st_ino
        if st.st_size > self.offset:
            events.extend(self._drain(self.path, st.st_size))
        return events, reset

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _drain(self, path: Path, size: int) -> list[dict]:
        try:
            with path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read(max(0, size - self.offset))
        except OSError as exc:
            log.warning("could not read JSONL %s: %s", path, exc)
            return []
        self.offset += len(data)
        complete, _, self._partial = (self._partial + data).rpartition(b"\n")
        return _parse_lines(complete)


def _inode(path: Path) -> int | None:
    try:
        return path.stat().st_ino
    except OSError:
        return None


def _parse_lines(data: bytes) -> list[dict]:
    events = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _inc(counts: dict, key: object, amount: int = 1) -> None:
    counts[key] = counts.get(key, 0) + amount


def _fill_record(event: dict, realized_before: float) -> dict | None:
    """The parts of an open event the live/shadow pairing needs."""
    ts = _parse_ts(event.get("ts"))
    if ts is None:
        return None
    return {
        "ts": ts,
        "market_id": str(event.get("market_id", "")),
        "outcome": str(event.get("outcome") or "YES"),
        "price": _safe_float(event.get("yes_price")),
        "slippage": None if event.get("slippage") is None else _safe_float(event.get("slippage")),
        "size_usd": _safe_float(event.get("size_usd")),
        "nav": None if event.get("nav") is None else _safe_float(event.get("nav")),
        # Realized P&L booked before this fill: older open events carry no
        # nav, so NAV at entry is rebuilt as starting NAV plus this.
        "realized_before": realized_before,
    }


def _fill_pct_nav(fill: dict, start_nav: float | None) -> float | None:
    nav = fill["nav"] if fill["nav"] is not None else (
        start_nav + fill["realized_before"] if start_nav is not None else None
    )
    return fill["size_usd"] / nav if nav and nav > 0 else None


def pair_fills(
    live: list[dict], shadow_by_key: dict[tuple[str, str], list[dict]], window_sec: float = PAIR_WINDOW_SEC
) -> list[tuple[dict, dict | None]]:
    """Each live fill (most recent first) with shadow's nearest fill on the same market and side."""
    pairs = []
    for fill in reversed(live):
        candidates = [
            s for s in shadow_by_key.get((fill["market_id"], fill["outcome"]), ())
            if abs(s["ts"] - fill["ts"]) <= window_sec
        ]
        pairs.append((fill, min(candidates, key=lambda s: abs(s["ts"] - fill["ts"]), default=None)))
    return pairs


def _start_nav(positions: object) -> float | None:
    """Starting NAV: NAV and total P&L both move only by realized P&L."""
    if not isinstance(positions, dict) or positions.get("nav") is None:
        return None
    return _safe_float(positions.get("nav")) - _safe_float(positions.get("total_pnl"))


def outstanding_intents(path: Path) -> int | None:
    """Order intents still pending or submitted; None if there is no journal or it can't be read.

    Opened with mode=ro, so the exporter never writes, migrates or locks the
    bot's journal for writing.
    """
    if not path.exists():
        return None
    marks = ", ".join("?" for _ in TERMINAL_STATUSES)
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)) as db:
            row = db.execute(
                f"SELECT COUNT(*) FROM order_intent WHERE status NOT IN ({marks})", TERMINAL_STATUSES
            ).fetchone()
    except sqlite3.Error as exc:
        log.warning("could not read order intents %s: %s", path, exc)
        return None
    return int(row[0])


class HistoryStats:
    """Running aggregates over history and diagnostics events.

    Nothing per-event is retained except trade events (opens awaiting their
    close, and closes) and the timestamps inside the recent quality window.
    """

    def __init__(self) -> None:
        self.events_seen = 0
        self.close: list[dict] = []
        self.effective_close: list[dict] = []
        self._open_queues: dict[str, list[dict]] = {}
        self._partial_pnl: dict[str, float] = {}  # partial_close P&L awaiting its final close
        self.book_source_counts: dict[str, int] = {}
        self.vol_source_counts: dict[str, int] = {}
        self.shadow_counts: dict[str, int] = {}
        self.failed_counts: dict[str, int] = {}
        self.order_counts: dict[tuple[str, str], int] = {}
        self.parsed_counts: dict[str, int] = {}
        self.signal_book_counts: dict[str, int] = {}
        self.signal_vol_counts: dict[str, int] = {}
        self.parse_attempted = 0
        self.parsed_markets = 0
        self.mid_edge_candidates = 0
        self.fill_ratio_sum = 0.0
        self.fill_ratio_count = 0
        self.slippage_sum = 0.0
        self.slippage_count = 0
        self.edge_sum = 0.0
        self.edge_count = 0
        self.scan_outcomes: dict[str, int] = dict.fromkeys(SCAN_OUTCOMES, 0)
        self.entries = 0
        self.realized_pnl = 0.0  # running close + partial_close P&L, for NAV at each fill
        self.recent_fills: deque[dict] = deque(maxlen=RECENT_FILLS)
        self.fills_by_key: dict[tuple[str, str], list[dict]] = {}
        self._recent_shadow: deque[tuple[float, bool]] = deque()
        self._recent_scans: deque[tuple[float, int, int]] = deque()

    def add(self, event: dict) -> None:
        self.events_seen += 1
        kind = event.get("event")
        src = _history_source(event)
        if src is None and event.get("book_source") is not None:
            src = str(event.get("book_source")) or "unknown"
        if src is not None:
            _inc(self.book_source_counts, src)
        if event.get("vol_source") is not None:
            _inc(self.vol_source_counts, str(event.get("vol_source")) or "unknown")

        if kind == "open":
            self._open_queues.setdefault(str(event.get("market_id", "")), []).append(event)
            self.entries += 1
            fill = _fill_record(event, self.realized_pnl)
            if fill is not None:
                self.recent_fills.append(fill)
                self.fills_by_key.setdefault((fill["market_id"], fill["outcome"]), []).append(fill)
            if event.get("slippage") is not None:
                self.slippage_sum += _safe_float(event.get("slippage"))
                self.slippage_count += 1
            if event.get("edge") is not None:
                self.edge_sum += _safe_float(event.get("edge"))
                self.edge_count += 1
        elif kind == "partial_close":
            market_id = str(event.get("market_id", ""))
            self._partial_pnl[market_id] = self._partial_pnl.get(market_id, 0.0) + _safe_float(event.get("pnl"))
            self.realized_pnl += _safe_float(event.get("pnl"))
        elif kind == "close":
            self._add_close(event)
            self.realized_pnl += _safe_float(event.get("pnl"))
        elif kind == "order":
            side = str(event.get("side", "unknown"))
            _inc(self.order_counts, (side, str(event.get("status", "unknown"))))
            req_usd, req_sh = _safe_float(event.get("requested_usd")), _safe_float(event.get("requested_shares"))
            if req_usd > 0:
                self._add_fill_ratio(_safe_float(event.get("filled_usd")) / req_usd)
            elif req_sh > 0:
                self._add_fill_ratio(_safe_float(event.get("filled_shares")) / req_sh)
        elif kind == "failed_order":
            _inc(self.failed_counts, str(event.get("side", "unknown")))
        elif kind == "shadow_quote":
            reason = str(event.get("reason", "unknown"))
            _inc(self.shadow_counts, reason)
            ts = _parse_ts(event.get("ts"))
            if ts is not None:
                self._recent_shadow.append((ts, reason == "ask_erased_edge"))
        elif kind == "signal_evaluation":
            if "parsed" in event:
                _inc(self.parsed_counts, "true" if bool(event.get("parsed")) else "false")
            if event.get("book_source") is not None:
                _inc(self.signal_book_counts, str(event.get("book_source")) or "unknown")
            if event.get("vol_source") is not None:
                _inc(self.signal_vol_counts, str(event.get("vol_source")) or "unknown")
        elif kind == "scan_summary":
            attempted = int(_safe_float(event.get("parse_attempted")))
            parsed = int(_safe_float(event.get("parsed_markets")))
            self.parse_attempted += attempted
            self.parsed_markets += parsed
            self.mid_edge_candidates += int(_safe_float(event.get("mid_edge_candidates")))
            for outcome in SCAN_OUTCOMES:
                self.scan_outcomes[outcome] += int(_safe_float(event.get(outcome)))
            for key, counts in (("book_sources", self.book_source_counts), ("vol_sources", self.vol_source_counts)):
                sources = event.get(key)
                if isinstance(sources, dict):
                    for source, count in sources.items():
                        _inc(counts, str(source) or "unknown", int(_safe_float(count)))
            ts = _parse_ts(event.get("ts"))
            if ts is not None:
                self._recent_scans.append((ts, attempted, parsed))

    def _add_fill_ratio(self, ratio: float) -> None:
        self.fill_ratio_sum += min(1.0, ratio)
        self.fill_ratio_count += 1

    def _add_close(self, event: dict) -> None:
        self.close.append(event)
        close_event = dict(event)
        queue = self._open_queues.get(str(event.get("market_id", "")))
        matching_open = queue.pop(0) if queue else None
        if matching_open is not None:
            close_event["_opened_ts"] = matching_open.get("ts")
            close_event["_question"] = close_event.get("question") or matching_open.get("question")
        # A round trip's P&L includes its earlier partial closes, as on the
        # performance page; otherwise win rate and drawdown miss that money.
        earlier = self._partial_pnl.pop(str(event.get("market_id", "")), None)
        close_event["_effective_pnl"] = (
            effective_close_pnl(matching_open, close_event)
            if earlier is None
            else earlier + _safe_float(event.get("pnl"))
        )
        self.effective_close.append(close_event)

    def recent_quality(self, now: float | None = None) -> dict[str, int]:
        """Shadow/scan counts inside the quality window, pruning older entries."""
        cutoff = (time.time() if now is None else now) - QUALITY_WINDOW_SEC
        for window in (self._recent_shadow, self._recent_scans):
            while window and window[0][0] < cutoff:
                window.popleft()
        return {
            "recent_shadow_count": len(self._recent_shadow),
            "recent_shadow_erased": sum(1 for _, erased in self._recent_shadow if erased),
            "recent_parse_attempted": sum(attempted for _, attempted, _ in self._recent_scans),
            "recent_parsed_markets": sum(parsed for _, _, parsed in self._recent_scans),
        }

    def groups(self) -> dict:
        pnls = [_safe_float(e.get("_effective_pnl", e.get("pnl"))) for e in self.effective_close]
        reason_counts: dict[str, int] = {}
        pnl_by_asset: dict[str, float] = {}
        pnl_by_weekday: dict[str, float] = {}
        for event, pnl in zip(self.effective_close, pnls):
            _inc(reason_counts, str(event.get("reason", "unknown")))
            asset = str(event.get("asset", "unknown"))
            pnl_by_asset[asset] = pnl_by_asset.get(asset, 0.0) + pnl
            ts = _parse_ts(event.get("ts"))
            if ts is not None:
                weekday = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a")
                pnl_by_weekday[weekday] = pnl_by_weekday.get(weekday, 0.0) + pnl
        return {
            "close": self.close,
            "effective_close": self.effective_close,
            "recent_close": self.effective_close[-RECENT_TRADES:],
            "book_source_counts": self.book_source_counts,
            "vol_source_counts": self.vol_source_counts,
            "shadow_counts": self.shadow_counts,
            "failed_counts": self.failed_counts,
            "order_counts": self.order_counts,
            "parsed_counts": self.parsed_counts,
            "signal_book_counts": self.signal_book_counts,
            "signal_vol_counts": self.signal_vol_counts,
            "parse_attempted": self.parse_attempted,
            "parsed_markets": self.parsed_markets,
            "mid_edge_candidates": self.mid_edge_candidates,
            "scan_outcomes": self.scan_outcomes,
            "entries": self.entries,
            "recent_fills": list(self.recent_fills),
            "avg_fill_ratio": self.fill_ratio_sum / self.fill_ratio_count if self.fill_ratio_count else None,
            "avg_slippage": self.slippage_sum / self.slippage_count if self.slippage_count else None,
            "avg_edge": self.edge_sum / self.edge_count if self.edge_count else None,
            "pnls": pnls,
            "reason_counts": reason_counts,
            "pnl_by_asset": pnl_by_asset,
            "pnl_by_weekday": pnl_by_weekday,
            **self.recent_quality(),
        }


class PairedFills:
    """The other bot's fills, read incrementally from its history (read-only)."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = Path(state_dir)
        self._reset()

    def _reset(self) -> None:
        self._stats = HistoryStats()
        self._tail = JsonlTail(self.state_dir / HISTORY_JSONL)

    def fills_by_key(self) -> dict[tuple[str, str], list[dict]]:
        events, reset = self._tail.read_new()
        if reset:
            log.info("paired history %s was truncated or replaced; rebuilding", self._tail.path)
            self._reset()
            events, _ = self._tail.read_new()
        for event in events:
            self._stats.add(event)
        return self._stats.fills_by_key

    def start_nav(self) -> float | None:
        return _start_nav(_load_json(str(self.state_dir / POSITIONS_FILE)))


class TurtleQuantCollector:
    def __init__(self, state_dir: str, pair_state_dir: str = ""):
        self.state_dir = state_dir
        self._paired = PairedFills(pair_state_dir) if pair_state_dir else None
        self._reset()

    def _reset(self) -> None:
        state = Path(self.state_dir)
        self._stats = HistoryStats()
        self._history_tail = JsonlTail(state / HISTORY_JSONL)
        self._diagnostics_tail = JsonlTail(state / DIAGNOSTICS_JSONL, follow_rotation=True)
        self._loaded = False

    def _load_initial(self) -> None:
        """Legacy JSON, then rotated diagnostics (oldest first), then the live tails."""
        state = Path(self.state_dir)
        path = state / "turtlequant-history.json"
        try:
            for event in read_legacy_events(path):
                self._stats.add(event)
        except (OSError, ValueError) as exc:
            log.warning("could not read legacy history %s: %s", path, exc)
        for rotated in (p for p in diagnostics_paths(state) if p.name != DIAGNOSTICS_JSONL):
            for event in JsonlTail(rotated).read_new()[0]:
                self._stats.add(event)
        self._loaded = True

    def _get_history_groups(self, hist_path: Path) -> dict | None:
        """Fold newly appended lines into the running aggregates."""
        if not self._loaded:
            self._load_initial()
        for tail in (self._history_tail, self._diagnostics_tail):
            events, reset = tail.read_new()
            if reset:
                log.info("%s was truncated or replaced; rebuilding history aggregates", tail.path.name)
                self._reset()
                return self._get_history_groups(hist_path)
            for event in events:
                self._stats.add(event)
        if self._stats.events_seen == 0:
            return None
        return self._stats.groups()

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
        marked_equity_g = GaugeMetricFamily(
            "turtlequant_marked_equity_usd",
            "Bookkeeping NAV plus open P&L marked exactly as the entry drawdown gate",
            labels=["strategy"],
        )
        risk_high_water_g = GaugeMetricFamily(
            "turtlequant_risk_high_water_usd",
            "Persisted marked-equity high-water mark used by the entry drawdown gate",
            labels=["strategy"],
        )
        risk_drawdown_pct_g = GaugeMetricFamily(
            "turtlequant_risk_drawdown_pct",
            "Current marked-equity drawdown from the entry gate high-water mark as a fraction",
            labels=["strategy"],
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
        ask_erased_edge_ratio_recent_g = GaugeMetricFamily(
            "turtlequant_ask_erased_edge_ratio_recent",
            "Fraction of recent shadow quote events where executable ask erased model edge",
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
        parser_hit_rate_recent_g = GaugeMetricFamily(
            "turtlequant_parser_hit_rate_recent",
            "Fraction of recent scan-summary parse attempts that were classified",
            labels=["strategy"],
        )
        mid_edge_candidates_g = GaugeMetricFamily(
            "turtlequant_mid_edge_candidates_total",
            "Cumulative scan candidates whose midpoint edge met the entry threshold",
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
            labels=["strategy", "market_id", "asset", "option_type", "outcome"],
        )
        pos_edge_g = GaugeMetricFamily(
            "turtlequant_position_edge_at_entry",
            "Edge at entry for open position",
            labels=["strategy", "market_id", "asset", "option_type", "outcome"],
        )
        pos_age_g = GaugeMetricFamily(
            "turtlequant_position_age_hours",
            "Hours since position was opened",
            labels=["strategy", "market_id", "asset", "option_type", "outcome"],
        )
        pos_model_prob_g = GaugeMetricFamily(
            "turtlequant_position_model_prob_at_entry",
            "Model probability at entry for open position",
            labels=["strategy", "market_id", "asset", "option_type", "outcome"],
        )
        pos_entry_price_g = GaugeMetricFamily(
            "turtlequant_position_entry_price",
            "Entry price per held token for open position",
            labels=["strategy", "market_id", "asset", "option_type", "outcome"],
        )
        pos_mark_price_g = GaugeMetricFamily(
            "turtlequant_position_mark_price",
            "Price the open position is marked at (last bid, or resolution price)",
            labels=["strategy", "market_id", "asset", "option_type", "outcome"],
        )
        pos_unrealized_g = GaugeMetricFamily(
            "turtlequant_position_unrealized_pnl_usd",
            "Open position P&L marked to last executable bid, net of exit fee",
            labels=["strategy", "market_id", "asset", "option_type", "outcome"],
        )
        pos_strike_g = GaugeMetricFamily(
            "turtlequant_position_strike_usd",
            "Underlying strike of the open position's market",
            labels=["strategy", "market_id", "asset", "option_type", "outcome"],
        )
        pos_expiry_g = GaugeMetricFamily(
            "turtlequant_position_hours_to_expiry",
            "Hours until the open position's market expires",
            labels=["strategy", "market_id", "asset", "option_type", "outcome"],
        )

        # --- Recent closed trades ---
        # Only a bounded idx label (0 = most recent): per-trade labels such as
        # market, question or timestamps would add a new series per trade.
        closed_pnl_g = GaugeMetricFamily(
            "turtlequant_closed_position_pnl_usd",
            "P&L of the idx-th most recent closed trade in USD (0 = most recent)",
            labels=["strategy", "idx"],
        )
        closed_hold_g = GaugeMetricFamily(
            "turtlequant_closed_position_holding_hours",
            "Holding period of the idx-th most recent closed trade in hours (0 = most recent)",
            labels=["strategy", "idx"],
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
        entries_halted_g = GaugeMetricFamily(
            "turtlequant_entries_halted",
            "1 while the bot's entry gate blocks new entries, labelled by reason category",
            labels=["strategy", "reason"],
        )
        entry_halt_age_g = GaugeMetricFamily(
            "turtlequant_entry_halt_age_sec",
            "Seconds the entry gate has been closed; 0 when open",
            labels=["strategy"],
        )
        broker_failures_g = GaugeMetricFamily(
            "turtlequant_consecutive_broker_failures",
            "Broker order failures since the last filled order",
            labels=["strategy"],
        )
        asset_delta_g = GaugeMetricFamily(
            "turtlequant_asset_delta_usd",
            "Net dollar delta of open positions per asset (sum of shares * dp/dS * S)",
            labels=["strategy", "asset"],
        )
        scrape_success_g = GaugeMetricFamily(
            "turtlequant_exporter_scrape_success",
            "1 when both positions and history files were readable; 0 otherwise",
            labels=["strategy"],
        )

        # --- Live vs shadow ---
        scan_outcomes_g = GaugeMetricFamily(
            "turtlequant_scan_outcomes_total",
            "Cumulative scan_summary counts per outcome over retained diagnostics",
            labels=["strategy", "outcome"],
        )
        entries_g = GaugeMetricFamily(
            "turtlequant_entries_total",
            "Count of open events (filled entries)",
            labels=["strategy"],
        )
        pending_redemption_g = GaugeMetricFamily(
            "turtlequant_pending_redemption_usd",
            "Resolved positions awaiting on-chain redemption, valued at their payout",
            labels=["strategy"],
        )
        wallet_g = GaugeMetricFamily(
            "turtlequant_wallet_collateral_usd",
            "CLOB pUSD balance the live bot last read",
            labels=["strategy"],
        )
        wallet_age_g = GaugeMetricFamily(
            "turtlequant_wallet_balance_age_sec",
            "Seconds since the live bot last read the wallet balance",
            labels=["strategy"],
        )
        intents_g = GaugeMetricFamily(
            "turtlequant_outstanding_order_intents",
            "Order intents still pending or submitted (entries halt while any are outstanding)",
            labels=["strategy"],
        )
        pair_labels = ["strategy", "idx", "market_id", "outcome"]
        paired_ts_g = GaugeMetricFamily(
            "turtlequant_paired_fill_ts",
            "Epoch seconds of the idx-th most recent live fill (0 = most recent)",
            labels=pair_labels,
        )
        paired_price_g = GaugeMetricFamily(
            "turtlequant_paired_fill_entry_price",
            "Entry price per token of a paired live or shadow fill",
            labels=pair_labels + ["fill"],
        )
        paired_slippage_g = GaugeMetricFamily(
            "turtlequant_paired_fill_slippage",
            "Fill price minus that bot's signal mid for a paired fill",
            labels=pair_labels + ["fill"],
        )
        paired_size_g = GaugeMetricFamily(
            "turtlequant_paired_fill_size_pct_nav",
            "Fill size as a fraction of that bot's realized NAV at entry",
            labels=pair_labels + ["fill"],
        )
        paired_price_gap_g = GaugeMetricFamily(
            "turtlequant_paired_fill_price_gap",
            "Live entry price minus shadow's on the same market and side",
            labels=pair_labels,
        )
        paired_slippage_gap_g = GaugeMetricFamily(
            "turtlequant_paired_fill_slippage_gap",
            "Live fill slippage minus shadow's on the same market and side",
            labels=pair_labels,
        )
        paired_time_gap_g = GaugeMetricFamily(
            "turtlequant_paired_fill_time_gap_sec",
            "Shadow fill time minus live fill time in seconds (negative = shadow filled first)",
            labels=pair_labels,
        )

        strategy, pos_file = STRATEGY, POSITIONS_FILE
        pos_path = os.path.join(self.state_dir, pos_file)
        hist_path = active_history_path(Path(self.state_dir))
        history_groups = self._get_history_groups(hist_path)
        nav: float | None = None
        total_pnl: float | None = None
        marked_equity: float | None = None
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
            marked_pnl_total = 0.0
            pending_redemption = 0.0
            marks: list[tuple[float, float]] = []  # (mark, unrealized) per position
            for pos in positions:
                asset = str(pos.get("asset", "unknown"))
                by_asset[asset] = by_asset.get(asset, 0.0) + _safe_float(pos.get("size_usd"))
                tokens = _safe_float(pos.get("token_size"))
                if tokens <= 0:
                    entry = _safe_float(pos.get("entry_price"))
                    size_usd = _safe_float(pos.get("size_usd"))
                    tokens = size_usd / entry if entry > 0 else 0.0
                entry = _safe_float(pos.get("entry_price"))
                mark = (
                    _safe_float(pos.get("resolution_price"))
                    if pos.get("status") == "pending_redemption" and pos.get("resolution_price") is not None
                    else _safe_float(pos.get("last_bid")) or _safe_float(pos.get("last_yes_price")) or entry
                )
                marked_pnl_total += (mark - entry) * tokens
                if pos.get("status") == "pending_redemption" and pos.get("resolution_price") is not None:
                    pending_redemption += mark * tokens
                # Discount the bid mark by the taker fee a sale would pay (r * p(1-p)).
                exit_fee = DEFAULT_CRYPTO_FEE.fee(tokens, mark) if 0.0 < mark <= 1.0 else 0.0
                unrealized = (mark - entry) * tokens - exit_fee
                unrealized_total += unrealized
                unrealized_by_asset[asset] = unrealized_by_asset.get(asset, 0.0) + unrealized
                marks.append((mark, unrealized))
            for asset, value in by_asset.items():
                exposure_by_asset_g.add_metric([strategy, asset], value)
            open_unrealized_pnl_g.add_metric([strategy], unrealized_total)
            pending_redemption_g.add_metric([strategy], pending_redemption)
            for asset, value in unrealized_by_asset.items():
                open_unrealized_pnl_by_asset_g.add_metric([strategy, asset], value)
            if nav is not None:
                marked_equity = nav + marked_pnl_total
                marked_equity_g.add_metric([strategy], marked_equity)
            largest = max((_safe_float(p.get("size_usd")) for p in positions), default=0.0)
            largest_position_pct_nav_g.add_metric([strategy], largest / nav if nav and nav > 0 else 0.0)

            now = time.time()
            for pos, (mark, unrealized) in zip(positions, marks):
                mid = str(pos.get("market_id", ""))
                asset = str(pos.get("asset", ""))
                opt_type = str(pos.get("option_type", ""))
                pos_labels = [strategy, mid, asset, opt_type, str(pos.get("outcome") or "YES")]

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

                pos_entry_price_g.add_metric(pos_labels, _safe_float(pos.get("entry_price")))
                pos_mark_price_g.add_metric(pos_labels, mark)
                pos_unrealized_g.add_metric(pos_labels, unrealized)
                if pos.get("strike") is not None:
                    pos_strike_g.add_metric(pos_labels, _safe_float(pos.get("strike")))
                expiry = _parse_ts(pos.get("expiry_iso"))
                if expiry is not None:
                    pos_expiry_g.add_metric(pos_labels, (expiry - now) / 3600.0)
        pos_age = _file_age_sec(pos_path)
        if pos_age is not None:
            state_file_age_g.add_metric([strategy, "positions"], pos_age)

        risk_path = os.path.join(self.state_dir, RISK_FILE)
        risk = _load_json(risk_path) if os.path.exists(risk_path) else None
        if isinstance(risk, dict):
            high_water = _safe_float(risk.get("high_water"))
            risk_high_water_g.add_metric([strategy], high_water)
            if marked_equity is not None and high_water > 0:
                risk_drawdown_pct_g.add_metric(
                    [strategy], max(0.0, (high_water - marked_equity) / high_water)
                )
            reason = str(risk.get("entry_halt") or "")
            entries_halted_g.add_metric([strategy, halt_category(reason)], 1.0 if reason else 0.0)
            since = _parse_ts(risk.get("entry_halt_since"))
            entry_halt_age_g.add_metric(
                [strategy], max(0.0, time.time() - since) if reason and since is not None else 0.0
            )
            broker_failures_g.add_metric([strategy], _safe_float(risk.get("consecutive_failures")))
            asset_risk = risk.get("asset_risk")
            if isinstance(asset_risk, dict):
                for asset, values in asset_risk.items():
                    if isinstance(values, dict):
                        asset_delta_g.add_metric([strategy, str(asset)], _safe_float(values.get("delta_usd")))
            if risk.get("wallet_collateral_usd") is not None:
                wallet_g.add_metric([strategy], _safe_float(risk.get("wallet_collateral_usd")))
                read_at = _parse_ts(risk.get("wallet_balance_at"))
                if read_at is not None:
                    wallet_age_g.add_metric([strategy], max(0.0, time.time() - read_at))

        intents = outstanding_intents(Path(self.state_dir) / INTENTS_FILE)
        if intents is not None:
            intents_g.add_metric([strategy], float(intents))

        log_age = _file_age_sec(os.path.join(self.state_dir, BOT_LOG_FILE))
        if log_age is not None:
            bot_log_age_g.add_metric([strategy], log_age)

        # ---- History file ----
        if history_groups is not None:
            close_events = history_groups["close"]
            effective_close_events = history_groups["effective_close"]
            hist_age = _file_age_sec(str(hist_path))
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

            # Trade statistics — all aggregations pre-computed in cache
            n_closed = len(close_events)
            closed_trades_g.add_metric([strategy], float(n_closed))

            if n_closed > 0:
                pnls = history_groups["pnls"]
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

                last_ts = _parse_ts(close_events[-1].get("ts"))
                if last_ts is not None:
                    last_trade_age_g.add_metric([strategy], time.time() - last_ts)

                for reason, count in history_groups["reason_counts"].items():
                    exit_reason_g.add_metric([strategy, reason], float(count))
                for asset, pnl in history_groups["pnl_by_asset"].items():
                    pnl_by_asset_g.add_metric([strategy, asset], pnl)
                for weekday, pnl in history_groups["pnl_by_weekday"].items():
                    pnl_by_weekday_g.add_metric([strategy, weekday], pnl)

            if history_groups["avg_edge"] is not None:
                avg_edge_g.add_metric([strategy], history_groups["avg_edge"])
            if history_groups["avg_slippage"] is not None:
                avg_entry_slippage_g.add_metric([strategy], history_groups["avg_slippage"])
            if history_groups["avg_fill_ratio"] is not None:
                avg_fill_ratio_g.add_metric([strategy], history_groups["avg_fill_ratio"])
            for (side, status), count in history_groups["order_counts"].items():
                order_count_g.add_metric([strategy, side, status], float(count))

            for side, count in history_groups["failed_counts"].items():
                failed_orders_g.add_metric([strategy, side], float(count))

            shadow_counts = history_groups["shadow_counts"]
            for reason, count in shadow_counts.items():
                shadow_quote_g.add_metric([strategy, reason], float(count))
            if shadow_counts:
                erased = shadow_counts.get("ask_erased_edge", 0)
                ask_erased_edge_ratio_g.add_metric([strategy], erased / sum(shadow_counts.values()))
            recent_shadow_count = history_groups["recent_shadow_count"]
            if recent_shadow_count > 0:
                ask_erased_edge_ratio_recent_g.add_metric(
                    [strategy], history_groups["recent_shadow_erased"] / recent_shadow_count
                )

            book_source_counts = history_groups["book_source_counts"]
            book_source_total = sum(book_source_counts.values())
            for source, count in book_source_counts.items():
                order_book_source_g.add_metric([strategy, source], float(count))
                if book_source_total > 0:
                    order_book_source_ratio_g.add_metric([strategy, source], count / book_source_total)
            if book_source_total > 0:
                synthetic_count = sum(c for s, c in book_source_counts.items() if _is_synthetic_book_source(s))
                synthetic_book_ratio_g.add_metric([strategy], synthetic_count / book_source_total)

            for parsed, count in history_groups["parsed_counts"].items():
                signal_evaluation_g.add_metric([strategy, parsed], float(count))
            parse_attempted = history_groups["parse_attempted"]
            parsed_markets = history_groups["parsed_markets"]
            if parse_attempted > 0:
                parser_hit_rate_g.add_metric([strategy], parsed_markets / parse_attempted)
            recent_parse_attempted = history_groups["recent_parse_attempted"]
            if recent_parse_attempted > 0:
                parser_hit_rate_recent_g.add_metric(
                    [strategy], history_groups["recent_parsed_markets"] / recent_parse_attempted
                )
            mid_edge_candidates_g.add_metric(
                [strategy], float(history_groups["mid_edge_candidates"])
            )
            for outcome, count in history_groups["scan_outcomes"].items():
                scan_outcomes_g.add_metric([strategy, outcome], float(count))
            entries_g.add_metric([strategy], float(history_groups["entries"]))
            if self._paired is not None:
                start_nav = _start_nav(pos_data)
                shadow_start_nav = self._paired.start_nav()
                pairs = pair_fills(history_groups["recent_fills"], self._paired.fills_by_key())
                for idx, (live_fill, shadow_fill) in enumerate(pairs):
                    labels = [strategy, str(idx), live_fill["market_id"], live_fill["outcome"]]
                    paired_ts_g.add_metric(labels, live_fill["ts"])
                    for name, fill, fill_start in (
                        ("live", live_fill, start_nav), ("shadow", shadow_fill, shadow_start_nav),
                    ):
                        if fill is None:
                            continue
                        paired_price_g.add_metric(labels + [name], fill["price"])
                        if fill["slippage"] is not None:
                            paired_slippage_g.add_metric(labels + [name], fill["slippage"])
                        pct = _fill_pct_nav(fill, fill_start)
                        if pct is not None:
                            paired_size_g.add_metric(labels + [name], pct)
                    if shadow_fill is None:
                        continue
                    paired_price_gap_g.add_metric(labels, live_fill["price"] - shadow_fill["price"])
                    if live_fill["slippage"] is not None and shadow_fill["slippage"] is not None:
                        paired_slippage_gap_g.add_metric(labels, live_fill["slippage"] - shadow_fill["slippage"])
                    paired_time_gap_g.add_metric(labels, shadow_fill["ts"] - live_fill["ts"])
            for source, count in history_groups["signal_book_counts"].items():
                signal_book_source_g.add_metric([strategy, source], float(count))
            for source, count in history_groups["signal_vol_counts"].items():
                parser_scanner_vol_source_g.add_metric([strategy, source], float(count))

            vol_source_counts = history_groups["vol_source_counts"]
            vol_source_total = sum(vol_source_counts.values())
            for source, count in vol_source_counts.items():
                vol_source_g.add_metric([strategy, source], float(count))
            if vol_source_total > 0:
                fallback_count = sum(c for s, c in vol_source_counts.items() if _is_fallback_source(s))
                realized_vol_fallback_ratio_g.add_metric([strategy], fallback_count / vol_source_total)

            for idx, e in enumerate(reversed(history_groups["recent_close"])):
                labels = [strategy, str(idx)]
                closed_pnl_g.add_metric(labels, _safe_float(e.get("_effective_pnl", e.get("pnl"))))
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
        yield marked_equity_g
        yield risk_high_water_g
        yield risk_drawdown_pct_g
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
        yield ask_erased_edge_ratio_recent_g
        yield order_book_source_g
        yield order_book_source_ratio_g
        yield signal_evaluation_g
        yield parser_hit_rate_g
        yield parser_hit_rate_recent_g
        yield mid_edge_candidates_g
        yield signal_book_source_g
        yield parser_scanner_vol_source_g
        yield vol_source_g
        yield synthetic_book_ratio_g
        yield realized_vol_fallback_ratio_g
        yield pos_size_g
        yield pos_edge_g
        yield pos_age_g
        yield pos_model_prob_g
        yield pos_entry_price_g
        yield pos_mark_price_g
        yield pos_unrealized_g
        yield pos_strike_g
        yield pos_expiry_g
        yield closed_pnl_g
        yield closed_hold_g
        yield state_file_age_g
        yield entries_halted_g
        yield entry_halt_age_g
        yield broker_failures_g
        yield asset_delta_g
        yield bot_log_age_g
        yield scrape_success_g
        yield scan_outcomes_g
        yield entries_g
        yield pending_redemption_g
        yield wallet_g
        yield wallet_age_g
        yield intents_g
        yield paired_ts_g
        yield paired_price_g
        yield paired_slippage_g
        yield paired_size_g
        yield paired_price_gap_g
        yield paired_slippage_gap_g
        yield paired_time_gap_g


def main():
    parser = argparse.ArgumentParser(description="TurtleQuant Prometheus metrics exporter")
    parser.add_argument("--state-dir", default=STATE_DIR)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--pair-state-dir", default=PAIR_STATE_DIR)
    args = parser.parse_args()

    REGISTRY.register(TurtleQuantCollector(args.state_dir, args.pair_state_dir))

    server = HTTPServer(("0.0.0.0", args.port), MetricsHandler)
    log.info("TurtleQuant Prometheus exporter listening on :%d", args.port)
    log.info("State dir: %s", args.state_dir)
    if args.pair_state_dir:
        log.info("Pairing fills with: %s", args.pair_state_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
