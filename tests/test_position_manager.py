from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

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


def test_partial_close_keeps_remaining_position(tmp_path):
    mgr = PositionManager(positions_file=tmp_path / "positions.json")
    pos = make_position(
        market_id="m-4",
        question="Will BTC be above $100k by March 30?",
        asset="btc",
        strike=100_000,
        expiry=datetime.now(UTC) + timedelta(days=30),
        option_type="european",
        yes_token_id="token-999",
        yes_price=0.50,
        size_usd=50.0,
        model_prob=0.60,
        token_size=100.0,
    )
    mgr.open_position(pos)

    closed, pnl = mgr.close_position("m-4", exit_price=0.60, reason="partial_exit", filled_shares=40.0)

    assert closed is not None
    assert pnl == pytest.approx(2.628)
    remaining = mgr.get_position("m-4")
    assert remaining is not None
    assert remaining.token_size == 60.0
    assert remaining.size_usd == 30.0


def test_marked_equity_uses_last_bid(tmp_path):
    mgr = PositionManager(positions_file=tmp_path / "positions.json")
    pos = make_position(
        market_id="m-mark",
        question="Question",
        asset="btc",
        strike=100_000,
        expiry=datetime.now(UTC) + timedelta(days=30),
        option_type="european",
        yes_token_id="token",
        yes_price=0.50,
        size_usd=50.0,
        model_prob=0.60,
        token_size=100.0,
    )
    mgr.open_position(pos)
    mgr.record_market_data("m-mark", bid=0.40)

    assert mgr.marked_equity() == 990.0


def test_resolution_remains_accounted_until_redemption(tmp_path):
    mgr = PositionManager(positions_file=tmp_path / "positions.json")
    pos = make_position(
        market_id="m-resolution", question="Question", asset="btc", strike=100_000,
        expiry=datetime.now(UTC) + timedelta(days=1), option_type="european",
        yes_token_id="token", yes_price=0.5, size_usd=50, model_prob=0.6,
    )
    mgr.open_position(pos)

    assert mgr.mark_pending_redemption("m-resolution", 1.0)
    saved = PositionManager(positions_file=tmp_path / "positions.json").get_position("m-resolution")
    assert saved is not None
    assert (saved.status, saved.resolution_price) == ("pending_redemption", 1.0)


def test_confirmed_fees_are_stored_and_used_for_pnl(tmp_path):
    mgr = PositionManager(positions_file=tmp_path / "positions.json")
    pos = make_position(
        market_id="m-fee",
        question="Will BTC be above $100k?",
        asset="btc",
        strike=100_000,
        expiry=datetime.now(UTC) + timedelta(days=30),
        option_type="european",
        yes_token_id="token-fee",
        yes_price=0.50,
        size_usd=50.0,
        model_prob=0.60,
        token_size=100.0,
    )
    mgr.open_position(pos)
    mgr.confirm_fill("m-fee", 0.50, fee_usd=1.25)

    _, pnl = mgr.close_position("m-fee", 0.60, exit_fee_usd=1.50)

    assert pnl == pytest.approx(7.25)


def test_load_legacy_position_backfills_stale_quote_fields(tmp_path):
    positions_file = tmp_path / "positions.json"
    positions_file.write_text(
        """
        {
          "nav": 1000,
          "total_pnl": 0,
          "positions": [
            {
              "market_id": "m-5",
              "question": "Will BTC be above $100k by March 30?",
              "asset": "btc",
              "strike": 100000,
              "expiry_iso": "2026-12-31T00:00:00+00:00",
              "option_type": "european",
              "yes_token_id": "token-5",
              "entry_price": 0.41,
              "size_usd": 41,
              "model_prob_at_entry": 0.5,
              "edge_at_entry": 0.09,
              "opened_at": "2026-05-01T00:00:00+00:00"
            }
          ]
        }
        """
    )

    mgr = PositionManager(positions_file=positions_file)
    pos = mgr.get_position("m-5")

    assert pos is not None
    assert pos.last_yes_price == 0.41
    assert pos.last_yes_price_at == "2026-05-01T00:00:00+00:00"
    assert pos.token_size == 100.0


def test_save_replaces_positions_file_atomically(tmp_path):
    positions_file = tmp_path / "positions.json"
    mgr = PositionManager(positions_file=positions_file)

    pos = make_position(
        market_id="m-6",
        question="Will ETH be above $3k by March 30?",
        asset="eth",
        strike=3_000,
        expiry=datetime.now(UTC) + timedelta(days=30),
        option_type="european",
        yes_token_id="token-6",
        yes_price=0.25,
        size_usd=25.0,
        model_prob=0.40,
    )
    mgr.open_position(pos)

    assert positions_file.exists()
    assert not list(tmp_path.glob("*.tmp"))

    reloaded = PositionManager(positions_file=positions_file)
    assert reloaded.get_position("m-6") is not None


@pytest.mark.parametrize(
    "state",
    [
        "{not json",
        '{"nav": 1000, "positions": [{"market_id": "bad"}]}',
        '{"nav": 0, "positions": []}',
    ],
)
def test_load_rejects_unsafe_position_state(tmp_path, state):
    positions_file = tmp_path / "positions.json"
    positions_file.write_text(state)

    with pytest.raises(RuntimeError, match="unsafe position state"):
        PositionManager(positions_file=positions_file)


def test_load_rejects_duplicate_market_ids(tmp_path):
    positions_file = tmp_path / "positions.json"
    pos = {
        "market_id": "m-duplicate",
        "question": "Question",
        "asset": "btc",
        "strike": 100_000,
        "expiry_iso": "2026-12-31T00:00:00+00:00",
        "option_type": "european",
        "yes_token_id": "token",
        "entry_price": 0.5,
        "size_usd": 50,
        "token_size": 100,
        "model_prob_at_entry": 0.6,
        "edge_at_entry": 0.1,
        "opened_at": "2026-05-01T00:00:00+00:00",
    }
    positions_file.write_text(json.dumps({"nav": 1000, "positions": [pos, pos]}))

    with pytest.raises(RuntimeError, match="unsafe position state"):
        PositionManager(positions_file=positions_file)


def test_save_raises_when_state_cannot_be_persisted(tmp_path, monkeypatch):
    mgr = PositionManager(positions_file=tmp_path / "positions.json")

    def fail_replace(*_):
        raise OSError("disk error")

    monkeypatch.setattr("turtlequant.position_manager.os.replace", fail_replace)

    with pytest.raises(RuntimeError, match="position state was not persisted"):
        mgr.open_position(
            make_position(
                market_id="m-save-error",
                question="Question",
                asset="btc",
                strike=100_000,
                expiry=datetime.now(UTC) + timedelta(days=1),
                option_type="european",
                yes_token_id="token",
                yes_price=0.5,
                size_usd=10,
                model_prob=0.6,
            )
        )
