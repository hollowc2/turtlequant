#!/usr/bin/env python3
"""List trades filled on a fallback (non-CLOB) book, and run totals without them (read-only).

Until 2026-10-05 paper and shadow filled on the synthetic book
ExecutionClient.get_order_book builds when the CLOB fetch fails: 1,000,000
shares at Gamma's top of book, which live refuses before posting. This lists
every open / close / partial_close that filled on such a book, then the
current run's realised totals with and without the round trips they touch.

Where each fill's book source comes from:

* ``open``: its own ``book_source`` (since 2026-06-04); else the ``quote.source``
  of the BUY ``order`` event just before it (every order since 2026-05-17);
  else the last ``signal_evaluation`` for the market in the diagnostics
  journal and its rotations, which only reach back a few days.
* ``close`` / ``partial_close`` sold before resolution: its own ``book_source``
  (since 2026-10-05); else the ``quote.source`` of the SELL ``order`` event
  just before it.
* ``close`` with reason ``resolved``: no book (settled at the payout). The
  round trip is still excluded when its open filled on a fallback book.

Fills whose source cannot be recovered are reported as ``unknown`` and kept
in the totals.

Usage:
    uv run python scripts/audit_book_sources.py --state-dir /opt/turtlequant/state
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from turtlequant.history import diagnostics_paths, effective_close_pnl, load_history
from turtlequant.performance_page import parse_ts

NO_BOOK_REASONS = {"resolved"}
REAL_BOOK = "clob"


@dataclass
class Fill:
    event: dict[str, Any]
    source: str  # "clob", "synthetic", ..., "resolution" (no book) or "unknown"

    @property
    def fallback(self) -> bool:
        return self.source not in (REAL_BOOK, "resolution", "unknown")


@dataclass
class RoundTrip:
    market_id: str
    fills: list[Fill] = field(default_factory=list)
    pnl: float = 0.0

    @property
    def fallback(self) -> bool:
        return any(f.fallback for f in self.fills)


def _source(event: dict[str, Any]) -> str | None:
    if event.get("book_source"):
        return str(event["book_source"])
    quote = event.get("quote")
    if isinstance(quote, dict) and quote.get("source"):
        return str(quote["source"])
    return None


def signal_sources(paths: list[Path]) -> dict[str, list[tuple[str, str]]]:
    """market_id -> [(ts, book_source)] from signal_evaluation diagnostics, oldest first."""
    found: dict[str, list[tuple[str, str]]] = {}
    for path in paths:
        with path.open() as journal:
            for line in journal:
                if '"signal_evaluation"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "signal_evaluation" and event.get("book_source"):
                    found.setdefault(str(event.get("market_id", "")), []).append(
                        (str(event.get("ts", "")), str(event["book_source"]))
                    )
    return found


def classify(
    events: list[dict[str, Any]], signals: dict[str, list[tuple[str, str]]] | None = None
) -> tuple[list[RoundTrip], list[RoundTrip]]:
    """Group fills into round trips (FIFO per market, as the performance page does).

    Returns (closed round trips in close order, round trips still open).
    """
    signals = signals or {}
    last_order: dict[tuple[str, str], dict[str, Any]] = {}
    opens: dict[str, list[RoundTrip]] = {}
    partials: dict[str, RoundTrip] = {}
    closed: list[RoundTrip] = []
    for event in events:
        kind = event.get("event")
        market_id = str(event.get("market_id", ""))
        if kind == "order" and event.get("success"):
            last_order[(market_id, str(event.get("side", "")))] = event
        elif kind == "open":
            order = last_order.pop((market_id, "BUY"), None)
            source = _source(event) or (order and _source(order)) or _signal_source(signals, market_id, event)
            trip = RoundTrip(market_id, [Fill(event, source or "unknown")])
            opens.setdefault(market_id, []).append(trip)
        elif kind in ("close", "partial_close"):
            if event.get("reason") in NO_BOOK_REASONS:
                source = "resolution"
            else:
                order = last_order.pop((market_id, "SELL"), None)
                source = _source(event) or (order and _source(order)) or "unknown"
            fill = Fill(event, source)
            if kind == "close" and parse_ts(event.get("ts")) is None:
                continue  # build_closed_trades drops it before pairing
            if kind == "partial_close":
                # Carried into the round trip its final close completes, as
                # build_closed_trades carries partial P&L.
                pending = partials.setdefault(market_id, RoundTrip(market_id))
                pending.fills.append(fill)
                pending.pnl += _float(event.get("pnl"))
                continue
            queue = opens.get(market_id)
            trip = queue.pop(0) if queue else RoundTrip(market_id)
            earlier = partials.pop(market_id, None)
            open_event = trip.fills[0].event if trip.fills else None
            if earlier is None:
                trip.pnl = effective_close_pnl(open_event, event)
            else:
                trip.fills.extend(earlier.fills)
                trip.pnl = earlier.pnl + _float(event.get("pnl"))
            trip.fills.append(fill)
            closed.append(trip)
    still_open = [trip for queue in opens.values() for trip in queue]
    for market_id, pending in partials.items():
        # Partial exits of a position not yet fully closed.
        for trip in opens.get(market_id, []):
            trip.fills.extend(pending.fills)
            trip.pnl += pending.pnl
            break
    return closed, still_open


def _signal_source(signals: dict[str, list[tuple[str, str]]], market_id: str, event: dict[str, Any]) -> str | None:
    ts = str(event.get("ts", ""))
    earlier = [source for seen, source in signals.get(market_id, []) if seen <= ts]
    return earlier[-1] if earlier else None


def _float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _size(event: dict[str, Any]) -> str:
    if event.get("event") == "open":
        return f"${_float(event.get('size_usd')):.2f}"
    return f"{_float(event.get('filled_shares')):.1f} sh"


def _pnl(event: dict[str, Any]) -> str:
    return "" if event.get("event") == "open" else f"{_float(event.get('pnl')):+.2f}"


def market_labels(events: list[dict[str, Any]]) -> dict[str, str]:
    """market_id -> "BTC K=80,000 2026-12-31 0xabc123" from its open events."""
    labels: dict[str, str] = {}
    for e in events:
        if e.get("event") == "open":
            market_id = str(e.get("market_id", ""))
            labels[market_id] = (
                f"{str(e.get('asset', '?')).upper()} K={_float(e.get('strike')):,.0f} "
                f"{str(e.get('expiry', ''))[:10]} {market_id[:10]}"
            )
    return labels


def report(closed: list[RoundTrip], still_open: list[RoundTrip], labels: dict[str, str]) -> str:
    lines: list[str] = []
    flagged = [f for trip in [*closed, *still_open] for f in trip.fills if f.fallback]
    flagged.sort(key=lambda f: str(f.event.get("ts", "")))
    unknown = sum(1 for trip in [*closed, *still_open] for f in trip.fills if f.source == "unknown")
    lines.append(f"{len(flagged)} fills on a non-CLOB book; {unknown} fills with no recoverable book source")
    if flagged:
        lines.append("")
        lines.append(f"{'ts':20s} {'event':14s} {'market':44s} {'side':4s} {'reason':14s} {'source':10s} "
                     f"{'size':>10s} {'pnl':>9s}")
        for fill in flagged:
            e = fill.event
            market_id = str(e.get("market_id", ""))
            label = (labels.get(market_id) or market_id)[:44]
            lines.append(
                f"{str(e.get('ts', ''))[:19]:20s} {str(e.get('event')):14s} {label:44s} "
                f"{str(e.get('outcome') or 'YES'):4s} {str(e.get('reason') or ''):14s} {fill.source:10s} "
                f"{_size(e):>10s} {_pnl(e):>9s}"
            )

    def totals(trips: list[RoundTrip]) -> str:
        wins = sum(1 for t in trips if t.pnl > 0)
        return f"{len(trips):>6d} {wins:>5d} {sum(t.pnl for t in trips):>+11.2f}"

    clean = [t for t in closed if not t.fallback]
    lines.append("")
    lines.append(f"current run, closed round trips {'trades':>6s} {'wins':>5s} {'realised':>11s}")
    lines.append(f"{'all':31s} {totals(closed)}")
    lines.append(f"{'without fallback-book fills':31s} {totals(clean)}")
    lines.append(f"{'fallback-book round trips':31s} {totals([t for t in closed if t.fallback])}")
    tainted_open = [t for t in still_open if t.fallback]
    lines.append("")
    lines.append(
        f"open positions: {len(still_open)}, of which {len(tainted_open)} entered on a fallback book "
        f"(${sum(_float(t.fills[0].event.get('size_usd')) for t in tainted_open):.2f} cost)"
    )
    excluded = sorted({t.market_id for t in [*closed, *tainted_open] if t.fallback})
    if excluded:
        lines.append("excluded market_ids: " + " ".join(excluded))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit trades for fills on fallback (non-CLOB) books")
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    events = load_history(args.state_dir)
    if not events:
        print(f"no history in {args.state_dir}", file=sys.stderr)
        return 1
    closed, still_open = classify(events)
    if any(f.source == "unknown" and f.event.get("event") == "open" for t in [*closed, *still_open] for f in t.fills):
        closed, still_open = classify(events, signal_sources(diagnostics_paths(args.state_dir)))
    first = next((str(e.get("ts")) for e in events if e.get("ts")), "?")
    print(f"{args.state_dir}: {len(events)} history events since {first[:19]}\n")
    print(report(closed, still_open, market_labels(events)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
