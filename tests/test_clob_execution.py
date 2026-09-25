from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from turtlequant.clob_execution import (
    DEFAULT_CRYPTO_FEE,
    BookLevel,
    ExecutionClient,
    FeeSchedule,
    OrderBook,
    OrderSide,
    _polymarket_env,
    estimate_buy_fill,
    estimate_sell_fill,
    taker_fee,
)


def test_buy_fill_uses_ask_depth_and_reports_partial():
    book = OrderBook(
        token_id="yes",
        bids=[BookLevel(0.40, 100)],
        asks=[BookLevel(0.42, 10), BookLevel(0.45, 10)],
    )

    fill = estimate_buy_fill(book, 10.0)

    assert fill.complete is False
    assert fill.filled_usd == 8.7
    assert fill.filled_shares == 20
    assert round(fill.avg_price, 4) == 0.435
    assert round(fill.unfilled_usd, 4) == 1.3


def test_sell_fill_uses_bid_depth_and_reports_partial():
    book = OrderBook(
        token_id="yes",
        bids=[BookLevel(0.39, 5), BookLevel(0.35, 5)],
        asks=[BookLevel(0.42, 100)],
    )

    fill = estimate_sell_fill(book, 12)

    assert fill.complete is False
    assert fill.filled_shares == 10
    assert fill.filled_usd == 3.7
    assert round(fill.avg_price, 4) == 0.37
    assert fill.unfilled_shares == 2


def test_failed_exit_when_no_executable_bid_depth():
    client = ExecutionClient(mode="paper")
    book = OrderBook(token_id="yes", bids=[], asks=[BookLevel(0.50, 100)])

    result = client.sell_yes("yes", 10, book)

    assert result.success is False
    assert result.filled_shares == 0
    assert result.status == "paper"


class _FlakyBookClient:
    def __init__(self):
        self.calls = 0

    def get_order_book(self, _token_id):
        self.calls += 1
        if self.calls < 3:
            raise RuntimeError("timeout")
        return {
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.42", "size": "20"}],
        }


def test_get_order_book_retries_before_synthetic_fallback():
    flaky = _FlakyBookClient()
    client = ExecutionClient(mode="paper", clob_client=flaky)

    book = client.get_order_book("yes", fallback_bid=0.30, fallback_ask=0.50)

    assert flaky.calls == 3
    assert book.best_bid == 0.40
    assert book.best_ask == 0.42
    assert book.source == "clob"


def test_get_order_book_marks_synthetic_fallback_source():
    client = ExecutionClient(mode="paper", clob_client=None)

    book = client.get_order_book("yes", fallback_bid=0.30, fallback_ask=0.50)

    assert book.best_bid == 0.30
    assert book.best_ask == 0.50
    assert book.source == "synthetic"


def test_polymarket_env_reads_crypto_clob_aliases(monkeypatch):
    monkeypatch.setenv("PRIVATE_KEY", "0xabc")
    monkeypatch.setenv("CLOB_API_KEY", "key1")
    monkeypatch.setenv("CLOB_API_SECRET", "sec1")
    monkeypatch.setenv("CLOB_API_PASSPHRASE", "pass1")
    monkeypatch.setenv("FUNDER_ADDRESS", "0xfunder")
    monkeypatch.setenv("SIGNATURE_TYPE", "1")

    pk, key, secret, phrase, sig, funder = _polymarket_env()

    assert pk == "0xabc"
    assert key == "key1"
    assert secret == "sec1"
    assert phrase == "pass1"
    assert sig == 1
    assert funder == "0xfunder"


def test_polymarket_env_ignores_funder_for_eoa_signature(monkeypatch):
    monkeypatch.setenv("PRIVATE_KEY", "0xabc")
    monkeypatch.setenv("FUNDER_ADDRESS", "0xfunder")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "0")

    _, _, _, _, sig, funder = _polymarket_env()

    assert sig == 0
    assert funder == ""


def test_build_clob_client_derives_api_creds_when_not_in_env(monkeypatch):
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.delenv("POLYMARKET_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    fake_creds = MagicMock(api_key="k", api_secret="s", api_passphrase="p")
    fake_client = MagicMock()
    fake_client.create_or_derive_api_key.return_value = fake_creds

    with patch("py_clob_client_v2.ClobClient", return_value=fake_client) as mock_ctor:
        client = ExecutionClient(mode="live", allow_live=True)

    assert client._client is fake_client
    mock_ctor.assert_called_once()
    fake_client.create_or_derive_api_key.assert_called_once()
    fake_client.set_api_creds.assert_called_once_with(fake_creds)


@pytest.mark.parametrize("mode", ["paper", "shadow"])
def test_non_live_client_ignores_wallet_key(monkeypatch, mode):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("POLYMARKET_API_KEY", "k")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "s")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "p")
    fake_client = MagicMock()

    with patch("py_clob_client_v2.ClobClient", return_value=fake_client) as mock_ctor:
        client = ExecutionClient(mode=mode)

    assert client._client is fake_client
    mock_ctor.assert_called_once_with(host=client.host, chain_id=client.chain_id)
    fake_client.create_or_derive_api_key.assert_not_called()
    fake_client.set_api_creds.assert_not_called()


