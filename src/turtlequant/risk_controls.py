"""Small persistent entry gate; exits remain available when it is closed."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class RiskControls:
    state_dir: Path
    high_water: float
    consecutive_failures: int = 0
    halt_reason: str = ""
    daily_loss_date: str = ""
    daily_realized_loss: float = 0.0

    @property
    def path(self) -> Path:
        return self.state_dir / "turtlequant-risk.json"

    @classmethod
    def load(cls, state_dir: Path, equity: float) -> "RiskControls":
        path = state_dir / "turtlequant-risk.json"
        if not path.exists():
            return cls(state_dir, equity)
        try:
            raw = json.loads(path.read_text())
            return cls(
                state_dir,
                max(float(raw["high_water"]), equity),
                int(raw.get("consecutive_failures", 0)),
                str(raw.get("halt_reason", "")),
                str(raw.get("daily_loss_date", "")),
                float(raw.get("daily_realized_loss", 0.0)),
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
        if self.consecutive_failures >= 3:
            return False, "three consecutive failures"
        if (
            market_data_at is None
            or (now - market_data_at).total_seconds() > max_market_data_age_secs
        ):
            return False, "stale market data"
        return True, ""

    def record_success(self, equity: float) -> None:
        self.high_water = max(self.high_water, equity)
        self.consecutive_failures = 0
        self.halt_reason = ""
        self.save()

    def record_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        self.halt_reason = reason
        self.save()

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
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {**asdict(self), "updated_at": datetime.now(UTC).isoformat()}
        payload.pop("state_dir")
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with tmp.open("w") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
