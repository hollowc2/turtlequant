from __future__ import annotations

from turtlequant.market_scanner import MarketScanner


def test_parse_raw_accepts_gamma_json_string_token_fields():
    scanner = MarketScanner()

    market = scanner._parse_raw(
        {
            "id": "701552",
            "conditionId": "condition-1",
            "question": "Will Ethereum dip to $1,500 by December 31, 2026?",
            "endDate": "2027-01-01T05:00:00Z",
            "clobTokenIds": '["yes-token", "no-token"]',
            "outcomes": '["Yes", "No"]',
            "bestBid": "0.38",
            "bestAsk": "0.40",
            "liquidity": "36104.5944",
            "volume24hr": "13297.018796",
        }
    )

    assert market is not None
    assert market.yes_token_id == "yes-token"
    assert market.no_token_id == "no-token"
    assert market.yes_price == 0.39
