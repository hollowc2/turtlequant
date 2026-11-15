from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


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


def test_collector_reads_legacy_and_jsonl_history(tmp_path):
    (tmp_path / "turtlequant-positions.json").write_text('{"nav": 1000, "positions": []}')
    (tmp_path / "turtlequant-history.json").write_text(
        '[{"event": "open", "market_id": "legacy"}]'
    )
    (tmp_path / "turtlequant-history.jsonl").write_text(
        '{"event":"close","market_id":"journal","pnl":1}\n'
    )

    families = {metric.name: metric for metric in TurtleQuantCollector(str(tmp_path)).collect()}
    assert families["turtlequant_closed_trades_total"].samples[0].value == 1.0


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
              "opened_at": "2026-05-01T00:00:00+00:00",
              "strike": 100000,
              "expiry_iso": "2026-06-01T00:00:00+00:00"
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

    # (0.45 - 0.40) * 100 shares, less the 0.07 * p(1-p) taker fee to sell at 0.45.
    assert abs(families["turtlequant_open_unrealized_pnl_usd"].samples[0].value - (5.0 - 100 * 0.07 * 0.45 * 0.55)) < 1e-9
    position = {
        name: families[f"turtlequant_position_{name}"].samples[0]
        for name in ("entry_price", "mark_price", "unrealized_pnl_usd", "strike_usd", "hours_to_expiry")
    }
    assert position["entry_price"].value == 0.40
    assert position["mark_price"].value == 0.45
    assert position["mark_price"].labels["market_id"] == "m-1"
    assert position["unrealized_pnl_usd"].value == families["turtlequant_open_unrealized_pnl_usd"].samples[0].value
    assert position["strike_usd"].value == 100000
    assert position["hours_to_expiry"].value < 0  # expired in 2026-06
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

    # Only strategy + idx labels: no per-trade label values (cardinality).
    assert {tuple(sorted(sample.labels)) for sample in pnl_samples + hold_samples} == {("idx", "strategy")}
    assert _sample_value(families["turtlequant_closed_position_pnl_usd"], idx="0") == 2.0  # most recent
    assert _sample_value(families["turtlequant_closed_position_pnl_usd"], idx="1") == 1.0
    assert _sample_value(families["turtlequant_closed_position_holding_hours"], idx="1") == 1.0


def test_entry_gate_metrics_use_bounded_reason_labels(tmp_path):
    (tmp_path / "turtlequant-risk.json").write_text(
        '{"high_water":1000,"consecutive_failures":4,'
        '"entry_halt":"4 consecutive broker failures (HTTP 500 for order abc)",'
        '"entry_halt_since":"2026-09-24T00:00:00+00:00"}'
    )

    families = {family.name: family for family in TurtleQuantCollector(str(tmp_path)).collect()}

    assert _sample_value(families["turtlequant_entries_halted"], reason="broker_failures") == 1.0
    assert _sample_value(families["turtlequant_consecutive_broker_failures"]) == 4.0
    assert _sample_value(families["turtlequant_entry_halt_age_sec"]) > 0


def test_halt_category_maps_every_gate_reason():
    assert grafana_exporter.halt_category("") == ""
    assert grafana_exporter.halt_category("15% drawdown") == "drawdown"
    assert grafana_exporter.halt_category("data errors in last scan (5/9 markets failed)") == "data_errors"
    assert grafana_exporter.halt_category("stale market data") == "stale_data"
    assert grafana_exporter.halt_category("something new") == "other"


def _closed_trades(collector):
    families = {family.name: family for family in collector.collect()}
    return families["turtlequant_closed_trades_total"].samples[0].value


def _shadow_total(collector, reason="ask_erased_edge"):
    families = {family.name: family for family in collector.collect()}
    return _sample_value(families["turtlequant_shadow_quotes_total"], reason=reason)


def test_collector_waits_for_a_partially_written_line(tmp_path):
    journal = tmp_path / "turtlequant-history.jsonl"
    journal.write_text('{"event":"close","market_id":"a","pnl":1}\n{"event":"clo')
    collector = TurtleQuantCollector(str(tmp_path))

    assert _closed_trades(collector) == 1.0

    with journal.open("a") as handle:
        handle.write('se","market_id":"b","pnl":2}\n')
    assert _closed_trades(collector) == 2.0


