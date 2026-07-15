"""Position manager — Kelly sizing, NAV limits, open positions, exit logic.

State is persisted to a JSON file so the bot can survive restarts.

NAV limits:
  - Max per market:     10% NAV
  - Max per expiry:     15% NAV  (correlated risk control)
  - Max total exposure: 40% NAV

Exit triggers:
  - edge reversed: model_prob < yes_price
  - edge decayed: current edge falls below 40% of entry edge
  - time cleanup: <= 6h to expiry and edge <= 5%
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Default NAV limits
DEFAULT_MAX_PER_MARKET_PCT = 0.10
DEFAULT_MAX_PER_EXPIRY_PCT = 0.15
DEFAULT_MAX_TOTAL_EXPOSURE_PCT = 0.40
DEFAULT_KELLY_FRACTION = 0.25

# Conservative flat taker fee applied to both entry and exit contract premium.
# Polymarket charges 30 bps on all crypto markets as of March 2026.
TAKER_FEE_RATE = 0.003

# Default state directory (overridden by --state-dir CLI arg or env)
DEFAULT_STATE_DIR = Path("state/turtlequant")
DEFAULT_POSITIONS_FILE = DEFAULT_STATE_DIR / "turtlequant-positions.json"


@dataclass
class Position:
    """An open position on a Polymarket YES token."""

    market_id: str
    question: str
    asset: str
    strike: float
    expiry_iso: str  # ISO 8601 UTC
    option_type: str  # "european" | "barrier"
    yes_token_id: str
    entry_price: float  # what we paid per YES token
    size_usd: float  # notional size in USD
    model_prob_at_entry: float
    edge_at_entry: float
    opened_at: str  # ISO 8601 UTC
    token_size: float = 0.0  # YES shares actually filled
    fill_confirmed: bool = False  # True once the order is confirmed filled
    status: str = "open"  # "open" | "pending_redemption"
    resolution_price: float | None = None
    entry_fee_usd: float | None = None  # actual reconciled entry fee, when known
    realized_exit_fees_usd: float = 0.0
    last_yes_price: float = 0.0  # last observed market YES price
    last_yes_price_at: str = ""  # ISO 8601 UTC for last observed market price
    last_bid: float = 0.0
    last_ask: float = 0.0

    @property
    def expiry(self) -> datetime:
        return datetime.fromisoformat(self.expiry_iso)


@dataclass(frozen=True)
class ExitDecision:
    """Result of evaluating whether an open position should be closed."""

    should_exit: bool
    reason: str | None = None
    current_edge: float = 0.0
    entry_edge: float = 0.0
    hours_to_expiry: float | None = None


@dataclass
class PositionManager:
    """Manages all open TurtleQuant positions with Kelly sizing and NAV limits."""

    starting_nav: float = 1000.0  # USD — updated as P&L accrues
    current_nav: float = 0.0
    total_pnl: float = 0.0  # cumulative realised P&L across all closed trades
    kelly_fraction: float = DEFAULT_KELLY_FRACTION
    max_per_market_pct: float = DEFAULT_MAX_PER_MARKET_PCT
    max_per_expiry_pct: float = DEFAULT_MAX_PER_EXPIRY_PCT
    max_total_exposure_pct: float = DEFAULT_MAX_TOTAL_EXPOSURE_PCT
    positions_file: Path = field(default_factory=lambda: DEFAULT_POSITIONS_FILE)
    _positions: dict[str, Position] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.current_nav <= 0:
            self.current_nav = self.starting_nav
        self._load()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def has_position(self, market_id: str) -> bool:
        return market_id in self._positions

    def get_position(self, market_id: str) -> Position | None:
        return self._positions.get(market_id)

    def all_positions(self) -> list[Position]:
        return list(self._positions.values())

    def marked_equity(self) -> float:
        """Current NAV including the latest persisted mark for each open claim."""
        return self.current_nav + sum(
            ((p.resolution_price if p.status == "pending_redemption" else p.last_bid or p.last_yes_price or p.entry_price) - p.entry_price)
            * (p.token_size or p.size_usd / p.entry_price)
            for p in self._positions.values()
        )

    def kelly_size(
        self,
        edge: float,
        model_p: float,
        yes_price: float,
    ) -> float:
        """Fractional Kelly position size in USD, capped by NAV limits.

        Args:
            edge:      model_p - yes_price
            model_p:   Model probability of YES resolving 1
            yes_price: Current market price of YES token
            (NAV, fraction, limits taken from self)

        Returns:
            Recommended USD size, potentially 0 if limits are already hit.
        """
        if yes_price <= 0 or yes_price >= 1 or model_p <= 0 or edge <= 0:
            return 0.0

        # Kelly formula: f* = (b*p - q) / b,  b = (1-price)/price
        b = (1.0 - yes_price) / yes_price
        q = 1.0 - model_p
        f_full = (b * model_p - q) / b
        if f_full <= 0:
            return 0.0

        f = f_full * self.kelly_fraction
        raw_size = f * self.current_nav

        # Cap by per-market limit
        max_market = self.max_per_market_pct * self.current_nav
        size = min(raw_size, max_market)

        # Cap by remaining total-exposure headroom
        current_exposure = sum(p.size_usd for p in self._positions.values())
        max_total = self.max_total_exposure_pct * self.current_nav
        headroom = max(0.0, max_total - current_exposure)
        size = min(size, headroom)

        return max(0.0, size)

    def expiry_exposure(self, expiry: datetime) -> float:
        """Total USD currently exposed to positions expiring on the same date."""
        target_date = expiry.date()
        return sum(p.size_usd for p in self._positions.values() if p.expiry.date() == target_date)

    def has_expiry_headroom(self, expiry: datetime, size_usd: float) -> bool:
        """Returns True if adding size_usd does not breach per-expiry NAV cap."""
        current = self.expiry_exposure(expiry)
        cap = self.max_per_expiry_pct * self.current_nav
        return (current + size_usd) <= cap

    def open_position(self, position: Position) -> None:
        self._positions[position.market_id] = position
        logger.info(
            "Opened position: %s %s K=%.0f exp=%s size=$%.2f edge=+%.3f",
            position.asset.upper(),
            position.option_type,
            position.strike,
            position.expiry_iso[:10],
            position.size_usd,
            position.edge_at_entry,
        )
        self._save()

    def record_market_data(
        self,
        market_id: str,
        *,
        yes_token_id: str | None = None,
        yes_price: float | None = None,
        bid: float | None = None,
        ask: float | None = None,
        observed_at: datetime | None = None,
    ) -> bool:
        """Persist the latest market snapshot for an open position.

        This keeps the last observed real quote available even if the market
        later falls out of the active scan set.
        """
        pos = self._positions.get(market_id)
        if not pos:
            return False

        changed = False
        if yes_token_id and yes_token_id != pos.yes_token_id:
            pos.yes_token_id = yes_token_id
            changed = True
        if yes_price is not None and yes_price > 0:
            pos.last_yes_price = yes_price
            pos.last_yes_price_at = (observed_at or datetime.now(UTC)).isoformat()
            changed = True
        if bid is not None and bid > 0:
            pos.last_bid = bid
            changed = True
        if ask is not None and ask > 0:
            pos.last_ask = ask
            changed = True
        if changed:
            self._save()
        return changed

    def close_position(
        self,
        market_id: str,
        exit_price: float,
        reason: str = "edge_reversed",
        filled_shares: float | None = None,
        *,
        exit_fee_usd: float | None = None,
    ) -> tuple[Position | None, float]:
        """Close a position and return (position, realised_pnl).

        P&L formula:
            tokens_held = size_usd / entry_price
            pnl         = (exit_price - entry_price) * tokens_held
        """
        pos = self._positions.get(market_id)
        pnl = 0.0
        if pos:
            tokens = pos.token_size if pos.token_size > 0 else pos.size_usd / pos.entry_price if pos.entry_price > 0 else 0.0
            closed_tokens = min(tokens, filled_shares) if filled_shares is not None and filled_shares > 0 else tokens
            if closed_tokens <= 0:
                return pos, 0.0
            gross_pnl = (exit_price - pos.entry_price) * closed_tokens
            close_ratio = closed_tokens / tokens if tokens > 0 else 1.0
            entry_fee = (
                pos.entry_fee_usd * close_ratio
                if pos.entry_fee_usd is not None
                else pos.size_usd * close_ratio * TAKER_FEE_RATE
            )
            exit_fee = (
                exit_fee_usd
                if exit_fee_usd is not None
                else closed_tokens * exit_price * TAKER_FEE_RATE
            )
            if exit_fee < 0 or not math.isfinite(exit_fee):
                raise ValueError("exit_fee_usd must be a finite non-negative number")
            pnl = gross_pnl - entry_fee - exit_fee
            self.current_nav += pnl
            self.total_pnl += pnl
            if closed_tokens >= tokens - 1e-6:
                self._positions.pop(market_id, None)
            else:
                pos.token_size = tokens - closed_tokens
                pos.size_usd = max(0.0, pos.size_usd * (1.0 - close_ratio))
                pos.realized_exit_fees_usd += exit_fee
                pos.last_yes_price = exit_price
                pos.last_yes_price_at = datetime.now(UTC).isoformat()
            logger.info(
                "Closing position: %s %s K=%.0f — reason=%s exit=%.4f shares=%.4f gross=%+.4f fees=%.4f pnl=%+.4f nav=%.2f",
                pos.asset.upper(),
                pos.option_type,
                pos.strike,
                reason,
                exit_price,
                closed_tokens,
                gross_pnl,
                entry_fee + exit_fee,
                pnl,
                self.current_nav,
            )
            self._save()
        return pos, pnl

    def confirm_fill(
        self,
        market_id: str,
        fill_price: float,
        yes_token_id: str | None = None,
        *,
        size_usd: float | None = None,
        token_size: float | None = None,
        bid: float | None = None,
        ask: float | None = None,
        fee_usd: float | None = None,
    ) -> None:
        pos = self._positions.get(market_id)
        if pos:
            pos.entry_price = fill_price
            if size_usd is not None and size_usd > 0:
                pos.size_usd = size_usd
            if token_size is not None and token_size > 0:
                pos.token_size = token_size
            if fee_usd is not None:
                if fee_usd < 0 or not math.isfinite(fee_usd):
                    raise ValueError("fee_usd must be a finite non-negative number")
                pos.entry_fee_usd = fee_usd
            pos.fill_confirmed = True
            pos.last_yes_price = fill_price
            pos.last_yes_price_at = datetime.now(UTC).isoformat()
            if bid is not None and bid > 0:
                pos.last_bid = bid
            if ask is not None and ask > 0:
                pos.last_ask = ask
            if yes_token_id:
                pos.yes_token_id = yes_token_id
            self._save()

    def mark_pending_redemption(self, market_id: str, resolution_price: float) -> bool:
        """Keep resolved claims accounted for until wallet redemption is reconciled."""
        pos = self._positions.get(market_id)
        if pos is None or not 0.0 <= resolution_price <= 1.0:
            return False
        pos.status = "pending_redemption"
        pos.resolution_price = resolution_price
        self._save()
        return True

    def exit_decision(
        self,
        market_id: str,
        model_prob: float,
        yes_price: float,
        now: datetime | None = None,
    ) -> ExitDecision:
        """Evaluate all exit triggers for an open position."""
        pos = self.get_position(market_id)
        if pos is None:
            return ExitDecision(False)

        current_edge = model_prob - yes_price
        entry_edge = pos.edge_at_entry
        now = now or datetime.now(UTC)
        hours_to_expiry = max((pos.expiry - now).total_seconds() / 3600.0, 0.0)

        if current_edge <= 0:
            return ExitDecision(True, "edge_reversed", current_edge, entry_edge, hours_to_expiry)
        if entry_edge > 0 and current_edge <= 0.4 * entry_edge:
            return ExitDecision(True, "edge_decayed", current_edge, entry_edge, hours_to_expiry)
        if hours_to_expiry <= 6.0 and current_edge <= 0.05:
            return ExitDecision(True, "time_cleanup", current_edge, entry_edge, hours_to_expiry)
        return ExitDecision(False, None, current_edge, entry_edge, hours_to_expiry)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.positions_file.exists():
            return
        try:
            with self.positions_file.open() as f:
                data = json.load(f)
            self._load_validated(data)
            logger.info("Loaded %d open positions from %s", len(self._positions), self.positions_file)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RuntimeError(f"unsafe position state: {self.positions_file}: {exc}") from exc

    def _load_validated(self, data: object) -> None:
        if not isinstance(data, dict):
            raise ValueError("state must be an object")
        nav = data.get("nav", self.current_nav)
        total_pnl = data.get("total_pnl", 0.0)
        positions = data.get("positions", [])
        if not isinstance(nav, (int, float)) or not math.isfinite(nav) or nav <= 0:
            raise ValueError("nav must be a positive finite number")
        if not isinstance(total_pnl, (int, float)) or not math.isfinite(total_pnl):
            raise ValueError("total_pnl must be finite")
        if not isinstance(positions, list):
            raise ValueError("positions must be a list")

        loaded: dict[str, Position] = {}
        for pos_data in positions:
            if not isinstance(pos_data, dict):
                raise TypeError("position must be an object")
            pos_data = pos_data.copy()
            if "token_size" not in pos_data:
                entry_price = pos_data.get("entry_price", 0.0)
                size_usd = pos_data.get("size_usd", 0.0)
                pos_data["token_size"] = size_usd / entry_price if entry_price > 0 else 0.0
            pos_data.setdefault("last_bid", 0.0)
            pos_data.setdefault("last_ask", 0.0)
            pos = Position(**pos_data)
            if (
                not pos.market_id
                or pos.market_id in loaded
                or not (0 < pos.entry_price < 1)
                or not math.isfinite(pos.entry_price)
                or not math.isfinite(pos.token_size)
                or pos.token_size <= 0
                or not math.isfinite(pos.size_usd)
                or pos.size_usd <= 0
            ):
                raise ValueError(f"invalid position {pos.market_id!r}")
            expiry = datetime.fromisoformat(pos.expiry_iso)
            if expiry.tzinfo is None:
                raise ValueError(f"position {pos.market_id!r} expiry must include a timezone")
            if pos.last_yes_price <= 0:
                pos.last_yes_price = pos.entry_price
            if not pos.last_yes_price_at:
                pos.last_yes_price_at = pos.opened_at
            loaded[pos.market_id] = pos

        self.current_nav = float(nav)
        self.total_pnl = float(total_pnl)
        self._positions = loaded

    def _save(self) -> None:
        try:
            self.positions_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "nav": self.current_nav,
                "total_pnl": self.total_pnl,
                "updated_at": datetime.now(UTC).isoformat(),
                "positions": [asdict(p) for p in self._positions.values()],
            }
            tmp_file = self.positions_file.with_name(
                f".{self.positions_file.name}.{os.getpid()}.tmp"
            )
            with tmp_file.open("w") as f:
                f.write(json.dumps(data, indent=2))
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.positions_file)
        except OSError as exc:
            raise RuntimeError("position state was not persisted; halt trading") from exc


def make_position(
    market_id: str,
    question: str,
    asset: str,
    strike: float,
    expiry: datetime,
    option_type: str,
    yes_token_id: str,
    yes_price: float,
    size_usd: float,
    model_prob: float,
    token_size: float = 0.0,
) -> Position:
    """Factory helper to build a Position from trade decision data."""
    opened_at = datetime.now(UTC).isoformat()
    return Position(
        market_id=market_id,
        question=question,
        asset=asset,
        strike=strike,
        expiry_iso=expiry.isoformat(),
        option_type=option_type,
        yes_token_id=yes_token_id,
        entry_price=yes_price,
        size_usd=size_usd,
        token_size=token_size if token_size > 0 else size_usd / yes_price if yes_price > 0 else 0.0,
        model_prob_at_entry=model_prob,
        edge_at_entry=model_prob - yes_price,
        opened_at=opened_at,
        last_yes_price=yes_price,
        last_yes_price_at=opened_at,
    )
