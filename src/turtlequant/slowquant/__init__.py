"""SlowQuant — Merton jump-diffusion strategy for short-dated Polymarket price threshold markets.

Reuses TurtleQuant infrastructure (scanner, parser, vol surface, position manager).
Adds: Monte Carlo pricing, volatility regime gating, opportunity ranking.

Public API:
    from turtlequant.slowquant.monte_carlo import JumpParams, calibrate_jump_params, simulate
    from turtlequant.slowquant.vol_regime import RegimeState, get_regime
    from turtlequant.slowquant.opportunity_ranker import Opportunity, score_opportunity, should_exit
    from turtlequant.slowquant.strategy_loop import SlowQuantRunner
"""

from .monte_carlo import FALLBACK_JUMP_PARAMS, JumpParams, calibrate_jump_params
from .monte_carlo import simulate as mc_simulate
from .opportunity_ranker import Opportunity, score_opportunity, should_exit, time_decay_multiplier
from .strategy_loop import SlowQuantRunner
from .vol_regime import RegimeState, get_regime

__all__ = [
    "JumpParams",
    "FALLBACK_JUMP_PARAMS",
    "calibrate_jump_params",
    "mc_simulate",
    "RegimeState",
    "get_regime",
    "Opportunity",
    "score_opportunity",
    "should_exit",
    "time_decay_multiplier",
    "SlowQuantRunner",
]
