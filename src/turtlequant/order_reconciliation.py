"""Fail-closed recovery of broker orders journaled before submission."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import datetime

from turtlequant.clob_execution import ExecutionClient, OrderSide, confirmed_fill
from turtlequant.order_intents import OrderIntent, OrderIntentLedger
from turtlequant.position_manager import PositionManager, make_position


class ReconciliationError(RuntimeError):
    """Broker evidence cannot safely be reflected in local state."""


def _actual_taker_fee(executor: ExecutionClient, intent: OrderIntent) -> float:
    """Derive the charged fee from confirmed authenticated trade records."""
    raw = executor.get_trades(intent.token_id)
    trades = raw.get("data", []) if isinstance(raw, dict) else raw
    if not isinstance(trades, list):
        raise ReconciliationError(f"intent {intent.id} returned invalid trade history")
    fee = Decimal(0)
    matched = False
    for trade in trades:
        if not isinstance(trade, dict) or trade.get("taker_order_id") != intent.order_id:
            continue
        if trade.get("status") != "TRADE_STATUS_CONFIRMED" or trade.get("trader_side") != "TAKER":
            raise ReconciliationError(f"intent {intent.id} has nonterminal trade evidence")
        try:
            shares = Decimal(str(trade["size"])) / Decimal("1000000")
            price = Decimal(str(trade["price"]))
            rate = Decimal(str(trade["fee_rate_bps"])) / Decimal("10000")
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ReconciliationError(f"intent {intent.id} has invalid trade fee evidence") from exc
        fee += shares * rate * price * (1 - price)
        matched = True
    if not matched:
        raise ReconciliationError(f"intent {intent.id} has no confirmed taker trade")
    return float(fee.quantize(Decimal("0.00001")))


def reconcile_intent(intent: OrderIntent, executor: ExecutionClient, positions: PositionManager) -> None:
    """Apply one terminal broker fill, or raise without changing local state."""
    if not intent.order_id:
        raise ReconciliationError(f"intent {intent.id} has no broker order id")
    try:
        fill = confirmed_fill(executor.get_order(intent.order_id), OrderSide(intent.side), intent.requested)
    except (RuntimeError, ValueError) as exc:
        raise ReconciliationError(f"intent {intent.id} is ambiguous: {exc}") from exc
    if intent.side == OrderSide.BUY.value:
        fee_usd = _actual_taker_fee(executor, intent)
        meta = intent.metadata or {}
        required = ("question", "asset", "strike", "expiry_iso", "option_type", "model_prob")
        existing = positions.get_position(intent.market_id)
        if existing is not None:
            if (
                existing.fill_confirmed and existing.yes_token_id == intent.token_id
                and abs(existing.token_size - fill.filled_shares) <= 1e-6
                and abs(existing.size_usd - fill.filled_usd) <= 1e-6
            ):
                positions.confirm_fill(intent.market_id, fill.avg_price, fee_usd=fee_usd)
                return  # Crash after state save but before journal acknowledgement.
            raise ReconciliationError(f"intent {intent.id} BUY disagrees with local position")
        if any(key not in meta for key in required):
            raise ReconciliationError(f"intent {intent.id} lacks safe BUY position metadata")
        try:
            position = make_position(
                market_id=intent.market_id, question=str(meta["question"]), asset=str(meta["asset"]),
                strike=float(meta["strike"]), expiry=datetime.fromisoformat(str(meta["expiry_iso"])),
                option_type=str(meta["option_type"]), yes_token_id=intent.token_id, yes_price=fill.avg_price,
                size_usd=fill.filled_usd, model_prob=float(meta["model_prob"]), token_size=fill.filled_shares,
            )
        except (TypeError, ValueError) as exc:
            raise ReconciliationError(f"intent {intent.id} has invalid BUY position metadata") from exc
        positions.open_position(position)
        positions.confirm_fill(intent.market_id, fill.avg_price, size_usd=fill.filled_usd, token_size=fill.filled_shares, fee_usd=fee_usd)
    else:
        fee_usd = _actual_taker_fee(executor, intent)
        if not positions.has_position(intent.market_id):
            raise ReconciliationError(f"intent {intent.id} SELL has no local position")
        positions.close_position(intent.market_id, fill.avg_price, reason="broker_recovery", filled_shares=fill.filled_shares, exit_fee_usd=fee_usd)


def reconcile_outstanding(ledger: OrderIntentLedger, executor: ExecutionClient, positions: PositionManager) -> None:
    """Reconcile all journaled broker actions. Any ambiguity blocks startup."""
    for intent in ledger.outstanding():
        reconcile_intent(intent, executor, positions)
        ledger.reconcile(intent.id)
