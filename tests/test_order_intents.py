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
