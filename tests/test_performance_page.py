import datetime as dt
import json
import re

import pytest

from turtlequant.history import append_history
from turtlequant.performance_page import (
    build_closed_trades,
    compute_stats,
    daily_returns,
    open_positions_from_state,
    render_page,
)

NOW = dt.datetime(2026, 5, 10, 12, 0, tzinfo=dt.UTC)


def _open(market_id, ts, *, price=0.40, size=100.0, edge=0.08, question="Will BTC be above $75,000?"):
    return {
        "event": "open",
        "market_id": market_id,
        "question": question,
        "asset": "btc",
        "strike": 75000.0,
        "expiry": "2026-06-01T00:00:00+00:00",
        "option_type": "european",
        "model_prob": price + edge,
        "yes_price": price,
        "edge": edge,
        "size_usd": size,
        "ts": ts,
    }


def _close(market_id, ts, pnl, *, price=0.50, reason="edge_decayed"):
    return {
        "event": "close",
        "market_id": market_id,
        "asset": "btc",
        "strike": 75000.0,
        "reason": reason,
        "yes_price": price,
        "pnl": pnl,
        "ts": ts,
    }


def _history():
    return [
        _open("m-1", "2026-05-01T00:00:00+00:00"),
        _close("m-1", "2026-05-01T06:00:00+00:00", 20.0),
        _open("m-2", "2026-05-02T00:00:00+00:00"),
        _close("m-2", "2026-05-03T00:00:00+00:00", -10.0, price=0.35, reason="edge_reversed"),
        _open("m-3", "2026-05-04T00:00:00+00:00"),
        _close("m-3", "2026-05-05T00:00:00+00:00", -5.0, price=0.38, reason="time_cleanup"),
        _open("m-4", "2026-05-06T00:00:00+00:00"),
        _close("m-4", "2026-05-06T12:00:00+00:00", 30.0, price=0.55),
    ]


def test_build_closed_trades_pairs_open_and_close():
    trades = build_closed_trades(_history())

    assert [t.market_id for t in trades] == ["m-1", "m-2", "m-3", "m-4"]
    first = trades[0]
    assert first.asset == "BTC"
    assert first.entry_price == 0.40
    assert first.exit_price == 0.50
    assert first.hold_hours == 6.0
    assert first.return_pct == 20.0
    assert first.question == "Will BTC be above $75,000?"


def test_partial_close_pnl_rolls_into_its_round_trip():
    events = [
        _open("m-1", "2026-05-01T00:00:00+00:00"),
        {"event": "partial_close", "market_id": "m-1", "pnl": 4.0, "ts": "2026-05-01T01:00:00+00:00"},
        _close("m-1", "2026-05-01T02:00:00+00:00", 6.0),
    ]

    trades = build_closed_trades(events)

    assert len(trades) == 1
    assert trades[0].pnl == 10.0


def test_legacy_flat_close_is_fee_adjusted():
    events = [
        _open("m-1", "2026-05-01T00:00:00+00:00", price=0.50, size=100.0),
        _close("m-1", "2026-05-01T01:00:00+00:00", 0.0, price=0.50),
    ]

    assert build_closed_trades(events)[0].pnl == pytest.approx(-0.6)


def test_compute_stats():
    stats = compute_stats(build_closed_trades(_history()), 1000.0, NOW)

    assert stats.total_pnl == 35.0
    assert stats.ending_nav == 1035.0
    assert stats.return_pct == pytest.approx(3.5)
    assert (stats.wins, stats.losses) == (2, 2)
    assert stats.win_rate == 50.0
    assert stats.profit_factor == pytest.approx(50.0 / 15.0)
    assert stats.payoff_ratio == pytest.approx(25.0 / 7.5)
    # Peak 1020 after m-1, trough 1005 after m-3.
    assert stats.max_drawdown_usd == pytest.approx(15.0)
    assert stats.max_drawdown_pct == pytest.approx(15.0 / 1020.0 * 100)
    assert stats.current_drawdown_pct == 0.0
    assert (stats.max_win_streak, stats.max_loss_streak) == (1, 2)
    assert stats.days_tracked == 10  # 2026-05-01 .. 2026-05-10 inclusive
    assert stats.sharpe is not None and stats.sharpe > 0
    assert stats.sortino is not None


