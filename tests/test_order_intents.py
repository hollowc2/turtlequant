from turtlequant.order_intents import OrderIntentLedger
from turtlequant.order_reconciliation import ReconciliationError, reconcile_outstanding
from turtlequant.clob_execution import ExecutionClient
from turtlequant.position_manager import PositionManager, make_position
from datetime import UTC, datetime


def test_intents_survive_restart_until_explicitly_reconciled(tmp_path):
    path = tmp_path / "order-intents.sqlite3"
    ledger = OrderIntentLedger(path)
    intent_id = ledger.pending("market", "token", "BUY", 10.0)
    ledger.submitted(intent_id, "order-1", {"status": "matched"})

    restarted = OrderIntentLedger(path)
    assert restarted.outstanding()[0].order_id == "order-1"
    restarted.reconcile(intent_id)
    assert restarted.outstanding() == []


class _Broker:
    def __init__(self, order, trades=None):
        self.order = order
        self.trades = trades or []

    def get_order(self, order_id):
        assert order_id == "order-1"
        return self.order

    def get_trades(self, _params):
        return self.trades


def _trade(size, price, fee_rate_bps="700"):
    return {
        "taker_order_id": "order-1", "status": "TRADE_STATUS_CONFIRMED",
        "trader_side": "TAKER", "size": size, "price": price,
        "fee_rate_bps": fee_rate_bps,
    }


def test_recovery_rebuilds_confirmed_buy_from_broker_order(tmp_path):
    ledger = OrderIntentLedger(tmp_path / "intents.sqlite3")
    intent = ledger.pending("market", "token", "BUY", 10.0, {
        "question": "Will BTC rise?", "asset": "btc", "strike": 100_000,
        "expiry_iso": "2026-08-01T00:00:00+00:00", "option_type": "european", "model_prob": 0.7,
    })
    ledger.submitted(intent, "order-1", {})
    broker = _Broker(
        {"status": "matched", "makingAmount": "9000000", "takingAmount": "20000000"},
        [_trade("20000000", "0.45")],
    )
    positions = PositionManager(positions_file=tmp_path / "positions.json")

    reconcile_outstanding(ledger, ExecutionClient(clob_client=broker), positions)

    pos = positions.get_position("market")
    assert pos and (pos.entry_price, pos.size_usd, pos.token_size, pos.fill_confirmed) == (0.45, 9.0, 20.0, True)
    assert pos.entry_fee_usd == 0.3465
    assert ledger.outstanding() == []


def test_recovery_applies_confirmed_sell_without_estimates(tmp_path):
    ledger = OrderIntentLedger(tmp_path / "intents.sqlite3")
    positions = PositionManager(positions_file=tmp_path / "positions.json")
    positions.open_position(make_position("market", "q", "btc", 100_000, datetime(2026, 8, 1, tzinfo=UTC), "european", "token", 0.5, 10, 0.7, 20))
    intent = ledger.pending("market", "token", "SELL", 20)
    ledger.submitted(intent, "order-1", {})
    broker = _Broker(
        {"status": "matched", "makingAmount": "10000000", "takingAmount": "4000000"},
        [_trade("10000000", "0.4")],
    )

    reconcile_outstanding(ledger, ExecutionClient(clob_client=broker), positions)

    assert positions.get_position("market").token_size == 10
    assert ledger.outstanding() == []


def test_recovery_blocks_ambiguous_order_without_local_mutation(tmp_path):
    ledger = OrderIntentLedger(tmp_path / "intents.sqlite3")
    intent = ledger.pending("market", "token", "BUY", 10.0, {})
    ledger.submitted(intent, "order-1", {})
    positions = PositionManager(positions_file=tmp_path / "positions.json")

    try:
        reconcile_outstanding(ledger, ExecutionClient(clob_client=_Broker({"status": "live"})), positions)
    except ReconciliationError:
        pass
    else:
        raise AssertionError("ambiguous broker order must block recovery")
    assert positions.get_position("market") is None
    assert len(ledger.outstanding()) == 1


