from datetime import UTC, datetime, timedelta

from turtlequant.risk_controls import RiskControls


def test_entry_gate_persists_failure_circuit_breaker(tmp_path):
    controls = RiskControls.load(tmp_path, 100.0)
    for _ in range(3):
        controls.record_failure("API failure")

    assert controls.entries_allowed(100.0, market_data_at=datetime.now(UTC)) == (False, "three consecutive failures")
    assert RiskControls.load(tmp_path, 100.0).consecutive_failures == 3


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
