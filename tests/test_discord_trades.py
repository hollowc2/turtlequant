import importlib.util
from pathlib import Path
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from turtlequant.discord_trades import DiscordTrades


def load_turtlequant_bot():
    path = Path(__file__).resolve().parents[1] / "scripts" / "turtlequant_bot.py"
    spec = importlib.util.spec_from_file_location("turtlequant_bot", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_chart_and_live_only_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_TRADES", "live")
    notifier = DiscordTrades(tmp_path, "paper")
    assert notifier.enabled is False

    now = datetime.now(UTC)
    frame = pd.DataFrame(
        {"open_time": [now - timedelta(hours=4), now], "close": [100.0, 105.0]}
    )
    chart = notifier.chart(
        frame,
        "test",
        int(now.timestamp() * 1000),
        strike=103.0,
        model_prob=0.62,
        entry_price=0.52,
        edge=0.10,
        sigma=0.45,
        expiry=now.isoformat(),
        yes_above_strike=False,
        bought_side="YES",
    )
    assert chart.startswith(b"\x89PNG")


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
    assert "TURTLEQUANT ENTERED" in content
    assert chart is None
    assert remember is True
