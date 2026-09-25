from datetime import UTC, datetime, timedelta

from turtlequant.risk_controls import RiskControls


def test_entry_gate_persists_failure_circuit_breaker(tmp_path):
    controls = RiskControls.load(tmp_path, 100.0)
    for _ in range(3):
        controls.record_failure("API failure")

    assert controls.entries_allowed(100.0, market_data_at=datetime.now(UTC)) == (
        False, "3 consecutive broker failures (API failure)"
    )
    assert RiskControls.load(tmp_path, 100.0).consecutive_failures == 3
    assert RiskControls.load(tmp_path, 100.0).broker_halted()


def test_broker_breaker_cools_down_then_retrips_on_next_failure(tmp_path):
    t0 = datetime(2026, 9, 24, 12, tzinfo=UTC)
    controls = RiskControls.load(tmp_path, 100.0, broker_cooldown_secs=1800)
    for _ in range(3):
        controls.record_failure("rejected", now=t0)

    assert controls.broker_halted(t0 + timedelta(minutes=29))
    assert not controls.broker_halted(t0 + timedelta(minutes=31))  # half-open: one retry

    controls.record_failure("rejected again", now=t0 + timedelta(minutes=31))
    assert controls.broker_halted(t0 + timedelta(minutes=32))

    controls.record_success(100.0)
    assert controls.consecutive_failures == 0
    assert not controls.broker_halted(t0 + timedelta(minutes=32))


def test_legacy_latched_state_expires_from_its_last_write(tmp_path):
    (tmp_path / "turtlequant-risk.json").write_text(
        '{"high_water":100.0,"consecutive_failures":7,"halt_reason":"x",'
        '"updated_at":"2026-09-01T00:00:00+00:00"}'
    )

    controls = RiskControls.load(tmp_path, 100.0)

    assert controls.consecutive_failures == 7
    assert not controls.broker_halted(datetime(2026, 9, 24, tzinfo=UTC))
    assert controls.broker_halted(datetime(2026, 9, 1, 0, 10, tzinfo=UTC))


def test_data_gate_trips_on_widespread_errors_and_resets_after_clean_scan(tmp_path):
    controls = RiskControls.load(tmp_path, 100.0)
    now = datetime.now(UTC)

    controls.record_scan(errors=1, attempted=20)  # one malformed market
    assert controls.entries_allowed(100.0, market_data_at=now) == (True, "")

    controls.record_scan(errors=15, attempted=20)
    allowed, reason = controls.entries_allowed(100.0, market_data_at=now)
    assert not allowed and reason.startswith("data errors")
    assert controls.consecutive_failures == 0  # never touches the broker breaker

    controls.record_scan(errors=0, attempted=20)
    assert controls.entries_allowed(100.0, market_data_at=now) == (True, "")


def test_entry_gate_state_is_persisted_only_on_change(tmp_path):
    controls = RiskControls.load(tmp_path, 100.0)

    assert controls.record_entry_gate("daily loss limit") is True
    assert controls.record_entry_gate("daily loss limit") is False
    restored = RiskControls.load(tmp_path, 100.0)
    assert restored.entry_halt == "daily loss limit"
    assert restored.entry_halt_since
    assert restored.record_entry_gate("") is True
    assert RiskControls.load(tmp_path, 100.0).entry_halt == ""


def test_entry_gate_honors_halt_and_drawdown(tmp_path):
    controls = RiskControls.load(tmp_path, 100.0)
    controls.record_success(120.0)
    assert controls.entries_allowed(100.0, market_data_at=datetime.now(UTC)) == (False, "15% drawdown")

    (tmp_path / "HALT").touch()
    assert controls.entries_allowed(120.0, market_data_at=datetime.now(UTC)) == (False, "HALT file present")


def test_entry_gate_persists_daily_loss_and_rejects_stale_data(tmp_path):
    now = datetime(2026, 7, 15, tzinfo=UTC)
    controls = RiskControls.load(tmp_path, 100.0)
    controls.record_realized_pnl(-12.0, now)

    restored = RiskControls.load(tmp_path, 100.0)
    assert restored.daily_realized_loss == 12.0
    assert restored.entries_allowed(
        100.0, max_daily_loss=10.0, market_data_at=now, now=now
    ) == (False, "daily loss limit")
    assert restored.entries_allowed(
        100.0,
        max_daily_loss=20.0,
        market_data_at=now - timedelta(seconds=91),
        now=now,
    ) == (False, "stale market data")

    restored.record_realized_pnl(0.0, now + timedelta(days=1))
    assert restored.daily_realized_loss == 0.0


def test_non_persistent_risk_controls_never_write(tmp_path):
    controls = RiskControls.load(tmp_path, 1000.0, persist=False)
    controls.record_failure("boom")
    controls.record_realized_pnl(-5.0)
    controls.record_success(1000.0)

    assert not (tmp_path / "turtlequant-risk.json").exists()


def test_risk_state_round_trips_without_persist_field(tmp_path):
    controls = RiskControls.load(tmp_path, 1000.0)
    controls.record_failure("boom")

    assert "persist" not in (tmp_path / "turtlequant-risk.json").read_text()
    assert RiskControls.load(tmp_path, 1000.0).consecutive_failures == 1


def test_unreconciled_order_intents_halt_entries(tmp_path):
    controls = RiskControls.load(tmp_path, 100.0)

    allowed, reason = controls.entries_allowed(100.0, market_data_at=datetime.now(UTC), unreconciled_orders=2)

    assert not allowed
    assert reason == "2 unreconciled order intent(s)"
