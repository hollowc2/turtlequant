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


def _sample_value(family, **labels):
    for sample in family.samples:
        if all(sample.labels.get(key) == value for key, value in labels.items()):
            return sample.value
    raise AssertionError(f"missing sample in {family.name}: {labels}")


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


def test_collector_exports_shadow_soak_history_metrics(tmp_path):
    (tmp_path / "turtlequant-positions.json").write_text(
        """
        {
          "nav": 1000,
          "total_pnl": 0,
          "positions": []
        }
        """
    )
    (tmp_path / "turtlequant-history.json").write_text(
        """
        [
          {
            "event": "shadow_quote",
            "reason": "ask_erased_edge",
            "quote": {"source": "clob"},
            "ts": "2026-05-01T00:00:00+00:00"
          },
          {
            "event": "shadow_quote",
            "reason": "ask_erased_edge",
            "quote": {"source": "gamma"},
            "ts": "2026-05-01T00:01:00+00:00"
          },
          {
            "event": "shadow_quote",
            "reason": "below_threshold",
            "quote": {"source": "clob"},
            "ts": "2026-05-01T00:02:00+00:00"
          },
          {
            "event": "signal_evaluation",
            "parsed": true,
            "book_source": "clob",
            "vol_source": "deribit",
            "ts": "2026-05-01T00:03:00+00:00"
          },
          {
            "event": "signal_evaluation",
            "parsed": false,
            "book_source": "synthetic",
            "vol_source": "realized",
            "ts": "2026-05-01T00:04:00+00:00"
          },
          {
            "event": "scan_summary",
            "markets_passed_filters": 10,
            "parse_attempted": 10,
            "parsed_markets": 8,
            "unclassified_markets": 2,
            "vol_sources": {"deribit": 6, "realized": 2},
            "book_sources": {"clob": 3, "synthetic": 1},
            "ts": "2026-05-01T00:05:00+00:00"
          }
        ]
        """
    )

    families = {metric.name: metric for metric in TurtleQuantCollector(str(tmp_path)).collect()}

    assert _sample_value(
        families["turtlequant_shadow_quotes_total"],
        strategy="turtlequant",
        reason="ask_erased_edge",
    ) == 2.0
    assert _sample_value(
        families["turtlequant_shadow_quotes_total"],
        strategy="turtlequant",
        reason="below_threshold",
    ) == 1.0
    assert families["turtlequant_ask_erased_edge_ratio"].samples[0].value == 2 / 3
    assert _sample_value(
        families["turtlequant_order_book_source_total"],
        strategy="turtlequant",
        source="clob",
    ) == 6.0
    assert _sample_value(
        families["turtlequant_order_book_source_ratio"],
        strategy="turtlequant",
        source="synthetic",
    ) == 2 / 9
    assert families["turtlequant_synthetic_book_ratio"].samples[0].value == 1 / 3
    assert families["turtlequant_parser_hit_rate"].samples[0].value == 0.8
    assert _sample_value(
        families["turtlequant_signal_evaluation_count"],
        strategy="turtlequant",
        parsed="true",
    ) == 1.0
    assert _sample_value(
        families["turtlequant_signal_evaluation_count"],
        strategy="turtlequant",
        parsed="false",
    ) == 1.0
    assert _sample_value(
        families["turtlequant_signal_book_source_count"],
        strategy="turtlequant",
        source="synthetic",
    ) == 1.0
    assert _sample_value(
        families["turtlequant_signal_vol_source_count"],
        strategy="turtlequant",
        source="deribit",
    ) == 1.0
    assert _sample_value(
        families["turtlequant_vol_source_total"],
        strategy="turtlequant",
        source="realized",
    ) == 3.0
    assert families["turtlequant_realized_vol_fallback_ratio"].samples[0].value == 3 / 10


def test_collector_exports_all_closed_trades(tmp_path):
    (tmp_path / "turtlequant-positions.json").write_text(
        """
        {
          "nav": 1000,
          "total_pnl": 0,
          "positions": []
        }
        """
    )
    (tmp_path / "turtlequant-history.json").write_text(
        """
        [
          {"event": "open", "market_id": "m-1", "question": "Will BTC test close?", "yes_price": 0.40, "size_usd": 40, "ts": "2026-05-01T00:00:00+00:00"},
          {"event": "close", "market_id": "m-1", "asset": "btc", "reason": "stop", "yes_price": 0.41, "pnl": 1.0, "ts": "2026-05-01T01:00:00+00:00"},
          {"event": "open", "market_id": "m-2", "yes_price": 0.50, "size_usd": 50, "ts": "2026-05-02T00:00:00+00:00"},
          {"event": "close", "market_id": "m-2", "asset": "eth", "reason": "target", "yes_price": 0.52, "pnl": 2.0, "ts": "2026-05-02T01:00:00+00:00"}
        ]
        """
    )

    families = {metric.name: metric for metric in TurtleQuantCollector(str(tmp_path)).collect()}

    pnl_samples = families["turtlequant_closed_position_pnl_usd"].samples
    hold_samples = families["turtlequant_closed_position_holding_hours"].samples

    assert len(pnl_samples) == 2
    assert len(hold_samples) == 2
    assert {sample.labels["idx"] for sample in pnl_samples} == {"0", "1"}
    assert pnl_samples[0].labels["opened_at"] == "2026-05-01T00:00:00+00:00"
    assert pnl_samples[0].labels["closed_at"] == "2026-05-01T01:00:00+00:00"
    assert pnl_samples[0].labels["question"] == "Will BTC test close?"
    assert _sample_value(
        families["turtlequant_closed_position_pnl_usd"],
        strategy="turtlequant",
        idx="0",
        market_id="m-1",
        asset="btc",
        reason="stop",
    ) == 1.0
    assert _sample_value(
        families["turtlequant_closed_position_pnl_usd"],
        strategy="turtlequant",
        idx="1",
        market_id="m-2",
        asset="eth",
        reason="target",
    ) == 2.0
    assert _sample_value(
        families["turtlequant_closed_position_holding_hours"],
        strategy="turtlequant",
        idx="0",
        market_id="m-1",
        asset="btc",
        reason="stop",
    ) == 1.0
