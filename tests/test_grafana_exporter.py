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
TurtleQuantCollector = grafana_exporter.TurtleQuantCollector


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


def test_collector_exports_live_readiness_metrics(tmp_path):
    (tmp_path / "turtlequant-positions.json").write_text(
        """
        {
          "nav": 1000,
          "total_pnl": 0,
          "positions": [
            {
              "market_id": "m-1",
              "asset": "btc",
              "option_type": "european",
              "entry_price": 0.40,
              "size_usd": 40,
              "token_size": 100,
              "last_bid": 0.45,
              "edge_at_entry": 0.1,
              "model_prob_at_entry": 0.5,
              "opened_at": "2026-05-01T00:00:00+00:00"
            }
          ]
        }
        """
    )
    (tmp_path / "turtlequant-history.json").write_text(
        """
        [
          {"event": "open", "market_id": "m-1", "edge": 0.1, "slippage": 0.02, "ts": "2026-05-01T00:00:00+00:00"},
          {"event": "order", "side": "BUY", "status": "paper", "requested_usd": 50, "filled_usd": 40, "filled_shares": 100, "ts": "2026-05-01T00:00:00+00:00"},
          {"event": "failed_order", "side": "SELL", "ts": "2026-05-01T01:00:00+00:00"}
        ]
        """
    )

    families = {metric.name: metric for metric in TurtleQuantCollector(str(tmp_path)).collect()}

    assert families["turtlequant_open_unrealized_pnl_usd"].samples[0].value > 4.0
    assert families["turtlequant_avg_entry_slippage"].samples[0].value == 0.02
    assert families["turtlequant_avg_fill_ratio"].samples[0].value == 0.8
    assert families["turtlequant_failed_orders_total"].samples[0].labels["side"] == "SELL"
