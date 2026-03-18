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


def test_slowquant_imports():
    from turtlequant.slowquant import (
        JumpParams,
        RegimeState,
        SlowQuantRunner,
        calibrate_jump_params,
        get_regime,
        score_opportunity,
    )
    assert SlowQuantRunner is not None
    assert callable(calibrate_jump_params)
    assert callable(get_regime)
    assert callable(score_opportunity)