def test_live_sell_records_actual_partial_fill_from_clob_response():
    book = OrderBook(
        token_id="yes",
        bids=[BookLevel(0.40, 10), BookLevel(0.38, 10)],
        asks=[BookLevel(0.45, 100)],
    )
    fake_client = MagicMock()
    fake_client.create_and_post_market_order.return_value = {
        "success": True,
        "status": "matched",
        "orderID": "order-1",
        "takingAmount": "4000000",
        "makingAmount": "10000000",
    }
    client = ExecutionClient(mode="live", allow_live=True, clob_client=fake_client)

    result = client.sell_yes("yes", 25.0, book)

    assert result.side == OrderSide.SELL
    assert result.success is True
    assert result.complete is False
    assert result.filled_shares == 10.0
    assert result.filled_usd == 4.0
    assert result.avg_price == 0.4
    assert result.order_id == "order-1"
    fake_client.create_and_post_market_order.assert_called_once()


def test_live_buy_uses_confirmed_fixed_point_amounts_and_price_limit():
    book = OrderBook(token_id="yes", bids=[BookLevel(0.40, 100)], asks=[BookLevel(0.45, 100)])
    fake_client = MagicMock()
    fake_client.create_and_post_market_order.return_value = {
        "success": True, "status": "matched", "orderID": "order-2",
        "makingAmount": "9000000", "takingAmount": "20000000",
    }

    result = ExecutionClient(mode="live", allow_live=True, clob_client=fake_client).buy_yes(
        "yes", 10.0, book, max_price=0.50
    )

    assert (result.success, result.filled_usd, result.filled_shares, result.avg_price) == (True, 9.0, 20.0, 0.45)
    assert fake_client.create_and_post_market_order.call_args.kwargs["order_args"].price == 0.50


def test_live_ambiguous_response_never_uses_estimated_fill():
    book = OrderBook(token_id="yes", bids=[BookLevel(0.40, 100)], asks=[BookLevel(0.45, 100)])
    fake_client = MagicMock()
    fake_client.create_and_post_market_order.return_value = {"success": True, "status": "delayed"}

    result = ExecutionClient(mode="live", allow_live=True, clob_client=fake_client).buy_yes(
        "yes", 10.0, book, max_price=0.50
    )

    assert result.status == "pending_reconciliation"
    assert result.success is False
    assert result.filled_usd == result.filled_shares == 0.0


def test_live_rejects_synthetic_book_before_order_submission():
    book = OrderBook(token_id="yes", asks=[BookLevel(0.45, 100)], source="synthetic")
    fake_client = MagicMock()

    result = ExecutionClient(mode="live", allow_live=True, clob_client=fake_client).buy_yes(
        "yes", 10.0, book, max_price=0.50
    )

    assert result.success is False
    assert "real CLOB book" in result.error
    fake_client.create_and_post_market_order.assert_not_called()


# Verbatim shape of get_clob_market_info for live BTC/ETH price markets (2026-09-24).
_LIVE_CRYPTO_MARKET_INFO = {"fd": {"r": 0.07, "e": 1, "to": True}}


def test_market_fee_reads_fd_exponent_as_power_not_decimal_scale():
    fake_client = MagicMock(spec=[])
    fake_client.get_clob_market_info = lambda _condition_id: _LIVE_CRYPTO_MARKET_INFO

    fee = ExecutionClient(mode="paper", clob_client=fake_client).get_market_fee("condition")

    assert fee == FeeSchedule(rate=0.07, exponent=1.0)
    # SDK formula: shares * r * (p*(1-p))**e -> 100 * 0.07 * 0.25
    assert abs(fee.fee(100.0, 0.5) - 1.75) < 1e-12


def test_fee_exponent_is_applied_to_price_curve():
    assert abs(FeeSchedule(rate=0.25, exponent=2.0).fee(100.0, 0.5) - 100 * 0.25 * 0.0625) < 1e-12
    assert abs(taker_fee(20.0, 0.45, 0.07) - 0.3465) < 1e-9


def test_market_fee_prefers_market_info_over_base_fee():
    fake_client = MagicMock(spec=[])
    fake_client.get_clob_market_info = lambda _condition_id: _LIVE_CRYPTO_MARKET_INFO
    fake_client.get_fee_rate_bps = lambda _token_id: 1000  # base_fee on the same live markets

    fee = ExecutionClient(mode="paper", clob_client=fake_client).get_market_fee("condition", "yes")

    assert fee is not None and fee.rate == 0.07


