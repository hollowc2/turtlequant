from __future__ import annotations

import pandas as pd

from turtlequant.data import binance


def test_fetch_latest_closes_omits_failed_symbols(monkeypatch):
    def fake_fetch(symbol, *_args):
        if symbol == "BADUSDT":
            raise OSError("offline")
        return pd.DataFrame({"close": [1.0, 2.0]})

    monkeypatch.setattr(binance, "fetch_klines", fake_fetch)

    assert binance.fetch_latest_closes(["BTCUSDT", "BADUSDT", "BTCUSDT"]) == {"BTCUSDT": 2.0}