def test_ledger_migrates_old_schema_and_supports_terminal_states(tmp_path):
    import sqlite3

    import pytest

    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE order_intent (
            id INTEGER PRIMARY KEY, market_id TEXT NOT NULL, token_id TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
            requested REAL NOT NULL CHECK(requested > 0),
            status TEXT NOT NULL CHECK(status IN ('pending', 'submitted', 'reconciled')),
            order_id TEXT NOT NULL DEFAULT '', response TEXT NOT NULL DEFAULT '')"""
    )
    db.execute("INSERT INTO order_intent (market_id, token_id, side, requested, status) VALUES ('m1','t','BUY',5,'pending')")
    db.execute("INSERT INTO order_intent (market_id, token_id, side, requested, status) VALUES ('m2','t','SELL',5,'submitted')")
    db.commit()
    db.close()

    ledger = OrderIntentLedger(path)
    assert [i.market_id for i in ledger.outstanding()] == ["m1", "m2"]
    assert [i.id for i in ledger.outstanding("m2")] == [2]

    ledger.fail(1, "live order requires a real CLOB book")
    ledger.resolve(2, "cancelled", "operator: cancelled unfilled at broker")
    assert ledger.outstanding() == []
    with pytest.raises(ValueError):
        ledger.resolve(2, "failed", "already terminal")
    with pytest.raises(ValueError):
        ledger.resolve(1, "pending", "not terminal")
    assert OrderIntentLedger(path).outstanding() == []  # reopen: no second migration


def test_recovery_fee_uses_market_fee_schedule_over_trade_base_fee(tmp_path):
    ledger = OrderIntentLedger(tmp_path / "intents.sqlite3")
    intent = ledger.pending("market", "token", "BUY", 10.0, {
        "question": "Will BTC rise?", "asset": "btc", "strike": 100_000,
        "expiry_iso": "2026-08-01T00:00:00+00:00", "option_type": "european", "model_prob": 0.7,
        "condition_id": "cond-1",
    })
    ledger.submitted(intent, "order-1", {})

    class _BrokerWithMarketInfo(_Broker):
        def get_clob_market_info(self, condition_id):
            assert condition_id == "cond-1"
            return {"t": [{"t": "token"}], "mts": 0.01, "fd": {"r": 0.07, "e": 1, "to": True}}

    broker = _BrokerWithMarketInfo(
        {"status": "matched", "makingAmount": "9000000", "takingAmount": "20000000"},
        [_trade("20000000", "0.45", fee_rate_bps="1000")],  # base fee, not the taker rate
    )
    positions = PositionManager(positions_file=tmp_path / "positions.json")

    reconcile_outstanding(ledger, ExecutionClient(clob_client=broker), positions)

    assert positions.get_position("market").entry_fee_usd == 0.3465  # 20 * 0.07 * 0.45 * 0.55


def test_order_intents_cli_lists_and_resolves(tmp_path, capsys):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "order_intents.py"
    spec = importlib.util.spec_from_file_location("order_intents_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    ledger = OrderIntentLedger(tmp_path / cli.LEDGER_FILE)
    intent = ledger.pending("market", "token", "BUY", 10.0)

    assert cli.main(["--state-dir", str(tmp_path), "list"]) == 0
    assert f"{intent}\tpending\tBUY" in capsys.readouterr().out
    assert cli.main(["--state-dir", str(tmp_path), "resolve", str(intent), "failed", "no order at broker"]) == 0
    assert OrderIntentLedger(tmp_path / cli.LEDGER_FILE).outstanding() == []
    assert cli.main(["--state-dir", str(tmp_path), "resolve", str(intent), "failed", "again"]) == 1


def test_recovery_rebuilds_a_no_position_on_the_no_token(tmp_path):
    ledger = OrderIntentLedger(tmp_path / "intents.sqlite3")
    intent = ledger.pending("market", "no-token", "BUY", 10.0, {
        "question": "Will BTC rise?", "asset": "btc", "strike": 100_000,
        "expiry_iso": "2026-08-01T00:00:00+00:00", "option_type": "european", "model_prob": 0.6,
        "outcome": "NO", "yes_token_id": "yes-token", "no_token_id": "no-token",
    })
    ledger.submitted(intent, "order-1", {})
    broker = _Broker(
        {"status": "matched", "makingAmount": "9000000", "takingAmount": "20000000"},
        [_trade("20000000", "0.45")],
    )
    positions = PositionManager(positions_file=tmp_path / "positions.json")

    reconcile_outstanding(ledger, ExecutionClient(clob_client=broker), positions)

    pos = positions.get_position("market")
    assert (pos.outcome, pos.token_id, pos.yes_token_id) == ("NO", "no-token", "yes-token")
    assert ledger.outstanding() == []
