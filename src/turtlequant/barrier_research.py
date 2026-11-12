"""Offline forward/flat-IV touch candidate; not wired into the trader.

The strike IV and forward still come from the smile surface, but a terminal
digital's skew adjustment is not applied to the path-dependent touch payoff.
This is a GBM approximation, not a calibrated local-volatility model.
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite, log

from .market_parser import MarketParams, OptionType
from .probability_engine import barrier_down_probability, barrier_probability


def forward_touch_probability(
    params: MarketParams,
    spot: float,
    forward: float,
    sigma: float,
    *,
    as_of: datetime,
) -> float:
    """Research candidate with explicit valuation time and no terminal scaling.

    Assumes the barrier has not previously touched; current spot crossing is
    certain. Historical touch state must be handled by a future replay.
    """
    if params.option_type not in (OptionType.BARRIER, OptionType.BARRIER_DOWN):
        raise ValueError("forward touch candidate requires a barrier market")
    if any(not isfinite(v) or v <= 0 for v in (spot, forward, sigma, params.strike)):
        raise ValueError("spot, forward, sigma and strike must be finite and positive")
    if as_of.tzinfo is None or params.expiry.tzinfo is None:
        raise ValueError("valuation and expiry must be timezone-aware")
    upward = params.option_type == OptionType.BARRIER
    if (upward and spot >= params.strike) or (not upward and spot <= params.strike):
        return 1.0
    T = (params.expiry - as_of).total_seconds() / (365 * 86400)
    if T <= 0:
        return 0.0
    drift = log(forward / spot) / T
    touch = barrier_probability if upward else barrier_down_probability
    return touch(spot, params.strike, T, sigma, drift)