def test_collector_rebuilds_after_history_truncation(tmp_path):
    journal = tmp_path / "turtlequant-history.jsonl"
    journal.write_text("".join(f'{{"event":"close","market_id":"m{i}","pnl":1}}\n' for i in range(3)))
    collector = TurtleQuantCollector(str(tmp_path))
    assert _closed_trades(collector) == 3.0

    journal.write_text('{"event":"close","market_id":"x","pnl":1}\n')

    assert _closed_trades(collector) == 1.0


def test_collector_follows_diagnostics_rotation_without_double_counting(tmp_path):
    live = tmp_path / "turtlequant-diagnostics.jsonl"
    line = '{"event":"shadow_quote","reason":"ask_erased_edge","ts":"2026-05-01T00:00:00+00:00"}\n'
    live.write_text(line * 2)
    collector = TurtleQuantCollector(str(tmp_path))
    assert _shadow_total(collector) == 2.0

    with live.open("a") as handle:  # written after the last scrape, then rotated away
        handle.write(line)
    live.rename(tmp_path / "turtlequant-diagnostics.jsonl.1")
    live.write_text(line)

    assert _shadow_total(collector) == 4.0
    # A fresh exporter reads the rotated file plus the live one.
    assert _shadow_total(TurtleQuantCollector(str(tmp_path))) == 4.0


def test_partial_close_pnl_is_part_of_its_round_trip(tmp_path):
    (tmp_path / "turtlequant-history.jsonl").write_text(
        '{"event":"open","market_id":"m","yes_price":0.4,"size_usd":40,"ts":"2026-05-01T00:00:00+00:00"}\n'
        '{"event":"partial_close","market_id":"m","pnl":-3.0,"ts":"2026-05-01T01:00:00+00:00"}\n'
        '{"event":"close","market_id":"m","asset":"btc","pnl":2.0,"ts":"2026-05-01T02:00:00+00:00"}\n'
    )

    families = {f.name: f for f in TurtleQuantCollector(str(tmp_path)).collect()}

    assert families["turtlequant_closed_trades_total"].samples[0].value == 1.0
    assert families["turtlequant_avg_pnl_per_trade_usd"].samples[0].value == -1.0
    assert families["turtlequant_win_rate"].samples[0].value == 0.0  # net loser, not a win


def test_asset_delta_gauge_reads_the_risk_file(tmp_path):
    (tmp_path / "turtlequant-risk.json").write_text(
        '{"high_water":1000,"asset_risk":{"btc":{"gross_usd":120.0,"delta_usd":-85.5}}}'
    )

    families = {family.name: family for family in TurtleQuantCollector(str(tmp_path)).collect()}

    assert _sample_value(families["turtlequant_asset_delta_usd"], asset="btc") == -85.5


def test_risk_drawdown_metrics_match_the_entry_gate_mark(tmp_path):
    (tmp_path / "turtlequant-positions.json").write_text(
        '{"nav":1000,"total_pnl":0,"positions":['
        '{"market_id":"m","asset":"btc","option_type":"european",'
        '"size_usd":40,"entry_price":0.4,"token_size":100,"last_bid":0.3,'
        '"opened_at":"2026-09-01T00:00:00+00:00"}]}'
    )
    (tmp_path / "turtlequant-risk.json").write_text('{"high_water":1050}')

    families = {family.name: family for family in TurtleQuantCollector(str(tmp_path)).collect()}

    assert _sample_value(families["turtlequant_marked_equity_usd"]) == 990.0
    assert _sample_value(families["turtlequant_risk_high_water_usd"]) == 1050.0
    assert _sample_value(families["turtlequant_risk_drawdown_pct"]) == pytest.approx(60.0 / 1050.0)


def test_position_gauges_carry_the_outcome(tmp_path):
    (tmp_path / "turtlequant-positions.json").write_text(
        '{"nav":1000,"positions":[{"market_id":"m","asset":"btc","option_type":"european","outcome":"NO",'
        '"size_usd":30,"entry_price":0.3,"token_size":100,"opened_at":"2026-09-01T00:00:00+00:00"}]}'
    )

    families = {family.name: family for family in TurtleQuantCollector(str(tmp_path)).collect()}

    assert _sample_value(families["turtlequant_position_size_usd"], market_id="m", outcome="NO") == 30.0


