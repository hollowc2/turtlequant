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


def test_settlement_realises_payout_without_exit_fee(tmp_path):
    mgr = PositionManager(starting_nav=1000.0, positions_file=tmp_path / "positions.json")
    mgr.open_position(make_position(
        market_id="m-settle", question="Question", asset="btc", strike=100_000,
        expiry=datetime.now(UTC) - timedelta(hours=3), option_type="european",
        yes_token_id="token", yes_price=0.5, size_usd=50, model_prob=0.6, token_size=100,
    ))
    mgr.confirm_fill("m-settle", 0.5, size_usd=50, token_size=100, fee_usd=1.75)

    pos, pnl = mgr.settle_position("m-settle", 1.0)

    assert pos is not None
    assert pnl == pytest.approx(50.0 - 1.75)
    assert not mgr.has_position("m-settle")
    assert mgr.current_nav == pytest.approx(1000.0 + 48.25)


def test_resolved_claims_do_not_consume_exposure_headroom(tmp_path):
    mgr = PositionManager(starting_nav=1000.0, positions_file=tmp_path / "positions.json")
    expiry = datetime.now(UTC) + timedelta(days=1)
    mgr.open_position(make_position(
        market_id="m-big", question="Question", asset="btc", strike=100_000, expiry=expiry,
        option_type="european", yes_token_id="token", yes_price=0.5, size_usd=150, model_prob=0.6,
    ))
    assert not mgr.has_expiry_headroom(expiry, 10.0)

    mgr.mark_pending_redemption("m-big", 1.0)

    assert mgr.has_expiry_headroom(expiry, 10.0)


def test_condition_id_is_persisted_and_backfilled(tmp_path):
    mgr = PositionManager(positions_file=tmp_path / "positions.json")
    mgr.open_position(make_position(
        market_id="m-cid", question="Question", asset="btc", strike=100_000,
        expiry=datetime.now(UTC) + timedelta(days=1), option_type="european",
        yes_token_id="token", yes_price=0.5, size_usd=50, model_prob=0.6,
    ))

    assert mgr.record_market_data("m-cid", condition_id="0xabc")
    saved = PositionManager(positions_file=tmp_path / "positions.json").get_position("m-cid")
    assert saved is not None and saved.condition_id == "0xabc"


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


def test_non_persistent_manager_never_writes_state(tmp_path):
    positions_file = tmp_path / "positions.json"
    manager = PositionManager(starting_nav=1000.0, positions_file=positions_file, persist=False)
    manager.open_position(
        make_position(
            market_id="m1", question="q", asset="btc", strike=100_000.0,
            expiry=datetime.now(UTC) + timedelta(days=5), option_type="european",
            yes_token_id="yes", yes_price=0.40, size_usd=40.0, model_prob=0.55,
        )
    )
    manager.close_position("m1", exit_price=0.50)

    assert not positions_file.exists()
    assert manager.current_nav != 1000.0  # in-memory accounting still runs


def test_partial_closes_charge_entry_fee_exactly_once(tmp_path):
    manager = PositionManager(starting_nav=1000.0, positions_file=tmp_path / "positions.json")
    manager.open_position(
        make_position(
            market_id="m1", question="q", asset="btc", strike=100_000.0,
            expiry=datetime.now(UTC) + timedelta(days=5), option_type="european",
            yes_token_id="yes", yes_price=0.50, size_usd=50.0, model_prob=0.60,
            token_size=100.0,
        )
    )
    manager.confirm_fill("m1", 0.50, size_usd=50.0, token_size=100.0, fee_usd=1.75)

    _, first = manager.close_position("m1", exit_price=0.50, filled_shares=50.0, exit_fee_usd=0.0)
    assert manager.get_position("m1").entry_fee_usd == pytest.approx(0.875)
    _, second = manager.close_position("m1", exit_price=0.50, filled_shares=50.0, exit_fee_usd=0.0)

    # Flat exit, no exit fees: total P&L is exactly minus the entry fee.
    assert first + second == pytest.approx(-1.75)
    assert manager.current_nav == pytest.approx(1000.0 - 1.75)
    assert not manager.has_position("m1")


def test_reentry_cooldown_survives_restart(tmp_path):
    positions_file = tmp_path / "positions.json"
    manager = PositionManager(starting_nav=1000.0, positions_file=positions_file)
    manager.open_position(
        make_position(
            market_id="m1", question="q", asset="btc", strike=100_000.0,
            expiry=datetime.now(UTC) + timedelta(days=5), option_type="european",
            yes_token_id="yes", yes_price=0.40, size_usd=40.0, model_prob=0.55,
        )
    )
    manager.close_position("m1", exit_price=0.45)

    restarted = PositionManager(starting_nav=1000.0, positions_file=positions_file)

    assert restarted.closed_within("m1", 2 * 3600)
    assert not restarted.closed_within("m1", 2 * 3600, now=datetime.now(UTC) + timedelta(hours=3))
    assert not restarted.closed_within("other", 2 * 3600)


def test_partial_close_does_not_start_reentry_cooldown(tmp_path):
    manager = PositionManager(starting_nav=1000.0, positions_file=tmp_path / "positions.json")
    manager.open_position(
        make_position(
            market_id="m1", question="q", asset="btc", strike=100_000.0,
            expiry=datetime.now(UTC) + timedelta(days=5), option_type="european",
            yes_token_id="yes", yes_price=0.40, size_usd=40.0, model_prob=0.55, token_size=100.0,
        )
    )
    manager.close_position("m1", exit_price=0.45, filled_shares=40.0)

    assert not manager.closed_within("m1", 2 * 3600)


def test_legacy_state_loads_as_yes_and_no_requires_its_token(tmp_path):
    positions_file = tmp_path / "positions.json"
    legacy = {
        "market_id": "m1", "question": "q", "asset": "btc", "strike": 80000.0,
        "expiry_iso": "2026-12-31T00:00:00+00:00", "option_type": "european", "yes_token_id": "yes",
        "entry_price": 0.4, "size_usd": 40.0, "model_prob_at_entry": 0.5, "edge_at_entry": 0.1,
        "opened_at": "2026-09-01T00:00:00+00:00", "token_size": 100.0,
    }
    positions_file.write_text(json.dumps({"nav": 1000.0, "positions": [legacy]}))
    pos = PositionManager(positions_file=positions_file).get_position("m1")
    assert pos.outcome == "YES" and pos.token_id == "yes"

    positions_file.write_text(json.dumps({"nav": 1000.0, "positions": [{**legacy, "outcome": "NO"}]}))
    with pytest.raises(RuntimeError):
        PositionManager(positions_file=positions_file)

    positions_file.write_text(
        json.dumps({"nav": 1000.0, "positions": [{**legacy, "outcome": "NO", "no_token_id": "no"}]})
    )
    assert PositionManager(positions_file=positions_file).get_position("m1").token_id == "no"