def test_profit_factor_is_none_without_losses():
    trades = build_closed_trades(_history()[:2])

    assert compute_stats(trades, 1000.0, NOW).profit_factor is None


def test_daily_returns_count_idle_days_as_zero():
    trades = build_closed_trades(_history()[:2])

    returns = daily_returns(trades, 1000.0, dt.datetime(2026, 5, 3, tzinfo=dt.UTC))

    assert returns == [pytest.approx(0.02), 0.0, 0.0]


def test_open_positions_are_marked_to_bid_or_resolution():
    state = {
        "positions": [
            {"market_id": "a", "question": "q", "asset": "eth", "strike": 3000, "expiry_iso": "2026-06-01T00:00:00+00:00",
             "option_type": "barrier", "entry_price": 0.40, "size_usd": 40.0, "token_size": 100.0,
             "edge_at_entry": 0.07, "opened_at": "2026-05-01T00:00:00+00:00", "last_bid": 0.45},
            {"market_id": "b", "question": "q", "asset": "btc", "strike": 90000, "expiry_iso": "2026-05-02T00:00:00+00:00",
             "option_type": "european", "entry_price": 0.20, "size_usd": 20.0, "token_size": 100.0,
             "edge_at_entry": 0.05, "opened_at": "2026-05-01T01:00:00+00:00", "status": "pending_redemption",
             "resolution_price": 1.0, "last_bid": 0.1},
        ]
    }

    positions = open_positions_from_state(state)

    assert [p.unrealized_pnl for p in positions] == [pytest.approx(5.0), pytest.approx(80.0)]


def _inline_scripts(page):
    return [
        body
        for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", page, re.DOTALL)
        if "src=" not in attrs and "application/json" not in attrs
    ]


def test_inline_script_is_identical_regardless_of_data():
    """The site's CSP pins this script by hash; data must only change the JSON block."""
    trades = build_closed_trades(_history())
    empty = render_page(trades=[], open_positions=[], starting_nav=1000.0, generated_at=NOW)
    full = render_page(trades=trades, open_positions=[], starting_nav=1000.0, generated_at=NOW)

    assert _inline_scripts(empty) == _inline_scripts(full)
    assert len(_inline_scripts(full)) == 1


def test_data_block_escapes_script_breakouts():
    events = [
        _open("m-1", "2026-05-01T00:00:00+00:00", question="</script><script>alert(1)</script>"),
        _close("m-1", "2026-05-01T06:00:00+00:00", 1.0),
    ]

    page = render_page(trades=build_closed_trades(events), open_positions=[], starting_nav=1000.0, generated_at=NOW)

    block = re.search(r'id="chart-data">(.*?)</script>', page, re.DOTALL).group(1)
    assert "<" not in block
    assert json.loads(block)["trades"][0]["question"].startswith("</script>")
    assert "<script>alert" not in page


def test_generator_end_to_end(tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "generate_performance_page",
        Path(__file__).resolve().parents[1] / "scripts" / "generate_performance_page.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    state_dir = tmp_path / "state"
    for event in _history():
        append_history(state_dir, event)
    (state_dir / "turtlequant-positions.json").write_text(
        json.dumps({"nav": 1035.0, "total_pnl": 35.0, "positions": []})
    )
    output = tmp_path / "site" / "index.html"

    assert module.main(["--state-dir", str(state_dir), "--output", str(output)]) == 0

    page = output.read_text()
    assert "Shadow Trading" in page
    assert "+$35.00" in page
    assert "https://github.com/hollowc2/turtlequant" in page
