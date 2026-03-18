"""Monte Carlo jump-diffusion engine — Merton model.

Models crypto price paths as:

    dS/S = (μ − λκ) dt + σ dW + J dN

Where:
    μ    = risk-neutral drift
    σ    = diffusion volatility (from VolSurface)
    dW   = Brownian increment
    dN   = Poisson jump process with intensity λ
    J    ~ Normal(μⱼ, σⱼ)   log jump sizes
    κ    = E[eᴶ − 1]         mean jump size correction

Jump parameters are calibrated from recent Binance 1h returns (30d window)
or fall back to conservative crypto defaults.

Uses hourly time steps for accuracy on short-dated markets (3–10d).
A Black-Scholes pre-filter (|bs_edge| >= 3%) gates MC to avoid wasted compute.

Typical performance: 50k paths × 240 hourly steps (10d) in ~80ms on a laptop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

RISK_FREE_RATE: float = 0.05  # annualized, matches turtlequant


# ---------------------------------------------------------------------------
# Jump parameters
# ---------------------------------------------------------------------------


@dataclass
class JumpParams:
    """Merton jump-diffusion parameters."""

    lambda_per_year: float  # jump intensity, annualized (e.g. 15.6 ≈ 0.3/week)
    mu_j: float  # mean log-jump size (risk-neutral, typically ~0)
    sigma_j: float  # std of log-jump (e.g. 0.08 = 8%)

    def kappa(self) -> float:
        """Mean jump size correction: E[e^J − 1]."""
        return float(np.exp(self.mu_j + 0.5 * self.sigma_j**2) - 1)


# Fallback used when calibration has insufficient data (< 5 jump events)
FALLBACK_JUMP_PARAMS = JumpParams(
    lambda_per_year=15.6,  # 0.3 jumps/week × 52 weeks
    mu_j=0.0,
    sigma_j=0.08,
)


def calibrate_jump_params(
    log_returns: np.ndarray,
    jump_threshold: float = 0.04,
    min_jump_count: int = 5,
) -> JumpParams:
    """Estimate Merton jump parameters from a log-return series.

    Assumes log_returns are drawn from 1h Binance candles (annualises by ×8760).

    Args:
        log_returns:    1D array of log returns (e.g. last 30d of 1h bars).
        jump_threshold: Absolute return above which we classify as a jump event.
                        Default 4% — typical for BTC/ETH 1h moves.
        min_jump_count: Fall back to FALLBACK_JUMP_PARAMS if fewer jumps found.

    Returns:
        Calibrated JumpParams, or FALLBACK_JUMP_PARAMS if insufficient data.
    """
    if len(log_returns) < 10:
        logger.debug("calibrate_jump_params: insufficient data (%d pts) — using fallback", len(log_returns))
        return FALLBACK_JUMP_PARAMS

    jump_mask = np.abs(log_returns) > jump_threshold
    n_jumps = int(jump_mask.sum())

    if n_jumps < min_jump_count:
        logger.debug(
            "calibrate_jump_params: only %d jumps (threshold=%.3f) — using fallback",
            n_jumps,
            jump_threshold,
        )
        return FALLBACK_JUMP_PARAMS

    # Annualise: assume 1h bars → 8760 observations per year
    jump_freq_per_obs = n_jumps / len(log_returns)
    lambda_per_year = float(np.clip(jump_freq_per_obs * 8760.0, 4.0, 200.0))

    jump_sizes = log_returns[jump_mask]
    mu_j = float(np.mean(jump_sizes))
    sigma_j = float(np.clip(np.std(jump_sizes), 0.01, 0.30))

    params = JumpParams(lambda_per_year=lambda_per_year, mu_j=mu_j, sigma_j=sigma_j)
    logger.info(
        "Jump calibration: λ=%.1f/yr  μⱼ=%+.4f  σⱼ=%.4f  (%d jumps / %d obs)",
        lambda_per_year,
        mu_j,
        sigma_j,
        n_jumps,
        len(log_returns),
    )
    return params


# ---------------------------------------------------------------------------
# Monte Carlo simulation
# ---------------------------------------------------------------------------


def simulate(
    S0: float,
    K: float,
    T: float,
    sigma: float,
    jump_params: JumpParams,
    n_sims: int = 50_000,
    r: float = RISK_FREE_RATE,
    seed: int | None = None,
) -> float:
    """Estimate P(S_T > K) via Merton jump-diffusion Monte Carlo.

    Uses hourly time steps (minimum 1 step) for accuracy on short-dated markets.

    Jump component uses a Bernoulli approximation per time step:
        - Valid when λ*dt << 1 (holds for hourly steps with λ ≈ 15/yr: λ*dt ≈ 0.0018)
        - P(>1 jump per hour) ≈ 1.6e-6 — negligible

    Args:
        S0:          Current spot price.
        K:           Strike price.
        T:           Time to expiry in years.
        sigma:       Annualised diffusion volatility (from VolSurface).
        jump_params: Calibrated or default Merton jump parameters.
        n_sims:      Number of Monte Carlo paths (default 50,000).
        r:           Risk-free rate (annualised, default 5%).
        seed:        Optional RNG seed for reproducibility.

    Returns:
        Estimated probability ∈ (1e-6, 1−1e-6).
    """
    if T <= 0 or sigma <= 0 or S0 <= 0 or K <= 0:
        return 1.0 if S0 > K else 0.0

    n_steps = max(int(T * 8760), 1)  # hourly steps, minimum 1
    dt = T / n_steps

    rng = np.random.default_rng(seed)

    # Risk-neutral drift corrected for jump compensation
    kappa = jump_params.kappa()
    lam = jump_params.lambda_per_year
    mu_eff = (r - 0.5 * sigma**2 - lam * kappa) * dt

    # Diffusion increments: shape (n_sims, n_steps)
    diffusion = sigma * np.sqrt(dt) * rng.standard_normal((n_sims, n_steps))

    # Jump component — Bernoulli approximation (valid when λ*dt << 1)
    lambda_dt = lam * dt
    jump_occurs = rng.random((n_sims, n_steps)) < lambda_dt
    jump_magnitudes = rng.normal(jump_params.mu_j, jump_params.sigma_j, (n_sims, n_steps))
    jumps = jump_magnitudes * jump_occurs  # zero where no jump

    # Cumulative log price at terminal time
    log_S_T = np.log(S0) + (mu_eff + diffusion + jumps).sum(axis=1)

    p = float(np.mean(log_S_T > np.log(K)))
    return max(1e-6, min(1.0 - 1e-6, p))
