from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "exit_counterfactual.py"
_SPEC = importlib.util.spec_from_file_location("exit_counterfactual", _PATH)
exit_counterfactual = importlib.util.module_from_spec(_SPEC)
sys.modules["exit_counterfactual"] = exit_counterfactual  # dataclasses resolve their module here
_SPEC.loader.exec_module(exit_counterfactual)


def test_counterfactual_compares_sale_with_resolution_payout():
    events = [
        {"event": "open", "market_id": "a", "yes_token_id": "ya", "yes_price": 0.40, "size_usd": 40.0},
        {"event": "order", "market_id": "a", "side": "SELL", "success": True, "fee_usd": 1.0},
        {"event": "close", "market_id": "a", "reason": "edge_decayed", "yes_price": 0.50, "filled_shares": 100.0},
        {"event": "open", "market_id": "b", "outcome": "NO", "token_id": "nb", "yes_price": 0.30, "size_usd": 30.0},
        {"event": "close", "market_id": "b", "reason": "time_cleanup", "yes_price": 0.60, "filled_shares": 100.0},
        {"event": "open", "market_id": "c", "yes_token_id": "yc", "yes_price": 0.5, "size_usd": 50.0},
        {"event": "close", "market_id": "c", "reason": "resolved", "yes_price": 1.0, "filled_shares": 100.0},
        {"event": "open", "market_id": "d", "yes_token_id": "yd", "yes_price": 0.5, "size_usd": 50.0},
        {"event": "close", "market_id": "d", "reason": "edge_reversed", "yes_price": 0.7, "filled_shares": 100.0},
    ]
    payouts = {("a", "ya", "YES"): 1.0, ("b", "nb", "NO"): 0.0, ("d", "yd", "YES"): None}

    rows, unresolved = exit_counterfactual.counterfactuals(events, lambda *key: payouts[key])

    assert unresolved == 1  # d has not resolved; c was not an exit
    by_market = {r.market_id: r for r in rows}
    # a: sold 100 at 0.50 less the recorded $1 fee; holding paid $100.
    assert by_market["a"].sold_for == pytest.approx(49.0)
    assert by_market["a"].hold_minus_sell == pytest.approx(51.0)
    # b: NO sold at 0.60 with the modelled fee; the NO token paid 0, so the exit was right.
    assert by_market["b"].outcome == "NO"
    assert by_market["b"].hold_minus_sell == pytest.approx(-(60.0 - 100 * 0.07 * 0.6 * 0.4))