def test_market_fee_falls_back_to_bps_without_condition_id():
    fake_client = MagicMock(spec=[])
    fake_client.get_fee_rate_bps = lambda _token_id: 700

    fee = ExecutionClient(mode="paper", clob_client=fake_client).get_market_fee("", "yes")

    assert fee == FeeSchedule(rate=0.07, exponent=1.0)


def test_market_fee_is_cached_per_condition():
    calls = []
    fake_client = MagicMock(spec=[])
    fake_client.get_clob_market_info = lambda cid: calls.append(cid) or _LIVE_CRYPTO_MARKET_INFO
    client = ExecutionClient(mode="paper", clob_client=fake_client)

    client.get_market_fee("condition")
    client.get_market_fee("condition")

    assert calls == ["condition"]


def test_market_fee_zero_rate_is_not_replaced_by_default():
    fake_client = MagicMock(spec=[])
    fake_client.get_clob_market_info = lambda _condition_id: {"fd": {"r": 0, "e": 1}}

    fee = ExecutionClient(mode="paper", clob_client=fake_client).get_market_fee("condition")

    assert fee == FeeSchedule(rate=0.0, exponent=1.0)


def test_shadow_fill_records_fee_from_schedule():
    client = ExecutionClient(mode="shadow", clob_client=MagicMock(spec=[]))
    book = OrderBook(token_id="yes", asks=[BookLevel(0.50, 100)])

    result = client.buy_yes("yes", 10.0, book, fee=DEFAULT_CRYPTO_FEE)

    # 20 shares * 0.07 * 0.25
    assert abs((result.fee_usd or 0.0) - 0.35) < 1e-9


def test_market_fee_supports_legacy_market_info_scalar():
    fake_client = MagicMock(spec=[])
    fake_client.getClobMarketInfo = lambda _condition_id: {"feeRate": "1000"}

    assert ExecutionClient(mode="paper", clob_client=fake_client).get_market_fee("condition") == FeeSchedule(0.1)


def test_paper_sell_without_bids_is_not_a_broker_failure():
    book = OrderBook(token_id="yes", asks=[BookLevel(0.45, 100)])

    result = ExecutionClient(mode="paper", clob_client=MagicMock()).sell_yes("yes", 10.0, book)

    assert result.success is False
    assert result.broker_failure is False


def test_live_pre_send_rejection_is_not_a_broker_failure():
    book = OrderBook(token_id="yes", asks=[BookLevel(0.45, 100)], source="synthetic")

    result = ExecutionClient(mode="live", allow_live=True, clob_client=MagicMock()).buy_yes(
        "yes", 10.0, book, max_price=0.50
    )

    assert result.sent is False
    assert result.broker_failure is False


def test_live_submission_exception_is_an_ambiguous_broker_failure():
    book = OrderBook(token_id="yes", bids=[BookLevel(0.40, 100)], asks=[BookLevel(0.45, 100)])
    fake_client = MagicMock()
    fake_client.create_and_post_market_order.side_effect = TimeoutError("read timed out")

    result = ExecutionClient(mode="live", allow_live=True, clob_client=fake_client).buy_yes(
        "yes", 10.0, book, max_price=0.50
    )

    assert result.status == "pending_reconciliation"
    assert result.sent is True
    assert result.broker_failure is True


def test_order_book_404_is_not_retried(monkeypatch):
    class NotFound(Exception):
        status_code = 404

    fake_client = MagicMock()
    fake_client.get_order_book.side_effect = NotFound("No orderbook exists for the requested token id")
    monkeypatch.setattr("turtlequant.clob_execution.time.sleep", lambda _s: None)

    book = ExecutionClient(mode="paper", clob_client=fake_client).get_order_book(
        "gone", fallback_bid=0.3, fallback_ask=0.4
    )

    assert fake_client.get_order_book.call_count == 1
    assert book.source == "synthetic"


def test_book_contract_on_a_real_clob_response():
    # Real /book response (trimmed to the 5 best levels per side). The CLOB
    # lists bids ascending and asks descending: the best levels come last.
    import json
    from pathlib import Path

    raw = json.loads((Path(__file__).parent / "fixtures" / "clob_book.json").read_text())
    client = MagicMock()
    client.get_order_book.return_value = raw

    book = ExecutionClient(mode="paper", clob_client=client).get_order_book(raw["asset_id"])

    assert book.source == "clob"
    assert book.best_bid == max(float(level["price"]) for level in raw["bids"])
    assert book.best_ask == min(float(level["price"]) for level in raw["asks"])
    assert book.best_bid < book.best_ask
