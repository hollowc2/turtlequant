"""Volatility regime detector — composite signal from vol acceleration,
liquidation volume, and funding rate.

Regime levels:
  normal   — standard conditions; conservative edge threshold; 60s scan
  elevated — vol increasing; slightly more aggressive; 20s scan
  spike    — vol event; tightest edge threshold; fastest scan (5s)

Each level returns a full set of operational parameters for the strategy loop.
The regime is a soft gate — the bot always runs, but thresholds and cadence
adapt to conditions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class RegimeState:
    """Current market regime and derived operational parameters."""

    level: Literal["normal", "elevated", "spike"]
    score: float  # composite signal, 0.0–1.0
    vol_acceleration: float  # vol_5m / vol_24h
    funding_signal: float  # normalized 0–1
    liquidation_signal: float  # normalized 0–1
    # Operational parameters (set by level):
    scan_interval_secs: int  # how often to run a full scan cycle
    edge_threshold: float  # minimum MC edge required to enter a trade
    size_multiplier: float  # multiplier applied to Kelly size
    min_hours_to_expiry: float  # minimum hours remaining for a market to be eligible


_REGIME_OPS: dict[str, dict] = {
    "normal": {"scan_interval_secs": 60, "edge_threshold": 0.06, "size_multiplier": 1.0, "min_hours_to_expiry": 24.0},
    "elevated": {"scan_interval_secs": 20, "edge_threshold": 0.04, "size_multiplier": 1.2, "min_hours_to_expiry": 12.0},
    "spike": {"scan_interval_secs": 5, "edge_threshold": 0.03, "size_multiplier": 1.5, "min_hours_to_expiry": 6.0},
}


_RECENT_BARS = 12  # 12 × 5m = 1h recent window for vol acceleration


def get_regime(
    spot_prices_5m: np.ndarray,
    funding_zscore: float = 0.0,
    liq_volume_1h_usd: float = 0.0,
    liq_baseline_usd: float = 1.0,
    spot_prices_1h: np.ndarray | None = None,  # unused; kept for API compat
) -> RegimeState:
    """Compute the current volatility regime from market microstructure data.

    Vol acceleration is computed as same-frequency ratio (recent 1h vs full 5m
    baseline), eliminating the cross-frequency microstructure upward bias that
    occurs when comparing annualised 5m vol against annualised 1h vol.

    Args:
        spot_prices_5m:    Array of recent 5m close prices (≥288 preferred for a
                           24h baseline; minimum 13 for a meaningful ratio).
        funding_zscore:    Funding rate z-score (from enrich_candles).
                           |zscore| > 3 = full liquidation-pressure signal.
        liq_volume_1h_usd: USD liquidation volume in the last 1h (from enrich_candles).
        liq_baseline_usd:  30-day average hourly liquidation volume (normalization
                           baseline).  If 0, liquidation signal is suppressed.
        spot_prices_1h:    Deprecated; no longer used for vol computation.

    Returns:
        RegimeState with level and operational parameters.
    """
    # --- Vol signals (same-frequency ratio — microstructure bias cancels) ---
    recent = spot_prices_5m[-_RECENT_BARS:] if len(spot_prices_5m) >= _RECENT_BARS else spot_prices_5m
    vol_recent = _realized_vol(recent, bars_per_year=105_120.0)
    vol_baseline = _realized_vol(spot_prices_5m, bars_per_year=105_120.0)

    vol_acceleration = vol_recent / max(vol_baseline, 1e-8)

    # Normalise each signal to [0, 1].
    # Divisor 3.0: same-frequency rest sits near 1.0×; a genuine spike
    # reaches 2–3×.  (Cross-frequency version used 4.0 but rested at ~1.5×
    # due to microstructure noise amplification — now corrected.)
    vol_sig = min(vol_acceleration / 3.0, 1.0)  # 3× vol = full signal
    fund_sig = min(abs(funding_zscore) / 3.0, 1.0)  # 3σ funding = full signal
    if liq_baseline_usd > 0:
        liq_sig = min(liq_volume_1h_usd / (liq_baseline_usd * 3.0), 1.0)  # 3× baseline = full
    else:
        liq_sig = 0.0

    # Composite score — vol weighted
    score = 0.6 * vol_sig + 0.2 * fund_sig + 0.2 * liq_sig

    if score > 0.65:
        level: Literal["normal", "elevated", "spike"] = "spike"
    elif score > 0.35:
        level = "elevated"
    else:
        level = "normal"

    ops = _REGIME_OPS[level]
    state = RegimeState(
        level=level,
        score=round(score, 4),
        vol_acceleration=round(vol_acceleration, 3),
        funding_signal=round(fund_sig, 3),
        liquidation_signal=round(liq_sig, 3),
        scan_interval_secs=ops["scan_interval_secs"],
        edge_threshold=ops["edge_threshold"],
        size_multiplier=ops["size_multiplier"],
        min_hours_to_expiry=ops["min_hours_to_expiry"],
    )
    logger.info(
        "Regime: %-8s  score=%.3f  vol_accel=%.2f×  fund=%.2f  liq=%.2f",
        level.upper(),
        score,
        vol_acceleration,
        fund_sig,
        liq_sig,
    )
    return state


def default_regime() -> RegimeState:
    """Return a normal-regime RegimeState used before first data fetch."""
    ops = _REGIME_OPS["normal"]
    return RegimeState(
        level="normal",
        score=0.0,
        vol_acceleration=1.0,
        funding_signal=0.0,
        liquidation_signal=0.0,
        scan_interval_secs=ops["scan_interval_secs"],
        edge_threshold=ops["edge_threshold"],
        size_multiplier=ops["size_multiplier"],
        min_hours_to_expiry=ops["min_hours_to_expiry"],
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _realized_vol(prices: np.ndarray, bars_per_year: float) -> float:
    """Annualised realised vol from a price series."""
    if len(prices) < 2:
        return 0.60
    log_returns = np.diff(np.log(np.asarray(prices, dtype=float)))
    if len(log_returns) == 0 or np.all(log_returns == 0):
        return 0.60
    return float(np.std(log_returns) * np.sqrt(bars_per_year))
