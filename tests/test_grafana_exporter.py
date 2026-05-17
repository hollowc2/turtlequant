from __future__ import annotations

import importlib.util
from pathlib import Path


_EXPORTER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "grafana_exporter.py"
_SPEC = importlib.util.spec_from_file_location("grafana_exporter", _EXPORTER_PATH)
assert _SPEC is not None
grafana_exporter = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(grafana_exporter)
_effective_close_events = grafana_exporter._effective_close_events


def test_effective_close_events_fee_adjust_legacy_flat_close():
    events = [
        {
            "event": "open",
            "market_id": "m-1",
            "yes_price": 0.50,
            "size_usd": 100.0,
            "ts": "2026-05-01T00:00:00+00:00",
        },
        {
            "event": "close",
            "market_id": "m-1",
            "yes_price": 0.50,
            "pnl": 0.0,
            "ts": "2026-05-01T01:00:00+00:00",
        },
    ]

    closes = _effective_close_events(events)

    assert len(closes) == 1
    assert closes[0]["_opened_ts"] == "2026-05-01T00:00:00+00:00"
    assert closes[0]["_effective_pnl"] == -0.6


def test_effective_close_events_keeps_recorded_nonzero_pnl():
    events = [
        {
            "event": "open",
            "market_id": "m-1",
            "yes_price": 0.50,
            "size_usd": 100.0,
            "ts": "2026-05-01T00:00:00+00:00",
        },
        {
            "event": "close",
            "market_id": "m-1",
            "yes_price": 0.60,
            "pnl": 19.0,
            "ts": "2026-05-01T01:00:00+00:00",
        },
    ]

    closes = _effective_close_events(events)

    assert closes[0]["_effective_pnl"] == 19.0
