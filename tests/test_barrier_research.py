from datetime import UTC, datetime, timedelta
from math import exp, log, sqrt
from statistics import NormalDist

import pytest

from turtlequant.barrier_research import forward_touch_probability
from turtlequant.market_parser import MarketParams, OptionType

NOW = datetime(2026, 10, 5, 14, 9, 30, tzinfo=UTC)


def params(kind, strike=100.0, days=30):
    return MarketParams("btc", strike, NOW + timedelta(days=days), kind)


@pytest.mark.parametrize("kind,direction", [(OptionType.BARRIER, -1), (OptionType.BARRIER_DOWN, 1)])
def test_boundary_is_continuous_from_the_unhit_side(kind, direction):
    p = params(kind)
    near = forward_touch_probability(p, 100 + direction * 1e-6, 101, .6, as_of=NOW)
    assert .9999 < near <= 1
    assert forward_touch_probability(p, 100, 101, .6, as_of=NOW) == 1


@pytest.mark.parametrize("kind,strikes", [
    (OptionType.BARRIER, (101, 110, 120, 140)),
    (OptionType.BARRIER_DOWN, (99, 90, 80, 60)),
])
def test_touch_decreases_with_barrier_distance_and_dominates_flat_terminal(kind, strikes):
    values = []
    T, sigma, forward = 30 / 365, .6, 101
    for K in strikes:
        p = forward_touch_probability(params(kind, K), 100, forward, sigma, as_of=NOW)
        d2 = (log(forward / K) - .5 * sigma**2 * T) / (sigma * sqrt(T))
        terminal = NormalDist().cdf(d2 if kind == OptionType.BARRIER else -d2)
        assert terminal <= p <= 1
        values.append(p)
    assert values == sorted(values, reverse=True)


@pytest.mark.parametrize("kind,K", [(OptionType.BARRIER, 120), (OptionType.BARRIER_DOWN, 80)])
def test_candidate_matches_independent_conditional_bridge_quadrature(kind, K):
    # Deterministic integration over exact GBM endpoints. Conditional crossing
    # probability follows the Brownian bridge, independent of reflection code.
    normal, n = NormalDist(), 20000
    T, sigma, r, spot = .25, .6, .05, 100.
    direction = -1 if kind == OptionType.BARRIER else 1
    x0 = abs(log(spot / K))
    mu = direction * (r - .5 * sigma**2)
    total = 0.
    for i in range(n):
        xT = x0 + mu * T + sigma * sqrt(T) * normal.inv_cdf((i + .5) / n)
        total += 1. if xT <= 0 else exp(-2 * x0 * xT / (sigma**2 * T))
    p = MarketParams("btc", K, NOW + timedelta(days=T * 365), kind)
    actual = forward_touch_probability(p, spot, spot * exp(r*T), sigma, as_of=NOW)
    assert actual == pytest.approx(total / n, abs=1e-4)


def test_expired_unhit_contract_and_invalid_inputs():
    assert forward_touch_probability(params(OptionType.BARRIER, 120, 0), 100, 101, .6, as_of=NOW) == 0
    with pytest.raises(ValueError, match="barrier"):
        forward_touch_probability(params(OptionType.EUROPEAN), 100, 101, .6, as_of=NOW)
    with pytest.raises(ValueError, match="positive"):
        forward_touch_probability(params(OptionType.BARRIER), 100, float("nan"), .6, as_of=NOW)
    with pytest.raises(ValueError, match="timezone"):
        forward_touch_probability(params(OptionType.BARRIER), 100, 101, .6, as_of=NOW.replace(tzinfo=None))
