"""Probability engine — risk-neutral digital and barrier option pricing.

European digital: P(S_T > K) = N(d2) from Black-Scholes.
Barrier (touch):  P(max S_t > K for any t in [0,T]) via reflection principle.

Both use risk-neutral drift r ≈ 5% annualized.
Barrier probability is always ≥ European probability for the same K / T.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import numpy as np
from scipy.stats import norm

from .market_parser import MarketParams, OptionType

logger = logging.getLogger(__name__)

# Risk-free rate (annualized) — matches crypto perpetual funding roughly
RISK_FREE_RATE: float = 0.05


def digital_probability(
    S0: float,
    K: float,
    T: float,
    sigma: float,
    r: float = RISK_FREE_RATE,
) -> float:
    """P(S_T > K) under risk-neutral measure — N(d2) from Black-Scholes.

    Args:
        S0:    Current spot price
        K:     Strike price
        T:     Time to expiry in years
        sigma: Annualized implied volatility (e.g., 0.65 for 65%)
        r:     Risk-free rate (annualized)

    Returns:
        Probability ∈ (0, 1). Returns 0.0 or 1.0 for degenerate inputs.
    """
    if T <= 0 or sigma <= 0 or S0 <= 0 or K <= 0:
        logger.debug("digital_probability: degenerate input S0=%.2f K=%.2f T=%.6f σ=%.4f", S0, K, T, sigma)
        return 1.0 if S0 > K else 0.0

    d2 = (np.log(S0 / K) + (r - 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    p = float(norm.cdf(d2))
    return max(1e-6, min(1.0 - 1e-6, p))


def barrier_probability(
    S0: float,
    K: float,
    T: float,
    sigma: float,
    r: float = RISK_FREE_RATE,
) -> float:
    """P(max(S_t) >= K for any t in [0,T]) — reflection principle.

    For questions like "Will BTC touch/reach $X before [date]?".
    Always ≥ digital_probability for same inputs.

    Derived from the standard first-passage result for drifted Brownian motion
    (Karatzas & Shreve). For GBM X_t = log(S_t/S_0) = μt + σW_t:

        P = N(d_plus) + (K/S0)^(2μ/σ²) × N(d_minus)

    where:
        μ = r - 0.5σ²
        d_plus  = (log(S0/K) + μT) / (σ√T)
        d_minus = (log(S0/K) - μT) / (σ√T)
        (K/S0)^(2μ/σ²) = exp(2μ × log(K/S0) / σ²) ∈ (0,1) when μ<0, K>S0

    Args:
        S0:    Current spot price
        K:     Strike (barrier) price
        T:     Time horizon in years
        sigma: Annualized volatility
        r:     Risk-free rate

    Returns:
        Probability ∈ (0, 1).
    """
    if T <= 0 or sigma <= 0 or S0 <= 0 or K <= 0:
        return 1.0 if S0 >= K else 0.0

    # Already at or above barrier
    if S0 >= K:
        return 1.0

    mu = r - 0.5 * sigma**2
    sqrtT = np.sqrt(T)
    log_S0_K = np.log(S0 / K)  # negative when S0 < K

    d_plus = (log_S0_K + mu * T) / (sigma * sqrtT)
    d_minus = (log_S0_K - mu * T) / (sigma * sqrtT)

    # Reflection coefficient: (K/S0)^(2μ/σ²) = exp(2μ × log(K/S0)/σ²)
    # When μ < 0 and K > S0: coefficient ∈ (0, 1)
    log_K_S0 = -log_S0_K  # positive
    reflection_factor = float(np.exp(2 * mu * log_K_S0 / sigma**2))

    p = float(norm.cdf(d_plus) + reflection_factor * norm.cdf(d_minus))
    return max(1e-6, min(1.0 - 1e-6, p))


def european_put_probability(
    S0: float,
    K: float,
    T: float,
    sigma: float,
    r: float = RISK_FREE_RATE,
) -> float:
    """P(S_T < K) at expiry — N(-d2), complement of digital call."""
    return 1.0 - digital_probability(S0, K, T, sigma, r)


def barrier_down_probability(
    S0: float,
    K: float,
    T: float,
    sigma: float,
    r: float = RISK_FREE_RATE,
) -> float:
    """P(min(S_t) <= K for any t in [0,T]) — downside barrier touch.

    Symmetrical to barrier_probability: by put-call symmetry, the probability
    of touching a lower barrier K < S0 is equivalent to barrier_probability
    for an up-barrier with S0' = K²/S0 (reflection). We use the direct formula:

        P(min S_t <= K) = N(-d_plus) + (K/S0)^(2μ/σ²+2) × N(d_minus')

    Standard result (e.g. Shreve): same reflection formula but for lower barrier.

        P = N((log(S0/K) - μT)/(σ√T)) ... Actually:

    P(τ_K^- <= T) = N((log(K/S0) + μT)/(σ√T)) + (K/S0)^(2μ/σ²) × N((log(K/S0) - μT)/(σ√T))

    where μ = r - σ²/2.
    """
    if T <= 0 or sigma <= 0 or S0 <= 0 or K <= 0:
        return 1.0 if S0 <= K else 0.0

    # Already at or below barrier
    if S0 <= K:
        return 1.0

    mu = r - 0.5 * sigma**2
    sqrtT = np.sqrt(T)
    log_K_S0 = np.log(K / S0)  # negative when K < S0

    d_plus = (log_K_S0 + mu * T) / (sigma * sqrtT)
    d_minus = (log_K_S0 - mu * T) / (sigma * sqrtT)

    # Reflection coefficient: (K/S0)^(2μ/σ²) — with K<S0 and μ<0: >1 possible, but formula stays valid
    log_S0_K = -log_K_S0  # positive
    reflection_factor = float(np.exp(-2 * mu * log_S0_K / sigma**2))

    p = float(norm.cdf(d_plus) + reflection_factor * norm.cdf(d_minus))
    return max(1e-6, min(1.0 - 1e-6, p))


def compute_probability(params: MarketParams, spot: float, sigma: float) -> float:
    """Dispatch to digital or barrier pricing based on option_type.

    Args:
        params: Parsed market parameters (asset, strike, expiry, option_type).
        spot:   Current spot price for params.asset.
        sigma:  Annualized implied/realized vol.

    Returns:
        Model probability ∈ (0, 1).
    """
    now = datetime.now(UTC)
    T = max((params.expiry - now).total_seconds() / (365 * 86400), 1e-6)

    if params.option_type == OptionType.EUROPEAN:
        prob = digital_probability(spot, params.strike, T, sigma)
    elif params.option_type == OptionType.BARRIER:
        prob = barrier_probability(spot, params.strike, T, sigma)
    elif params.option_type == OptionType.EUROPEAN_PUT:
        prob = european_put_probability(spot, params.strike, T, sigma)
    elif params.option_type == OptionType.BARRIER_DOWN:
        prob = barrier_down_probability(spot, params.strike, T, sigma)
    else:
        prob = digital_probability(spot, params.strike, T, sigma)

    logger.debug(
        "%s %s %s K=%.0f T=%.4fy σ=%.3f → model_p=%.4f",
        params.option_type.value,
        params.asset.upper(),
        params.expiry.strftime("%Y-%m-%d"),
        params.strike,
        T,
        sigma,
        prob,
    )
    return prob
