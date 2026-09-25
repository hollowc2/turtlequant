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
    assert market.liquidity_usd == 36104.5944


class _FailingSession:
    def get(self, *_args, **_kwargs):
        raise RuntimeError("api outage")


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _FlakySession:
    def __init__(self):
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return _Response([], status_code=503)
        return _Response([])


class _CachedMarketSession:
    def __init__(self):
        self.fail = False

    def get(self, *_args, **_kwargs):
        if self.fail:
            raise RuntimeError("api outage")
        return _Response([{"id": "market-1"}])


class _ResolutionFailureResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"resolutionPrice": "not-a-number"}


class _ResolutionFailureSession:
    def get(self, *_args, **_kwargs):
        return _ResolutionFailureResponse()


class _ResolvedSession:
    """Serves one market payload shaped like Gamma's /markets/{id}."""

    def __init__(self, payload):
        self.payload = payload

    def get(self, *_args, **_kwargs):
        return _Response(self.payload)


# Verbatim fields from a resolved Gamma market (ETH above 2,600 on Sep 23, 2026).
_RESOLVED_YES = {
    "closed": True,
    "umaResolutionStatus": "resolved",
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["1", "0"]',
    "clobTokenIds": '["yes-token", "no-token"]',
    "lastTradePrice": 0.999,
}


def test_fetch_all_pages_handles_api_outage():
    scanner = MarketScanner(session=_FailingSession())

    assert scanner._fetch_all_pages() == []


def test_fetch_all_pages_handles_transient_status():
    session = _FlakySession()
    scanner = MarketScanner(session=session)

    assert scanner._fetch_all_pages() == []
    assert session.calls == 1


def test_fetch_all_pages_uses_recent_cache_on_outage():
    session = _CachedMarketSession()
    scanner = MarketScanner(session=session)

    assert scanner._fetch_all_pages() == [{"id": "market-1"}]
    session.fail = True

    assert scanner._fetch_all_pages() == [{"id": "market-1"}]


def test_fetch_market_price_handles_resolution_failure():
    scanner = MarketScanner(session=_ResolutionFailureSession())

    assert scanner.fetch_market_price("market-1") is None


def test_fetch_resolution_reads_outcome_prices_of_resolved_market():
    assert MarketScanner(session=_ResolvedSession(_RESOLVED_YES)).fetch_resolution("m", "yes-token") == 1.0
    no_won = {**_RESOLVED_YES, "outcomePrices": '["0", "1"]'}
    assert MarketScanner(session=_ResolvedSession(no_won)).fetch_resolution("m", "yes-token") == 0.0
    split = {**_RESOLVED_YES, "outcomePrices": '["0.5", "0.5"]'}
    assert MarketScanner(session=_ResolvedSession(split)).fetch_resolution("m", "yes-token") == 0.5


def test_fetch_resolution_matches_yes_by_token_id_before_label():
    reordered = {**_RESOLVED_YES, "clobTokenIds": '["no-token", "yes-token"]', "outcomePrices": '["0", "1"]'}
    assert MarketScanner(session=_ResolvedSession(reordered)).fetch_resolution("m", "yes-token") == 1.0


def test_fetch_resolution_ignores_closed_but_unresolved_quotes():
    proposed = {**_RESOLVED_YES, "umaResolutionStatus": "proposed", "outcomePrices": '["0.9995", "0.0005"]'}
    open_market = {**_RESOLVED_YES, "closed": False}
    assert MarketScanner(session=_ResolvedSession(proposed)).fetch_resolution("m", "yes-token") is None
    assert MarketScanner(session=_ResolvedSession(open_market)).fetch_resolution("m", "yes-token") is None
    assert MarketScanner(session=_ResolutionFailureSession()).fetch_resolution("m") is None
