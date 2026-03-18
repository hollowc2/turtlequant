"""Opportunity ranker — score and sort trade candidates.

Composite score:
    score = mc_edge × liquidity_factor × proximity(log-moneyness) × regime.size_multiplier

Higher score = better trade. Strategy loop executes the top N per cycle.

Also provides exit logic for open positions:
    1. Edge reversed        — model_prob < market_price
    2. Edge decayed 40%     — current_edge < 0.4 × entry_edge
    3. Time decay cleanup   — < 6h remaining AND edge < 5%
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from turtlequant.market_parser import MarketParams
from turtlequant.market_scanner import ActiveMarket
from turtlequant.position_manager import Position

from .vol_regime import RegimeState

logger = logging.getLogger(__name__)


@dataclass
class Opportunity:
    """A ranked trade candidate ready for execution."""

    market: ActiveMarket
    params: MarketParams
    sigma: float
    bs_prob: float
    bs_edge: float
    mc_prob: float
    mc_edge: float
    score: float
    recommended_size_usd: float


def score_opportunity(
    mc_edge: float,
    liquidity_usd: float,
    spot: float,
    strike: float,
    hours_to_expiry: float,  # noqa: ARG001  (reserved for future time-weighting)
    regime: RegimeState,
) -> float:
    """Compute composite opportunity score.

    Args:
        mc_edge:         Monte Carlo edge (mc_prob − yes_price).
        liquidity_usd:   Market liquidity in USD.
        spot:            Current spot price of the underlying.
        strike:          Market strike price.
        hours_to_expiry: Hours until resolution (reserved for future use).
        regime:          Current vol regime state.

    Returns:
        Score ≥ 0. Higher is better.
    """
    if mc_edge <= 0 or spot <= 0:
        return 0.0

    # Proximity to ATM: Gaussian log-moneyness kernel, σ=5%.
    # 1.0 at ATM, ~0.99 at 1% away, ~0.84 at 3% away, ~0.61 at 5% away.
    # More accurate than linear: symmetric around ATM, correct gamma intuition.
    log_moneyness = math.log(spot / strike)
    proximity = math.exp(-0.5 * (log_moneyness / 0.05) ** 2)

    # Liquidity factor: saturates at $50k
    liq_factor = min(liquidity_usd / 50_000.0, 1.0)

    # Near-ATM during a vol spike: non-linear boost (high dP/dS sensitivity).
    # With Gaussian σ=5%, threshold 0.95 fires only within ~1.6% of strike —
    # i.e., the single nearest Polymarket round-number strike.
    if regime.vol_acceleration >= 2.0 and proximity >= 0.95:
        spike_boost = min(regime.vol_acceleration / 2.0, 2.5)
    else:
        spike_boost = 1.0

    score = mc_edge * liq_factor * proximity * regime.size_multiplier * spike_boost
    return max(0.0, score)


def time_decay_multiplier(hours_to_expiry: float) -> float:
    """Position size scaling based on time remaining to expiry.

    Reduces size for short-dated markets where model error is higher
    and gamma risk is elevated.

    Returns:
        1.0 for > 72h, 0.7 for 24–72h, 0.4 for 6–24h.
    """
    if hours_to_expiry > 72:
        return 1.0
    if hours_to_expiry > 24:
        return 0.7
    return 0.4


def should_exit(
    pos: Position,
    current_mc_prob: float,
    current_market_price: float,
    hours_to_expiry: float,
) -> tuple[bool, str]:
    """Determine whether to exit an open position.

    Exit triggers (evaluated in priority order):
      1. edge_reversed       — model_prob < market_price (negative edge)
      2. edge_decayed_40pct  — current_edge < 40% of entry edge
      3. time_decay_cleanup  — < 6h remaining AND edge < 5%

    Args:
        pos:                  The open position (from PositionManager).
        current_mc_prob:      Current MC model probability.
        current_market_price: Current YES token price (mid).
        hours_to_expiry:      Hours until market resolution.

    Returns:
        (should_exit: bool, reason: str)
    """
    current_edge = current_mc_prob - current_market_price
    entry_edge = pos.edge_at_entry

    if current_edge < 0:
        return True, "edge_reversed"

    if entry_edge > 0 and current_edge < 0.4 * entry_edge:
        return True, "edge_decayed_40pct"

    if hours_to_expiry < 6.0 and current_edge < 0.05:
        return True, "time_decay_cleanup"

    return False, ""
