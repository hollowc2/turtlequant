from datetime import UTC, datetime, timedelta

import pandas as pd

from turtlequant.discord_trades import DiscordTrades


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
