from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from turtlequant.history import diagnostics_paths, load_history
from turtlequant.performance_page import build_closed_trades

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit_book_sources.py"
_SPEC = importlib.util.spec_from_file_location("audit_book_sources", _PATH)
audit = importlib.util.module_from_spec(_SPEC)
sys.modules["audit_book_sources"] = audit  # dataclasses resolve their module here
_SPEC.loader.exec_module(audit)

STATE = Path(__file__).resolve().parent / "fixtures" / "book_source_audit"


def flagged(trips):
    return sorted((f.event["market_id"], f.event["event"]) for t in trips for f in t.fills if f.fallback)


def test_flags_every_fill_on_a_fallback_book():
    events = load_history(STATE)
    closed, still_open = audit.classify(events, audit.signal_sources(diagnostics_paths(STATE)))

    assert flagged([*closed, *still_open]) == [
        ("b", "open"),  # book_source on the open
        ("c", "close"),  # close predates book_source; the SELL order's quote says synthetic
        ("d", "partial_close"),
        ("e", "open"),  # no field and no order: the last signal_evaluation before the open
        ("f", "open"),  # still held
    ]
    assert [t.market_id for t in closed if not t.fallback] == ["a"]
    assert [t.market_id for t in still_open] == ["f", "g"]


def test_totals_match_the_performance_page_pairing():
    events = load_history(STATE)
    closed, _ = audit.classify(events, audit.signal_sources(diagnostics_paths(STATE)))

    # Same round trips and P&L as the page, partial_close carried into its close.
    assert sum(t.pnl for t in closed) == pytest.approx(sum(t.pnl for t in build_closed_trades(events)))
    assert {t.market_id: t.pnl for t in closed} == pytest.approx(
        {"a": 13.0, "b": 70.0, "c": 18.5, "d": 2.5, "e": -20.0}
    )


def test_resolved_close_and_failed_orders_are_not_book_fills():
    closed, _ = audit.classify(load_history(STATE))

    sources = {(f.event["market_id"], f.event["event"]): f.source for t in closed for f in t.fills}
    assert sources[("b", "close")] == "resolution"
    assert sources[("d", "open")] == "clob"  # from the BUY order's quote
    assert sources[("e", "open")] == "unknown"  # without diagnostics


def test_main_reads_diagnostics_when_an_open_has_no_source(capsys):
    assert audit.main(["--state-dir", str(STATE)]) == 0

    out = capsys.readouterr().out
    assert "5 fills on a non-CLOB book; 0 fills with no recoverable book source" in out
    assert "all                                  5     4      +84.00" in out
    assert "without fallback-book fills          1     1      +13.00" in out
    assert "open positions: 2, of which 1 entered on a fallback book ($25.00 cost)" in out
    assert "excluded market_ids: b c d e f" in out
