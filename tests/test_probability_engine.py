from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import log, sqrt
from statistics import NormalDist

import pytest

from turtlequant.market_parser import MarketParams, OptionType
from turtlequant.probability_engine import (
    barrier_down_probability,
    barrier_probability,
    digital_probability,
    smile_probability,
    terminal_above_probability,
)

N = NormalDist()


def _black76_call(F, K, T, sigma):
    s = sigma * sqrt(T)
    d1 = (log(F / K) + 0.5 * s * s) / s
    return F * N.cdf(d1) - K * N.cdf(d1 - s)


def test_terminal_probability_without_skew_is_n_d2_on_the_forward():
    assert terminal_above_probability(105.0, 100.0, 0.25, 0.6) == pytest.approx(
        digital_probability(105.0, 100.0, 0.25, 0.6, r=0.0)
    )


def test_skew_term_matches_minus_dc_dk_on_a_sloped_smile():
    F, T, K = 100.0, 0.25, 95.0
    slope = -0.004  # IV falls 0.4 vol points per unit of strike

    def smile(k):
        return 0.6 + slope * (k - K)

    h = 1e-3
    numeric = -(_black76_call(F, K + h, T, smile(K + h)) - _black76_call(F, K - h, T, smile(K - h))) / (2 * h)

    assert terminal_above_probability(F, K, T, smile(K), slope) == pytest.approx(numeric, abs=1e-5)
    assert numeric > terminal_above_probability(F, K, T, smile(K))  # put skew raises P(above)


def _params(kind, strike, days=30):
    return MarketParams("btc", strike, datetime.now(UTC) + timedelta(days=days), kind)


def test_smile_touch_without_skew_equals_flat_reflection_with_forward_drift():
    spot, forward, sigma, days = 100.0, 101.0, 0.6, 30
    T = days / 365
    drift = log(forward / spot) / T

    up = smile_probability(_params(OptionType.BARRIER, 120.0, days), spot, forward, sigma, 0.0)
    down = smile_probability(_params(OptionType.BARRIER_DOWN, 80.0, days), spot, forward, sigma, 0.0)

    assert up == pytest.approx(barrier_probability(spot, 120.0, T, sigma, drift), rel=1e-3)
    assert down == pytest.approx(barrier_down_probability(spot, 80.0, T, sigma, drift), rel=1e-3)


def test_put_skew_lowers_the_down_touch_the_legacy_model_overprices():
    params = _params(OptionType.BARRIER_DOWN, 80.0)
    flat = smile_probability(params, 100.0, 100.0, 0.8, 0.0)  # priced at the put wing's IV
    skewed = smile_probability(params, 100.0, 100.0, 0.8, -0.01)

    assert skewed < flat


def test_already_touched_barrier_is_certain():
    assert smile_probability(_params(OptionType.BARRIER, 90.0), 100.0, 100.0, 0.6, -0.01) == 1.0
