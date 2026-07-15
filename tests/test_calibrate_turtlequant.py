from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd


_SPEC = importlib.util.spec_from_file_location(
    "calibrate_turtlequant", Path(__file__).parents[1] / "scripts" / "calibrate_turtlequant.py"
)
assert _SPEC and _SPEC.loader
calibrate_turtlequant = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(calibrate_turtlequant)


def test_simulate_entries_uses_non_overlapping_intraday_barrier_labels(monkeypatch):
    monkeypatch.setattr(calibrate_turtlequant, "DAYS_TO_EXPIRY", [2])
    monkeypatch.setattr(calibrate_turtlequant, "STRIKE_MONEYNESS", [0.9, 1.1])
    monkeypatch.setattr(calibrate_turtlequant, "REALIZED_VOL_LOOKBACK_DAYS", 2)
    prices = pd.DataFrame(
        {
            "close": [100] * 9,
            "high": [100, 100, 100, 112, 100, 112, 100, 112, 100],
            "low": [100, 100, 100, 100, 88, 100, 88, 100, 88],
        },
        index=pd.date_range("2026-01-01", periods=9, tz="UTC"),
    )

    rows = calibrate_turtlequant.simulate_entries(prices)

    entries = {row["entry_date"] for row in rows}
    assert entries == {prices.index[2], prices.index[4], prices.index[6]}
    assert {row["contract_type"] for row in rows} == {"european", "barrier_up", "barrier_down"}
    assert all(row["outcome"] == 1 for row in rows if row["contract_type"].startswith("barrier_"))
