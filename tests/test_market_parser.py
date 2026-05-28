from __future__ import annotations

from datetime import UTC, datetime, timedelta

from turtlequant.market_parser import parse_market


def test_parse_market_scales_k_suffix_strike():
    params = parse_market(
        "Will BTC be above $75k by June 30?", datetime.now(UTC) + timedelta(days=30)
    )

    assert params is not None
    assert params.strike == 75_000.0


def test_parse_market_keeps_plain_strike_value():
    params = parse_market(
        "Will Ethereum dip to $1,500 by December 31, 2026?",
        datetime.now(UTC) + timedelta(days=30),
    )

    assert params is not None
    assert params.strike == 1_500.0
