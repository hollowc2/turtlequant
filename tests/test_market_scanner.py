from __future__ import annotations

import json
from pathlib import Path

from turtlequant.market_scanner import MarketScanner

FIXTURES = Path(__file__).parent / "fixtures"


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
        return _Response([{"markets": [{"id": "market-1"}]}])


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


def test_fetch_market_quote_reads_best_bid_ask_not_last_trade():
    payload = json.loads((FIXTURES / "gamma_market_open.json").read_text())

    quote = MarketScanner(session=_ResolvedSession(payload)).fetch_market_quote(payload["id"])

    assert quote == (payload["bestBid"], payload["bestAsk"])
    assert payload["price"] is None  # Gamma has no "price" field to fall back to


def test_fetch_market_quote_reports_missing_side_as_zero():
    payload = {"bestAsk": 0.2, "lastTradePrice": 0.9}

    assert MarketScanner(session=_ResolvedSession(payload)).fetch_market_quote("m") == (0.0, 0.2)


def test_fetch_market_quote_handles_api_failure():
    assert MarketScanner(session=_FailingSession()).fetch_market_quote("market-1") is None


def test_markets_fetched_at_keeps_last_fresh_time_on_cache_fallback():
    session = _CachedMarketSession()
    scanner = MarketScanner(session=session)
    scanner._fetch_all_pages()
    fresh = scanner.markets_fetched_at
    assert fresh is not None

    session.fail = True
    scanner._fetch_all_pages()

    assert scanner.markets_fetched_at == fresh


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


class _RecordingSession:
    def __init__(self, payload):
        self.payload = payload
        self.params = []

    def get(self, url, params=None, **_kwargs):
        self.params.append((url, params))
        return _Response(self.payload if not params or params.get("offset", 0) == 0 else [])


def _events_fixture():
    return json.loads((FIXTURES / "gamma_events_crypto_prices.json").read_text())


def test_discovery_queries_crypto_price_events_without_up_or_down():
    session = _RecordingSession(_events_fixture())

    markets = MarketScanner(session=session)._fetch_all_pages()

    url, params = session.params[0]
    assert url.endswith("/events")
    assert params["tag_id"] == 1312 and params["exclude_tag_id"] == 102127
    assert "tag_slug" not in params  # Gamma ignores it (returned NFL/MLB markets)
    assert len(markets) == sum(len(e["markets"]) for e in _events_fixture())
    assert all("Up or Down" not in m["question"] for m in markets)


def test_discovery_skips_closed_markets_inside_open_events():
    events = _events_fixture()
    events[0]["markets"][0]["closed"] = True

    markets = MarketScanner(session=_RecordingSession(events))._fetch_all_pages()

    assert events[0]["markets"][0]["id"] not in {m["id"] for m in markets}


def test_absolute_spread_filter_keeps_cheap_long_dated_markets():
    events = _events_fixture()
    tail = events[-1]["markets"][0]  # real: BTC $200k by Dec 31, 0.013 / 0.016
    assert "December 31" in tail["question"]
    scanner = MarketScanner(session=_RecordingSession(events), assets=["btc"], min_liquidity=0.0)

    passed = {m.market_id for m in scanner.get_active_markets()}

    assert tail["id"] in passed  # a 0.3c spread is ~20% "relative" and was rejected before
    assert scanner.last_scan_counts["fetched"] == sum(len(e["markets"]) for e in events)

    wide = MarketScanner(session=_RecordingSession(events), assets=["btc"], min_liquidity=0.0, max_spread=0.001)
    assert tail["id"] not in {m.market_id for m in wide.get_active_markets()}
    assert wide.last_scan_counts["spread"] >= 1


def test_resolution_contract_on_a_real_resolved_market():
    # Real /markets/{id} for "Ethereum above 2,760 on September 25, 12AM ET?"
    # (resolved NO). Gamma has no resolutionPrice field; outcomePrices is final.
    payload = json.loads((FIXTURES / "gamma_market_resolved.json").read_text())
    assert "resolutionPrice" not in payload
    yes_token = json.loads(payload["clobTokenIds"])[0]

    assert MarketScanner(session=_ResolvedSession(payload)).fetch_resolution(payload["id"], yes_token) == 0.0
