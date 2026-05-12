from __future__ import annotations

from datetime import UTC, datetime, timedelta

from turtlequant.position_manager import PositionManager, make_position


def test_record_market_data_backfills_token_and_price(tmp_path):
    mgr = PositionManager(positions_file=tmp_path / "positions.json")
    pos = make_position(
        market_id="m-1",
        question="Will BTC be above $100k by March 30?",
        asset="btc",
        strike=100_000,
        expiry=datetime.now(UTC) + timedelta(days=30),
        option_type="european",
        yes_token_id="",
        yes_price=0.42,
        size_usd=25.0,
        model_prob=0.5,
    )
    mgr.open_position(pos)

    changed = mgr.record_market_data(
        "m-1",
        yes_token_id="token-123",
        yes_price=0.39,
        observed_at=datetime.now(UTC),
    )

    assert changed is True
    updated = mgr.get_position("m-1")
    assert updated is not None
    assert updated.yes_token_id == "token-123"
    assert updated.last_yes_price == 0.39
    assert updated.last_yes_price_at


def test_exit_decision_covers_all_triggers(tmp_path):
    mgr = PositionManager(positions_file=tmp_path / "positions.json")
    expiry = datetime.now(UTC) + timedelta(hours=4)
    pos = make_position(
        market_id="m-2",
        question="Will ETH be above $3k by tomorrow?",
        asset="eth",
        strike=3_000,
        expiry=expiry,
        option_type="european",
        yes_token_id="token-456",
        yes_price=0.40,
        size_usd=30.0,
        model_prob=0.55,
    )
    mgr.open_position(pos)

    reversed_decision = mgr.exit_decision("m-2", model_prob=0.39, yes_price=0.40)
    assert reversed_decision.should_exit is True
    assert reversed_decision.reason == "edge_reversed"

    decayed_decision = mgr.exit_decision("m-2", model_prob=0.45, yes_price=0.40)
    assert decayed_decision.should_exit is True
    assert decayed_decision.reason == "edge_decayed"

    cleanup_pos = make_position(
        market_id="m-3",
        question="Will SOL be above $200 by tomorrow?",
        asset="sol",
        strike=200,
        expiry=datetime.now(UTC) + timedelta(hours=5),
        option_type="european",
        yes_token_id="token-789",
        yes_price=0.49,
        size_usd=30.0,
        model_prob=0.55,
    )
    mgr.open_position(cleanup_pos)
    cleanup_decision = mgr.exit_decision("m-3", model_prob=0.52, yes_price=0.49)
    assert cleanup_decision.should_exit is True
    assert cleanup_decision.reason == "time_cleanup"

    hold_decision = mgr.exit_decision("m-2", model_prob=0.57, yes_price=0.40)
    assert hold_decision.should_exit is False
    assert hold_decision.reason is None
