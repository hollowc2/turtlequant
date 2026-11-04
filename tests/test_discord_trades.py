import importlib.util
import math
from pathlib import Path
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from turtlequant.discord_trades import DiscordTrades, _complement, chart_window, render_trade_chart


def load_turtlequant_bot():
    path = Path(__file__).resolve().parents[1] / "scripts" / "turtlequant_bot.py"
    spec = importlib.util.spec_from_file_location("turtlequant_bot", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_live_only_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_TRADES", "live")
    assert DiscordTrades(tmp_path, "paper").enabled is False
    assert DiscordTrades(tmp_path, "live").enabled is True


def _frame(hours: int, start: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    now = datetime.now(UTC)
    closes = [start + step * i for i in range(hours)]
    return pd.DataFrame(
        {
            "open_time": [now - timedelta(hours=hours - i) for i in range(hours)],
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
        }
    )


def test_render_trade_chart_covers_market_shapes():
    frame = _frame(48)
    entry_ms = int(frame["open_time"].iloc[10].timestamp() * 1000)
    exit_ms = int(frame["open_time"].iloc[-1].timestamp() * 1000)
    for side, option_type, strike, upper in (
        ("YES", "european", 110.0, None),
        ("NO", "barrier", 112.0, None),  # touched inside the window
        ("YES", "barrier_down", 50.0, None),  # far below: drawn as an edge pill
        ("NO", "range", 105.0, 115.0),
    ):
        png = render_trade_chart(
            frame,
            asset="btc",
            question="Will Bitcoin do something long enough to need truncating " * 3,
            side=side,
            option_type=option_type,
            strike=strike,
            upper_strike=upper,
            entry_ms=entry_ms,
            exit_ms=exit_ms,
            entry_price=0.52,
            exit_price=0.61,
            model_prob=0.62,
            edge=0.10,
            pnl=-1.25,
            size_usd=25.0,
            reason="ev_exit",
            expiry=datetime.now(UTC).isoformat(),
            mode="shadow",
        )
        assert png.startswith(b"\x89PNG")
    assert render_trade_chart(frame.iloc[:1], asset="btc", question="q", side="YES",
                              option_type="european", strike=1.0, entry_ms=0, exit_ms=1) is None


def test_complement_of_yes_regions():
    assert _complement([(100.0, math.inf)]) == [(-math.inf, 100.0)]
    assert _complement([(100.0, 110.0)]) == [(-math.inf, 100.0), (110.0, math.inf)]


def test_chart_window_scales_interval_with_hold(monkeypatch):
    monkeypatch.delenv("DISCORD_CHART_INTERVAL", raising=False)
    hour = 3_600_000
    interval, start, end = chart_window(0, 3 * hour)
    assert interval == "15m" and start == -12 * hour and end > 3 * hour
    assert chart_window(0, 7 * 24 * hour)[0] == "1h"
    assert chart_window(0, 31 * 24 * hour)[0] == "4h"
    monkeypatch.setenv("DISCORD_CHART_INTERVAL", "1d")
    assert chart_window(0, 3 * hour)[0] == "1d"


def test_notify_exit_attaches_chart_with_reason(monkeypatch):
    turtlequant_bot = load_turtlequant_bot()
    calls = []
    monkeypatch.setattr(
        turtlequant_bot,
        "trade_chart",
        lambda *args, **kwargs: calls.append(kwargs) or b"png",
    )
    sent = []
    discord = SimpleNamespace(
        send=lambda key, content, chart=None, *, remember=False: sent.append((key, content, chart))
    )
    pos = SimpleNamespace(
        market_id="m-1",
        asset="btc",
        question="Will BTC be above 100k?",
        entry_price=0.52,
        size_usd=25.0,
        opened_at=(datetime.now(UTC) - timedelta(hours=5)).isoformat(),
        outcome="YES",
    )

    turtlequant_bot.notify_exit(discord, pos, 0.61, 2.1, "ev_exit")

    assert calls == [{"exit_price": 0.61, "pnl": 2.1, "reason": "ev_exit"}]
    assert sent[0][2] == b"png"
    assert "TURTLEQUANT EXITED" in sent[0][1]


def test_notify_entry_sends_text_only_and_remembers_link(monkeypatch):
    turtlequant_bot = load_turtlequant_bot()

    def fail_if_charted(*_args, **_kwargs):
        raise AssertionError("entry notifications should not generate charts")

    sent = []
    monkeypatch.setattr(turtlequant_bot, "trade_chart", fail_if_charted)
    discord = SimpleNamespace(
        send=lambda key, content, chart=None, *, remember=False: sent.append(
            (key, content, chart, remember)
        )
    )
    pos = SimpleNamespace(
        market_id="m-1",
        asset="btc",
        question="Will BTC be above 100k?",
        entry_price=0.52,
        size_usd=25.0,
        token_size=48.0769,
        edge_at_entry=0.10,
        strike=100000.0,
        expiry_iso="2026-12-31T00:00:00+00:00",
        outcome="NO",
    )

    turtlequant_bot.notify_entry(
        discord,
        pos,
        model_prob=0.62,
        bid=0.51,
        ask=0.53,
        sigma=0.45,
    )

    assert len(sent) == 1
    key, content, chart, remember = sent[0]
    assert key == "m-1"
    assert "TURTLEQUANT ENTERED** `BTC NO`" in content
    assert chart is None
    assert remember is True
