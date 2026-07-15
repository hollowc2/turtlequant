import pytest


def test_data_binance_import():
    from turtlequant.data.binance import fetch_klines
    assert callable(fetch_klines)


def test_turtlequant_core_imports():
    from turtlequant import (
        MarketScanner,
        PositionManager,
        VolSurface,
        compute_probability,
        parse_market,
    )
    assert MarketScanner is not None
    assert PositionManager is not None
    assert VolSurface is not None
    assert callable(compute_probability)
    assert callable(parse_market)


def test_digital_probability_known_value():
    from turtlequant.probability_engine import digital_probability

    assert digital_probability(100, 100, 1, 0.2) == pytest.approx(0.5596176924)


def test_down_barrier_known_value():
    from turtlequant.probability_engine import barrier_down_probability

    assert barrier_down_probability(3000, 1500, 0.47, 0.70) == pytest.approx(
        0.1935560612
    )
