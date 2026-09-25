"""Main-loop tests: Trader against a fake scanner, CLOB and spot source."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from turtlequant.clob_execution import ExecutionClient
from turtlequant.history import DIAGNOSTICS_JSONL, HISTORY_JSONL
from turtlequant.market_scanner import ActiveMarket
from turtlequant.order_intents import OrderIntentLedger
from turtlequant.position_manager import PositionManager, make_position
from turtlequant.risk_controls import RiskControls
from turtlequant.trader import Trader, TraderConfig, plan_entry

SPOTS = {"BTCUSDT": 84_000.0, "ETHUSDT": 2_600.0}
# Same fd shape the live CLOB returns for crypto price markets (2026-09-24).
MARKET_INFO = {"t": [{"t": "yes-1"}], "mts": 0.01, "fd": {"r": 0.07, "e": 1, "to": True}}


class FakeScanner:
    def __init__(self, markets=(), resolution=None, quote=(0.0, 0.0)):
        self.markets = list(markets)
        self.resolution = resolution
        self.quote = quote
        self.markets_fetched_at = datetime.now(UTC)
        self.last_scan_counts = {"fetched": len(self.markets)}

    def get_active_markets(self):
        self.markets_fetched_at = datetime.now(UTC)
        return list(self.markets)

    def fetch_market_quote(self, _market_id):
        return self.quote

    def fetch_resolution(self, _market_id, _token_id="", outcome="YES"):
        if self.resolution is None:
            return None
        return 1.0 - self.resolution if outcome == "NO" else self.resolution  # resolution = YES payout


class FakeVol:
    def __init__(self, smile=None, source="deribit"):
        self._smile = smile  # (sigma, dsigma_dk, forward) or None
        self.last_source = source

    def get_iv(self, _spot, _strike, _expiry):
        return 0.60

    def smile(self, _spot, _strike, _expiry):
        return self._smile


class FakeClob:
    """YES book as given; the NO token ("no-1") mirrors it, as on the live CLOB."""

    def __init__(self, bids=((0.39, 500),), asks=((0.41, 500),)):
        self.bids, self.asks = bids, asks
        self.book_calls = 0
        self.tokens_booked: list[str] = []

    def get_order_book(self, token_id):
        self.book_calls += 1
        self.tokens_booked.append(token_id)
        bids, asks = self.bids, self.asks
        if token_id == "no-1":
            bids = tuple((round(1 - p, 6), s) for p, s in self.asks)
            asks = tuple((round(1 - p, 6), s) for p, s in self.bids)
        return {
            "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
            "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        }

    def get_clob_market_info(self, _condition_id):
        return MARKET_INFO


def market(bid=0.39, ask=0.41, market_id="m-1", days=90):
    return ActiveMarket(
        market_id=market_id,
        condition_id="cond-1",
        question="Will the price of Bitcoin be above $80,000 on December 31?",
        yes_token_id="yes-1",
        no_token_id="no-1",
        yes_price=(bid + ask) / 2,
        bid=bid,
        ask=ask,
        spread=ask - bid,
        liquidity_usd=50_000.0,
        resolution_time=datetime.now(UTC) + timedelta(days=days),
    )


def make_trader(
    tmp_path: Path, *, mode="shadow", scanner=None, clob=None, dry_run=False, intents=None, executor=None,
    vol=None, **config,
):
    persist = not dry_run
    positions = PositionManager(starting_nav=1000.0, positions_file=tmp_path / "positions.json", persist=persist)
    return Trader(
        TraderConfig(execution_mode=mode, assets=("btc", "eth"), dry_run=dry_run, **config),
        state_dir=tmp_path,
        scanner=scanner or FakeScanner([market()]),
        vol_surfaces={"btc": vol or FakeVol(), "eth": vol or FakeVol()},
        positions=positions,
        risk=RiskControls.load(tmp_path, positions.current_nav, persist=persist),
        executor=executor or ExecutionClient(mode=mode, clob_client=clob or FakeClob()),
        intents=intents,
        spot_source=lambda symbols: {s: SPOTS[s] for s in symbols},
    )


def events(tmp_path: Path, name=HISTORY_JSONL) -> list[dict]:
    path = tmp_path / name
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def kinds(tmp_path: Path, name=HISTORY_JSONL) -> list[str]:
    return [e["event"] for e in events(tmp_path, name)]


def hold(trader: Trader, *, entry_price=0.40, token_size=100.0, days=90, model_prob=0.52, strike=80_000.0):
    pos = make_position(
        market_id="m-1", question="Will the price of Bitcoin be above $80,000 on December 31?", asset="btc",
        strike=strike, expiry=datetime.now(UTC) + timedelta(days=days), option_type="european",
        yes_token_id="yes-1", yes_price=entry_price, size_usd=entry_price * token_size, model_prob=model_prob,
        token_size=token_size, condition_id="cond-1",
    )
    trader.positions.open_position(pos)
    trader.positions.confirm_fill("m-1", entry_price, fee_usd=0.0)
    return pos


def test_scan_enters_on_executable_edge_and_journals_it(tmp_path):
    trader = make_trader(tmp_path)

    stats = trader.scan()

    pos = trader.positions.get_position("m-1")
    assert pos is not None and pos.fill_confirmed
    assert pos.entry_price == pytest.approx(0.41)  # filled at the ask, not the mid
    assert pos.entry_fee_usd == pytest.approx(pos.token_size * 0.07 * 0.41 * 0.59)
    assert kinds(tmp_path) == ["order", "open"]
    assert kinds(tmp_path, DIAGNOSTICS_JSONL) == ["signal_evaluation", "shadow_quote", "scan_summary"]
    assert stats["executable_edge_candidates"] == 1 and stats["market_errors"] == 0


def test_paper_mode_skips_shadow_quote_diagnostics(tmp_path):
    trader = make_trader(tmp_path, mode="paper")

    trader.scan()

    assert trader.positions.has_position("m-1")
    assert "shadow_quote" not in kinds(tmp_path, DIAGNOSTICS_JSONL)


def test_ask_that_erases_the_edge_blocks_entry(tmp_path):
    # Mid 0.45 leaves a mid edge, but the 0.49 ask plus fee does not clear 5%.
    trader = make_trader(tmp_path, scanner=FakeScanner([market(bid=0.41, ask=0.49)]), clob=FakeClob(
        bids=((0.41, 500),), asks=((0.49, 500),)
    ))

    stats = trader.scan()

    assert not trader.positions.has_position("m-1")
    assert stats["ask_erased_edge"] == 1
    assert "open" not in kinds(tmp_path)


def test_reprice_exits_when_edge_reverses_and_starts_cooldown(tmp_path):
    trader = make_trader(tmp_path, clob=FakeClob(bids=((0.70, 500),), asks=((0.72, 500),)))
    hold(trader)

    trader.reprice_positions()  # model ~0.52 < 0.70 bid: edge reversed

    assert not trader.positions.has_position("m-1")
    close = [e for e in events(tmp_path) if e["event"] == "close"][0]
    assert close["reason"] == "edge_reversed" and close["yes_price"] == 0.70
    assert close["pnl"] == pytest.approx((0.70 - 0.40) * 100 - 100 * 0.07 * 0.70 * 0.30)
    assert trader.positions.closed_within("m-1", 3600)

    trader.scanner.markets = [market(bid=0.30, ask=0.31)]  # big edge again, but cooling down
    trader.scan()
    assert not trader.positions.has_position("m-1")


def test_exit_with_no_bids_holds_without_tripping_the_breaker(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    trader = make_trader(tmp_path, clob=FakeClob(bids=(), asks=((0.05, 500),)))
    # Strike far above spot: the model says ~0, so the edge has decayed and
    # the bot wants out, but there is nobody to sell to.
    hold(trader, model_prob=0.9, strike=200_000.0)

    for _ in range(4):
        trader.reprice_positions()

    assert "[EXIT_UNFILLED]" in caplog.text
    assert trader.positions.has_position("m-1")
    assert trader.risk.consecutive_failures == 0
    assert trader.check_entry_gate(datetime.now(UTC))
    assert kinds(tmp_path) == []  # no order / failed_order spam every 30s


def test_resolved_position_settles_at_payout(tmp_path):
    trader = make_trader(tmp_path, scanner=FakeScanner([], resolution=1.0))
    hold(trader, days=-1)

    trader.reprice_positions()

    assert not trader.positions.has_position("m-1")
    close = [e for e in events(tmp_path) if e["event"] == "close"][0]
    assert close["reason"] == "resolved" and close["pnl"] == pytest.approx((1.0 - 0.40) * 100)


def test_unresolved_expired_position_waits(tmp_path):
    trader = make_trader(tmp_path, scanner=FakeScanner([], resolution=None))
    hold(trader, days=-1)

    trader.reprice_positions()

    assert trader.positions.has_position("m-1")
    assert "close" not in kinds(tmp_path)


def test_dry_run_scan_and_exit_write_nothing(tmp_path):
    trader = make_trader(tmp_path, dry_run=True, clob=FakeClob(bids=((0.70, 500),), asks=((0.72, 500),)))
    hold(trader)

    trader.reprice_positions()
    trader.scan()

    assert trader.positions.has_position("m-1")
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_halt_file_blocks_entries_and_logs_the_gate_once(tmp_path):
    trader = make_trader(tmp_path)
    (tmp_path / "HALT").touch()

    trader.scan()
    trader.scan()

    assert not trader.positions.has_position("m-1")
    gate = [e for e in events(tmp_path) if e["event"] == "entry_gate"]
    assert [(e["halted"], e["reason"]) for e in gate] == [(True, "HALT file present")]

    (tmp_path / "HALT").unlink()
    trader.scan()
    assert trader.positions.has_position("m-1")
    assert [e["halted"] for e in events(tmp_path) if e["event"] == "entry_gate"] == [True, False]


def test_one_bad_market_is_a_data_error_not_a_broker_failure(tmp_path):
    bad = market(market_id="bad")
    bad.resolution_time = "not-a-datetime"  # makes this market throw during processing
    trader = make_trader(tmp_path, scanner=FakeScanner([bad, market()]))

    stats = trader.scan()

    assert stats["market_errors"] == 1
    assert trader.risk.consecutive_failures == 0
    assert trader.positions.has_position("m-1")  # the good market still traded


def test_plan_entry_prices_the_fee_into_the_edge(tmp_path):
    from turtlequant.clob_execution import DEFAULT_CRYPTO_FEE, BookLevel, OrderBook

    book = OrderBook("yes-1", bids=[BookLevel(0.39, 500)], asks=[BookLevel(0.41, 500)])
    positions = PositionManager(starting_nav=1000.0, positions_file=tmp_path / "p.json")

    plan = plan_entry(book, DEFAULT_CRYPTO_FEE, 0.55, 0.15, positions)

    assert plan.executable_price == pytest.approx(0.41 + 0.07 * 0.41 * 0.59)
    assert plan.edge == pytest.approx(0.55 - plan.executable_price)
    assert 1.0 <= plan.size_usd <= 100.0  # per-market cap: 10% of NAV
    assert plan.fill.complete


class AmbiguousLiveClob(FakeClob):
    """Live broker whose order POST times out; the order later shows as matched."""

    def __init__(self):
        super().__init__()
        self.orders_posted = 0

    def create_and_post_market_order(self, **_kwargs):
        self.orders_posted += 1
        raise TimeoutError("read timed out")


def test_live_ambiguous_buy_halts_entries_until_reconciled(tmp_path):
    ledger = OrderIntentLedger(tmp_path / "intents.sqlite3")
    clob = AmbiguousLiveClob()
    trader = make_trader(
        tmp_path, mode="live", intents=ledger, clob=clob,
        executor=ExecutionClient(mode="live", allow_live=True, clob_client=clob),
    )

    trader.scan()
    trader.scan()

    assert clob.orders_posted == 1  # the second scan must not buy again
    (intent,) = ledger.outstanding()
    assert intent.status == "submitted" and intent.order_id == ""
    assert trader.risk.entry_halt == "1 unreconciled order intent(s)"
    assert trader.risk.consecutive_failures == 1

    ledger.resolve(intent.id, "failed", "operator: no order at broker")
    trader.scan()
    assert clob.orders_posted == 2


def test_live_pre_send_rejection_closes_its_intent(tmp_path):
    ledger = OrderIntentLedger(tmp_path / "intents.sqlite3")
    clob = FakeClob(bids=((0.70, 500),), asks=((0.72, 500),))
    trader = make_trader(
        tmp_path, mode="live", intents=ledger, clob=clob,
        executor=ExecutionClient(mode="live", allow_live=False, clob_client=clob),  # refuses before sending
    )
    hold(trader)

    trader.reprice_positions()

    assert ledger.outstanding() == []
    assert trader.positions.has_position("m-1")
    assert trader.risk.consecutive_failures == 0


# A strong put skew around the 80k strike: the smile model prices "BTC above
# 80k" well above the legacy N(d2), so the edge flips relative to the mid.
SKEWED = (0.60, -0.00002, 84_500.0)


def test_legacy_pricing_is_the_default_and_logs_both_models(tmp_path):
    trader = make_trader(tmp_path, vol=FakeVol(smile=SKEWED))

    trader.scan()

    evaluation = [e for e in events(tmp_path, DIAGNOSTICS_JSONL) if e["event"] == "signal_evaluation"][0]
    assert evaluation["pricing_model"] == "legacy"
    assert evaluation["model_prob"] == evaluation["model_prob_legacy"]
    assert evaluation["model_prob_smile"] > evaluation["model_prob_legacy"]
    assert evaluation["forward"] == 84_500.0


def test_smile_pricing_changes_the_decision(tmp_path):
    # Mid 0.70: legacy (~0.52) sees no YES edge; the skewed smile does.
    quiet = make_trader(tmp_path / "legacy", vol=FakeVol(smile=SKEWED),
                        scanner=FakeScanner([market(bid=0.69, ask=0.71)]),
                        clob=FakeClob(bids=((0.69, 500),), asks=((0.71, 500),)))
    smile = make_trader(tmp_path / "smile", vol=FakeVol(smile=SKEWED), pricing_model="smile",
                        scanner=FakeScanner([market(bid=0.69, ask=0.71)]),
                        clob=FakeClob(bids=((0.69, 500),), asks=((0.71, 500),)))

    quiet.scan()
    smile.scan()

    assert not quiet.positions.has_position("m-1")
    assert smile.positions.has_position("m-1")
    assert smile.positions.get_position("m-1").model_prob_at_entry > 0.76


def test_smile_mode_without_a_smile_does_not_enter(tmp_path):
    trader = make_trader(tmp_path, vol=FakeVol(smile=None), pricing_model="smile")

    stats = trader.scan()

    assert not trader.positions.has_position("m-1")
    assert stats["vol_blocked"] == 1


def test_stale_iv_blocks_entries_but_not_exits(tmp_path):
    trader = make_trader(tmp_path, vol=FakeVol(source="stale"),
                         clob=FakeClob(bids=((0.70, 500),), asks=((0.72, 500),)))
    stats = trader.scan()
    assert not trader.positions.has_position("m-1") and stats["vol_blocked"] == 1

    hold(trader)
    trader.reprice_positions()  # model ~0.52 < 0.70 bid: exits still run
    assert not trader.positions.has_position("m-1")


def test_delta_headroom_limits_adding_and_allows_reducing():
    from turtlequant.trader import delta_headroom_usd

    assert delta_headroom_usd(0.0, 2.0, 0.5, 100.0) == pytest.approx(25.0)  # 50 shares * 2 = 100
    assert delta_headroom_usd(120.0, 2.0, 0.5, 100.0) == 0.0  # already past the cap
    assert delta_headroom_usd(120.0, -2.0, 0.5, 100.0) == pytest.approx(55.0)  # down to -100
    assert delta_headroom_usd(0.0, 2.0, 0.5, 0.0) == float("inf")  # off


def test_dollar_delta_sign_follows_the_bet(tmp_path):
    from turtlequant.market_parser import MarketParams, OptionType

    trader = make_trader(tmp_path)
    expiry = datetime.now(UTC) + timedelta(days=30)
    above = MarketParams("btc", 85_000.0, expiry, OptionType.EUROPEAN)
    dip = MarketParams("btc", 75_000.0, expiry, OptionType.BARRIER_DOWN)

    assert trader.dollar_delta_per_share(above, 84_000.0, trader.price(above, 84_000.0)) > 0
    assert trader.dollar_delta_per_share(dip, 84_000.0, trader.price(dip, 84_000.0)) < 0


def test_asset_caps_are_off_by_default_and_shrink_when_set(tmp_path):
    uncapped = make_trader(tmp_path / "a")
    uncapped.scan()
    full = uncapped.positions.get_position("m-1").size_usd
    assert full > 20

    capped = make_trader(tmp_path / "b", max_asset_exposure_pct=0.02)  # $20 of $1000 NAV
    capped.scan()
    assert capped.positions.get_position("m-1").size_usd == pytest.approx(20.0, rel=1e-6)
    assert capped.asset_risk["btc"]["gross_usd"] == pytest.approx(20.0, rel=1e-6)


def test_delta_cap_blocks_adding_to_a_full_book(tmp_path):
    trader = make_trader(tmp_path, max_asset_delta_pct=0.05)  # +-$50 of dollar delta
    trader.positions.open_position(make_position(
        market_id="held", question="q", asset="btc", strike=84_000.0,
        expiry=datetime.now(UTC) + timedelta(days=5), option_type="european", yes_token_id="t",
        yes_price=0.5, size_usd=100.0, model_prob=0.6, token_size=200.0,
    ))

    stats = trader.scan()

    assert trader.asset_risk["btc"]["delta_usd"] > 50  # the held ATM digital alone exceeds the cap
    assert not trader.positions.has_position("m-1")  # same-direction entry refused
    assert stats["asset_capped"] == 1
    saved = RiskControls.load(tmp_path, 1000.0).asset_risk
    assert saved["btc"]["delta_usd"] == pytest.approx(trader.asset_risk["btc"]["delta_usd"])


def test_kelly_shrink_sizes_toward_the_market(tmp_path):
    raw = make_trader(tmp_path / "raw")
    shrunk = make_trader(tmp_path / "half", kelly_shrink=0.5)
    at_market = make_trader(tmp_path / "zero", kelly_shrink=0.0)

    for trader in (raw, shrunk, at_market):
        trader.scan()

    assert shrunk.positions.get_position("m-1").size_usd < raw.positions.get_position("m-1").size_usd
    assert not at_market.positions.has_position("m-1")  # the mid has no edge over the ask


# Market at 0.70/0.72 while the model (~0.52) says YES is overpriced: a NO edge.
def _rich_yes():
    return FakeScanner([market(bid=0.70, ask=0.72)]), FakeClob(bids=((0.70, 500),), asks=((0.72, 500),))


def test_no_side_is_off_by_default(tmp_path):
    scanner, clob = _rich_yes()
    trader = make_trader(tmp_path, scanner=scanner, clob=clob)

    trader.scan()

    assert not trader.positions.has_position("m-1")


def test_no_side_buys_the_no_token_when_enabled(tmp_path):
    scanner, clob = _rich_yes()
    trader = make_trader(tmp_path, scanner=scanner, clob=clob, sides=("YES", "NO"))

    stats = trader.scan()

    pos = trader.positions.get_position("m-1")
    assert pos is not None and pos.outcome == "NO" and pos.token_id == "no-1"
    assert pos.yes_token_id == "yes-1"
    assert pos.entry_price == pytest.approx(0.30)  # NO ask = 1 - YES bid
    assert pos.model_prob_at_entry == pytest.approx(1 - 0.52, abs=0.02)
    assert "no-1" in clob.tokens_booked
    assert stats["no_side_candidates"] == 1
    opened = [e for e in events(tmp_path) if e["event"] == "open"][0]
    assert opened["outcome"] == "NO" and opened["token_id"] == "no-1"
    assert trader.asset_risk["btc"]["delta_usd"] < 0  # NO on "above 80k" is short BTC


def test_no_position_exits_on_the_no_book_and_settles_on_the_no_payout(tmp_path):
    scanner, clob = _rich_yes()
    trader = make_trader(tmp_path, scanner=scanner, clob=clob, sides=("YES", "NO"))
    trader.scan()
    assert trader.positions.get_position("m-1").outcome == "NO"

    # YES collapses to 0.20/0.22: the NO bid (0.78) is now far above P(NO) ~0.48.
    trader.executor._client.bids, trader.executor._client.asks = ((0.20, 500),), ((0.22, 500),)
    trader.reprice_positions()
    close = [e for e in events(tmp_path) if e["event"] == "close"][0]
    assert close["outcome"] == "NO" and close["yes_price"] == pytest.approx(0.78)
    assert close["pnl"] > 0

    settled = make_trader(tmp_path / "s", scanner=FakeScanner([], resolution=1.0), sides=("YES", "NO"))
    settled.positions.open_position(make_position(
        market_id="m-2", question="q", asset="btc", strike=80_000.0,
        expiry=datetime.now(UTC) - timedelta(hours=1), option_type="european", yes_token_id="yes-1",
        yes_price=0.30, size_usd=30.0, model_prob=0.5, token_size=100.0, outcome="NO", no_token_id="no-1",
    ))
    settled.reprice_positions()
    resolved = [e for e in events(tmp_path / "s") if e["event"] == "close"][0]
    assert resolved["resolution_price"] == 0.0  # YES won, so the NO token pays 0
    assert resolved["pnl"] == pytest.approx(-30.0 - 100 * 0.07 * 0.30 * 0.70)  # stake plus modelled entry fee


def test_ev_exit_rule_holds_decayed_edges_and_sells_rich_bids(tmp_path):
    decayed = FakeClob(bids=((0.45, 500),), asks=((0.47, 500),))  # model ~0.52: edge 0.07 of entry 0.5
    legacy = make_trader(tmp_path / "legacy", clob=decayed)
    ev = make_trader(tmp_path / "ev", clob=decayed, exit_rule="ev")
    for trader in (legacy, ev):
        hold(trader, model_prob=0.9)
        trader.reprice_positions()

    assert not legacy.positions.has_position("m-1")  # edge_decayed sold at 0.45 < model
    assert ev.positions.has_position("m-1")

    rich = make_trader(tmp_path / "rich", clob=FakeClob(bids=((0.70, 500),), asks=((0.72, 500),)), exit_rule="ev")
    hold(rich)
    rich.reprice_positions()
    close = [e for e in events(tmp_path / "rich") if e["event"] == "close"][0]
    assert close["reason"] == "ev_exit" and close["yes_price"] == 0.70


def test_ev_exit_rule_holds_when_the_vol_source_is_degraded(tmp_path):
    trader = make_trader(
        tmp_path, vol=FakeVol(source="realized"), clob=FakeClob(bids=((0.70, 500),), asks=((0.72, 500),)),
        exit_rule="ev",
    )
    hold(trader)

    trader.reprice_positions()

    assert trader.positions.has_position("m-1")
