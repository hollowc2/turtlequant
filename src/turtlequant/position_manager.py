"""Position manager — Kelly sizing, NAV limits, open positions, exit logic.

State is persisted to a JSON file so the bot can survive restarts.

NAV limits:
  - Max per market:     10% NAV
  - Max per expiry:     15% NAV  (correlated risk control)
  - Max total exposure: 40% NAV

Exit trigger: close when model_prob < yes_price (edge reversed, not just shrunk).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Default NAV limits
DEFAULT_MAX_PER_MARKET_PCT = 0.10
DEFAULT_MAX_PER_EXPIRY_PCT = 0.15
DEFAULT_MAX_TOTAL_EXPOSURE_PCT = 0.40
DEFAULT_KELLY_FRACTION = 0.25

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
    fill_confirmed: bool = False  # True once the order is confirmed filled

    @property
    def expiry(self) -> datetime:
        return datetime.fromisoformat(self.expiry_iso)


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

    def close_position(
        self, market_id: str, exit_price: float, reason: str = "edge_reversed"
    ) -> tuple[Position | None, float]:
        """Close a position and return (position, realised_pnl).

        P&L formula:
            tokens_held = size_usd / entry_price
            pnl         = (exit_price - entry_price) * tokens_held
        """
        pos = self._positions.pop(market_id, None)
        pnl = 0.0
        if pos:
            tokens = pos.size_usd / pos.entry_price if pos.entry_price > 0 else 0.0
            pnl = (exit_price - pos.entry_price) * tokens
            self.current_nav += pnl
            self.total_pnl += pnl
            logger.info(
                "Closing position: %s %s K=%.0f — reason=%s exit=%.4f pnl=%+.4f nav=%.2f",
                pos.asset.upper(),
                pos.option_type,
                pos.strike,
                reason,
                exit_price,
                pnl,
                self.current_nav,
            )
            self._save()
        return pos, pnl

    def confirm_fill(self, market_id: str, fill_price: float) -> None:
        pos = self._positions.get(market_id)
        if pos:
            pos.entry_price = fill_price
            pos.fill_confirmed = True
            self._save()

    def update_nav(self, new_nav: float) -> None:
        self.current_nav = new_nav

    def should_exit(self, market_id: str, model_prob: float, yes_price: float) -> bool:
        """Returns True if edge has reversed (model_prob < yes_price)."""
        if not self.has_position(market_id):
            return False
        return model_prob < yes_price

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.positions_file.exists():
            return
        try:
            with self.positions_file.open() as f:
                data = json.load(f)
            nav = data.get("nav", self.current_nav)
            if nav > 0:
                self.current_nav = nav
            self.total_pnl = data.get("total_pnl", 0.0)
            for pos_data in data.get("positions", []):
                pos = Position(**pos_data)
                self._positions[pos.market_id] = pos
            logger.info("Loaded %d open positions from %s", len(self._positions), self.positions_file)
        except Exception as exc:
            logger.warning("Could not load positions file: %s", exc)

    def _save(self) -> None:
        try:
            self.positions_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "nav": self.current_nav,
                "total_pnl": self.total_pnl,
                "updated_at": datetime.now(UTC).isoformat(),
                "positions": [asdict(p) for p in self._positions.values()],
            }
            self.positions_file.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("Could not save positions file: %s", exc)


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
) -> Position:
    """Factory helper to build a Position from trade decision data."""
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
        model_prob_at_entry=model_prob,
        edge_at_entry=model_prob - yes_price,
        opened_at=datetime.now(UTC).isoformat(),
    )
