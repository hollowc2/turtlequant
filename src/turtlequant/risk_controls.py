"""Small persistent entry gate; exits remain available when it is closed.

Two independent breakers block new entries:

* Broker breaker — orders that reached the broker and failed or came back
  ambiguous. It trips after ``max_broker_failures`` in a row and blocks
  entries for ``broker_cooldown_secs``; after that one retry is allowed
  (half-open) and another failure re-trips it. A filled order resets it.
* Data gate — per-market processing errors in the last scan. It closes when
  a scan's errors are both >= 3 and >= half the markets attempted, and
  reopens after one clean scan. One malformed market never halts entries.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from turtlequant.position_manager import StatePersistenceError

DEFAULT_MAX_BROKER_FAILURES = 3
DEFAULT_BROKER_COOLDOWN_SECS = 30 * 60
_DATA_ERROR_MIN = 3
_DATA_ERROR_RATIO = 0.5

# Fields that are runtime configuration, not persisted state.
_RUNTIME_FIELDS = ("state_dir", "persist", "max_broker_failures", "broker_cooldown_secs", "data_degraded")


@dataclass
class RiskControls:
    state_dir: Path
    high_water: float
    consecutive_failures: int = 0  # broker failures since the last filled order
    halt_reason: str = ""  # last broker failure
    daily_loss_date: str = ""
    daily_realized_loss: float = 0.0
    last_failure_at: str = ""  # ISO 8601 UTC of the last broker failure
    entry_halt: str = ""  # last evaluated entry-gate reason ("" = open), for the exporter
    entry_halt_since: str = ""
    persist: bool = True  # False (dry-run) never writes the risk file
    max_broker_failures: int = DEFAULT_MAX_BROKER_FAILURES
    broker_cooldown_secs: float = DEFAULT_BROKER_COOLDOWN_SECS
    data_degraded: str = field(default="", repr=False)  # in memory: last scan's error summary

    @property
    def path(self) -> Path:
        return self.state_dir / "turtlequant-risk.json"

    @classmethod
    def load(
        cls,
        state_dir: Path,
        equity: float,
        *,
        persist: bool = True,
        max_broker_failures: int = DEFAULT_MAX_BROKER_FAILURES,
        broker_cooldown_secs: float = DEFAULT_BROKER_COOLDOWN_SECS,
    ) -> "RiskControls":
        path = state_dir / "turtlequant-risk.json"
        config = {
            "persist": persist,
            "max_broker_failures": max_broker_failures,
            "broker_cooldown_secs": broker_cooldown_secs,
        }
        if not path.exists():
            return cls(state_dir, equity, **config)
        try:
            raw = json.loads(path.read_text())
            return cls(
                state_dir,
                max(float(raw["high_water"]), equity),
                int(raw.get("consecutive_failures", 0)),
                str(raw.get("halt_reason", "")),
                str(raw.get("daily_loss_date", "")),
                float(raw.get("daily_realized_loss", 0.0)),
                # Legacy files lack last_failure_at; start the cooldown from
                # their last write so an old latch expires instead of sticking.
                str(raw.get("last_failure_at") or raw.get("updated_at") or ""),
                str(raw.get("entry_halt", "")),
                str(raw.get("entry_halt_since", "")),
                **config,
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RuntimeError(f"unsafe risk state: {path}: {exc}") from exc

    def entries_allowed(
        self,
        equity: float,
        *,
        max_daily_loss: float = float("inf"),
        market_data_at: datetime | None = None,
        max_market_data_age_secs: float = 90.0,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        now = now or datetime.now(UTC)
        self._roll_day(now)
        if (self.state_dir / "HALT").exists():
            return False, "HALT file present"
        if equity <= 0.85 * self.high_water:
            return False, "15% drawdown"
        if self.daily_realized_loss >= max_daily_loss:
            return False, "daily loss limit"
        if self.broker_halted(now):
            return False, f"{self.consecutive_failures} consecutive broker failures ({self.halt_reason})"
        if self.data_degraded:
            return False, f"data errors in last scan ({self.data_degraded})"
        if (
            market_data_at is None
            or (now - market_data_at).total_seconds() > max_market_data_age_secs
        ):
            return False, "stale market data"
        return True, ""

    def broker_halted(self, now: datetime | None = None) -> bool:
        """True while the broker breaker is tripped and its cooldown is running."""
        if self.consecutive_failures < self.max_broker_failures:
            return False
        try:
            failed_at = datetime.fromisoformat(self.last_failure_at)
        except ValueError:
            return False  # no timestamp: nothing to cool down from
        now = now or datetime.now(UTC)
        return (now - failed_at).total_seconds() < self.broker_cooldown_secs

    def record_success(self, equity: float) -> None:
        """A filled order: raise the high-water mark and reset the broker breaker."""
        self.high_water = max(self.high_water, equity)
        self.consecutive_failures = 0
        self.halt_reason = ""
        self.save()

    def record_failure(self, reason: str, now: datetime | None = None) -> None:
        """An order that reached the broker and failed or is ambiguous."""
        self.consecutive_failures += 1
        self.halt_reason = reason
        self.last_failure_at = (now or datetime.now(UTC)).isoformat()
        self.save()

    def record_scan(self, *, errors: int, attempted: int) -> None:
        """Open or close the data gate from one scan's per-market error count."""
        if errors >= _DATA_ERROR_MIN and errors >= _DATA_ERROR_RATIO * max(attempted, 1):
            self.data_degraded = f"{errors}/{attempted} markets failed"
        else:
            self.data_degraded = ""

    def record_entry_gate(self, reason: str, now: datetime | None = None) -> bool:
        """Persist the entry gate's state. Returns True when it changed."""
        if reason == self.entry_halt:
            return False
        self.entry_halt = reason
        self.entry_halt_since = (now or datetime.now(UTC)).isoformat() if reason else ""
        self.save()
        return True

    def record_realized_pnl(self, pnl: float, now: datetime | None = None) -> None:
        """Accumulate realised losses for the UTC day; exits are never gated."""
        now = now or datetime.now(UTC)
        self._roll_day(now)
        if pnl < 0:
            self.daily_realized_loss -= pnl
        self.save()

    def _roll_day(self, now: datetime) -> None:
        day = now.astimezone(UTC).date().isoformat()
        if self.daily_loss_date != day:
            self.daily_loss_date = day
            self.daily_realized_loss = 0.0

    def save(self) -> None:
        if not self.persist:
            return
        payload = {**asdict(self), "updated_at": datetime.now(UTC).isoformat()}
        for key in _RUNTIME_FIELDS:
            payload.pop(key)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with tmp.open("w") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            raise StatePersistenceError(f"risk state was not persisted: {exc}") from exc
