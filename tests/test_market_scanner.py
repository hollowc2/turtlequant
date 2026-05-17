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


class _FailingSession:
    def get(self, *_args, **_kwargs):
        raise RuntimeError("api outage")


class _ResolutionFailureResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"resolutionPrice": "not-a-number"}


class _ResolutionFailureSession:
    def get(self, *_args, **_kwargs):
        return _ResolutionFailureResponse()


def test_fetch_all_pages_handles_api_outage():
    scanner = MarketScanner(session=_FailingSession())

    assert scanner._fetch_all_pages() == []


def test_fetch_market_price_handles_resolution_failure():
    scanner = MarketScanner(session=_ResolutionFailureSession())

    assert scanner.fetch_market_price("market-1") is None