def test_strategy_label_comes_from_the_environment(monkeypatch):
    # The live exporter runs this same script with STRATEGY=turtlequant-live.
    monkeypatch.setenv("STRATEGY", "turtlequant-live")
    spec = importlib.util.spec_from_file_location("grafana_exporter_live", _EXPORTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.STRATEGY == "turtlequant-live"
    assert grafana_exporter.STRATEGY == "turtlequant"


def _families(collector):
    return {family.name: family for family in collector.collect()}


def test_scan_outcomes_sum_every_listed_counter_and_zero_fill_the_rest(tmp_path):
    (tmp_path / "turtlequant-diagnostics.jsonl").write_text(
        '{"event":"scan_summary","mid_edge_candidates":3,"consensus_blocked":2,"ask_erased_edge":1,'
        '"executable_edge_candidates":2,"min_order_rounded":1,"brand_new_counter":9}\n'
        '{"event":"scan_summary","mid_edge_candidates":1,"below_min_order":4,"wallet_capped":1}\n'
    )

    outcomes = _families(TurtleQuantCollector(str(tmp_path)))["turtlequant_scan_outcomes_total"]

    values = {sample.labels["outcome"]: sample.value for sample in outcomes.samples}
    assert set(values) == set(grafana_exporter.SCAN_OUTCOMES)  # bounded: unknown counters are not exported
    assert values["mid_edge_candidates"] == 4
    assert values["consensus_blocked"] == 2
    assert values["below_min_order"] == 4
    assert values["wallet_balance_unknown"] == 0


def test_entries_count_open_events(tmp_path):
    (tmp_path / "turtlequant-history.jsonl").write_text(
        '{"event":"open","market_id":"a","ts":"2026-11-01T00:00:00+00:00"}\n'
        '{"event":"order","side":"BUY","status":"matched"}\n'
        '{"event":"open","market_id":"b","ts":"2026-11-02T00:00:00+00:00"}\n'
    )

    assert _sample_value(_families(TurtleQuantCollector(str(tmp_path)))["turtlequant_entries_total"]) == 2


def test_pending_redemption_is_valued_at_the_payout(tmp_path):
    (tmp_path / "turtlequant-positions.json").write_text(
        '{"nav":100,"total_pnl":0,"positions":['
        '{"market_id":"won","asset":"btc","size_usd":4,"entry_price":0.4,"token_size":10,'
        '"status":"pending_redemption","resolution_price":1.0},'
        '{"market_id":"open","asset":"btc","size_usd":4,"entry_price":0.4,"token_size":10,"last_bid":0.5}]}'
    )

    families = _families(TurtleQuantCollector(str(tmp_path)))

    assert _sample_value(families["turtlequant_pending_redemption_usd"]) == 10.0


def test_wallet_balance_and_its_age_come_from_the_risk_file(tmp_path):
    (tmp_path / "turtlequant-risk.json").write_text(
        '{"high_water":100,"wallet_collateral_usd":61.25,"wallet_balance_at":"2026-11-01T00:00:00+00:00"}'
    )

    families = _families(TurtleQuantCollector(str(tmp_path)))

    assert _sample_value(families["turtlequant_wallet_collateral_usd"]) == 61.25
    assert _sample_value(families["turtlequant_wallet_balance_age_sec"]) > 0


def test_shadow_exports_no_wallet_balance(tmp_path):
    (tmp_path / "turtlequant-risk.json").write_text(
        '{"high_water":1000,"wallet_collateral_usd":null,"wallet_balance_at":""}'
    )

    families = _families(TurtleQuantCollector(str(tmp_path)))

    assert families["turtlequant_wallet_collateral_usd"].samples == []
    assert families["turtlequant_wallet_balance_age_sec"].samples == []


def test_outstanding_intents_are_read_without_writing_the_state_dir(tmp_path):
    from turtlequant.order_intents import OrderIntentLedger

    ledger = OrderIntentLedger(tmp_path / "turtlequant-order-intents.sqlite3")
    ledger.pending("m-1", "t-1", "BUY", 5.0)
    done = ledger.pending("m-2", "t-2", "BUY", 5.0)
    ledger.reconcile(done)
    ledger.db.close()
    before = sorted(p.name for p in tmp_path.iterdir())
    tmp_path.chmod(0o555)  # as the exporter's read-only mount
    try:
        families = _families(TurtleQuantCollector(str(tmp_path)))
    finally:
        tmp_path.chmod(0o755)

    assert _sample_value(families["turtlequant_outstanding_order_intents"]) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_missing_or_unreadable_intent_journal_exports_nothing(tmp_path):
    assert _families(TurtleQuantCollector(str(tmp_path)))["turtlequant_outstanding_order_intents"].samples == []

    (tmp_path / "turtlequant-order-intents.sqlite3").write_text("not a database")

    families = _families(TurtleQuantCollector(str(tmp_path)))
    assert families["turtlequant_outstanding_order_intents"].samples == []
    assert families["turtlequant_exporter_scrape_success"].samples  # the scrape still completes


def _pairing_state(tmp_path):
    live, shadow = tmp_path / "live", tmp_path / "shadow"
    live.mkdir()
    shadow.mkdir()
    (live / "turtlequant-positions.json").write_text('{"nav":102,"total_pnl":2,"positions":[]}')
    (shadow / "turtlequant-positions.json").write_text('{"nav":1000,"total_pnl":0,"positions":[]}')
    (live / "turtlequant-history.jsonl").write_text(
        # An older fill without nav: NAV at entry is rebuilt as $100 start plus the $2 closed before it.
        '{"event":"close","market_id":"old","pnl":2.0,"ts":"2026-11-01T00:00:00+00:00"}\n'
        '{"event":"open","market_id":"m-1","outcome":"NO","yes_price":0.42,"slippage":0.02,'
        '"size_usd":2.1,"ts":"2026-11-02T12:00:00+00:00"}\n'
        '{"event":"open","market_id":"m-2","yes_price":0.30,"slippage":0.01,"size_usd":1.5,"nav":102,'
        '"ts":"2026-11-03T00:00:00+00:00"}\n'
    )
    (shadow / "turtlequant-history.jsonl").write_text(
        '{"event":"open","market_id":"m-1","outcome":"NO","yes_price":0.40,"slippage":0.005,'
        '"size_usd":30,"nav":1000,"ts":"2026-11-02T11:00:00+00:00"}\n'
        # Same market, other side: not a pair.
        '{"event":"open","market_id":"m-2","outcome":"NO","yes_price":0.70,"size_usd":30,'
        '"nav":1000,"ts":"2026-11-03T00:00:00+00:00"}\n'
    )
    return live, shadow


def test_live_fills_pair_with_shadows_on_the_same_market_and_side(tmp_path):
    live, shadow = _pairing_state(tmp_path)

    families = _families(TurtleQuantCollector(str(live), str(shadow)))

    price = families["turtlequant_paired_fill_entry_price"]
    assert _sample_value(price, idx="1", market_id="m-1", outcome="NO", fill="live") == 0.42
    assert _sample_value(price, idx="1", fill="shadow") == 0.40
    assert _sample_value(families["turtlequant_paired_fill_price_gap"], idx="1") == pytest.approx(0.02)
    assert _sample_value(families["turtlequant_paired_fill_slippage_gap"], idx="1") == pytest.approx(0.015)
    assert _sample_value(families["turtlequant_paired_fill_time_gap_sec"], idx="1") == -3600.0
    size = families["turtlequant_paired_fill_size_pct_nav"]
    assert _sample_value(size, idx="1", fill="live") == pytest.approx(2.1 / 102)
    assert _sample_value(size, idx="1", fill="shadow") == pytest.approx(0.03)
    # The most recent live fill has no shadow fill on its side: live columns only.
    assert _sample_value(price, idx="0", market_id="m-2", fill="live") == 0.30
    assert {s.labels["fill"] for s in price.samples if s.labels["idx"] == "0"} == {"live"}
    assert [s for s in families["turtlequant_paired_fill_price_gap"].samples if s.labels["idx"] == "0"] == []
    assert _sample_value(families["turtlequant_paired_fill_ts"], idx="0") == grafana_exporter._parse_ts(
        "2026-11-03T00:00:00+00:00"
    )


def test_shadow_fill_outside_the_window_is_not_a_pair():
    live = [{"market_id": "m", "outcome": "YES", "ts": 0.0}]
    far = {"market_id": "m", "outcome": "YES", "ts": grafana_exporter.PAIR_WINDOW_SEC + 1.0}
    near = {"market_id": "m", "outcome": "YES", "ts": -60.0}

    assert grafana_exporter.pair_fills(live, {("m", "YES"): [far]}) == [(live[0], None)]
    assert grafana_exporter.pair_fills(live, {("m", "YES"): [far, near]}) == [(live[0], near)]


def test_pairing_is_off_without_a_pair_state_dir(tmp_path):
    live, _ = _pairing_state(tmp_path)

    families = _families(TurtleQuantCollector(str(live)))

    assert families["turtlequant_paired_fill_entry_price"].samples == []
