"""Probability engine — risk-neutral digital and barrier option pricing.

European digital: P(S_T > K) = N(d2) from Black-Scholes.
Barrier (touch):  P(max S_t > K for any t in [0,T]) via reflection principle.

Both use risk-neutral drift r ≈ 5% annualized.
Barrier probability is always ≥ European probability for the same K / T.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from math import exp, log, sqrt
from statistics import NormalDist

from .market_parser import MarketParams, OptionType

logger = logging.getLogger(__name__)
_NORMAL = NormalDist()

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

    d2 = (log(S0 / K) + (r - 0.5 * sigma**2) * T) / (sigma * sqrt(T))
    p = _NORMAL.cdf(d2)
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
    sqrtT = sqrt(T)
    log_S0_K = log(S0 / K)  # negative when S0 < K

    d_plus = (log_S0_K + mu * T) / (sigma * sqrtT)
    d_minus = (log_S0_K - mu * T) / (sigma * sqrtT)

    # Reflection coefficient: (K/S0)^(2μ/σ²) = exp(2μ × log(K/S0)/σ²)
    # When μ < 0 and K > S0: coefficient ∈ (0, 1)
    log_K_S0 = -log_S0_K  # positive
    reflection_factor = exp(2 * mu * log_K_S0 / sigma**2)

    p = _NORMAL.cdf(d_plus) + reflection_factor * _NORMAL.cdf(d_minus)
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
    sqrtT = sqrt(T)
    log_K_S0 = log(K / S0)  # negative when K < S0

    d_plus = (log_K_S0 + mu * T) / (sigma * sqrtT)
    d_minus = (log_K_S0 - mu * T) / (sigma * sqrtT)

    # Reflection coefficient: (K/S0)^(2μ/σ²) — with K<S0 and μ<0: >1 possible, but formula stays valid
    log_S0_K = -log_K_S0  # positive
    reflection_factor = exp(-2 * mu * log_S0_K / sigma**2)

    p = _NORMAL.cdf(d_minus) + reflection_factor * _NORMAL.cdf(d_plus)
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


# ---------------------------------------------------------------------------
# Smile-consistent pricing (--pricing-model smile)
# ---------------------------------------------------------------------------


def terminal_above_probability(
    forward: float, K: float, T: float, sigma: float, dsigma_dk: float = 0.0
) -> float:
    """P(S_T > K) = -dC/dK on the smile: N(d2) - vega * dsigma/dK, zero drift on F.

    ``N(d2)`` alone evaluates the digital at the strike's own IV and ignores
    how IV changes with strike; the vega term restores it. ``vega`` is the
    undiscounted forward vega ``F * phi(d1) * sqrt(T)``.
    """
    if T <= 0 or sigma <= 0 or forward <= 0 or K <= 0:
        return 1.0 if forward > K else 0.0
    s = sigma * sqrt(T)
    d1 = (log(forward / K) + 0.5 * s * s) / s
    vega = forward * _NORMAL.pdf(d1) * sqrt(T)
    p = _NORMAL.cdf(d1 - s) - vega * dsigma_dk
    return max(1e-6, min(1.0 - 1e-6, p))


def smile_probability(
    params: MarketParams, spot: float, forward: float, sigma: float, dsigma_dk: float
) -> float:
    """Model probability using the Deribit forward and the smile's slope at K.

    Terminal (european) markets use ``terminal_above_probability``. Touch
    markets take the flat-vol reflection price with the forward's drift and
    scale it by the skew correction of the matching terminal probability
    (``P_touch ~ 2 P_terminal`` near zero drift, so the correction carries
    over). This is an approximation; with no skew it equals the flat price.
    """
    now = datetime.now(UTC)
    T = max((params.expiry - now).total_seconds() / (365 * 86400), 1e-6)
    K = params.strike
    kind = params.option_type
    p_above_flat = terminal_above_probability(forward, K, T, sigma)
    p_above = terminal_above_probability(forward, K, T, sigma, dsigma_dk)
    if kind == OptionType.EUROPEAN:
        return p_above
    if kind == OptionType.EUROPEAN_PUT:
        return max(1e-6, min(1.0 - 1e-6, 1.0 - p_above))

    upward = kind == OptionType.BARRIER
    if (upward and spot >= K) or (not upward and spot <= K):
        return 1.0  # already touched
    drift = log(forward / spot) / T if spot > 0 and forward > 0 else 0.0
    touch = barrier_probability if upward else barrier_down_probability
    flat_touch = touch(spot, K, T, sigma, drift)
    flat_terminal = p_above_flat if upward else 1.0 - p_above_flat
    smile_terminal = p_above if upward else 1.0 - p_above
    p = flat_touch * smile_terminal / flat_terminal if flat_terminal > 1e-9 else flat_touch
    return max(1e-6, min(1.0 - 1e-6, p))
