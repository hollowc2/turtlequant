from __future__ import annotations

from turtlequant.clob_execution import BookLevel, ExecutionClient, OrderBook, estimate_buy_fill, estimate_sell_fill


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
