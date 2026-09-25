"""Trading loop units: reprice/settle open positions, scan for entries.

``Trader`` owns one reprice pass and one scan pass; ``scripts/turtlequant_bot.py``
only parses arguments, wires the components and calls them on a timer. Every
external dependency (scanner, vol surfaces, executor, spot source, notifier)
is injected so the loop can be tested against fakes.

Decisions stay pure where possible: exits via ``PositionManager.exit_decision``
and entry sizing via ``plan_entry``. ``Trader`` turns them into orders, state
changes and history events.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from turtlequant.clob_execution import (
    DEFAULT_CRYPTO_FEE,
    ExecutionClient,
    ExecutionResult,
    FeeSchedule,
    FillEstimate,
    OrderBook,
    estimate_buy_fill,
)
from turtlequant.data.binance import ASSET_TO_SYMBOL, fetch_latest_closes
from turtlequant.history import append_history
from turtlequant.market_parser import MarketParams, OptionType, parse_market, strike_is_plausible
from turtlequant.market_scanner import ActiveMarket, MarketScanner
from turtlequant.order_intents import OrderIntentLedger
from turtlequant.order_reconciliation import ReconciliationError, reconcile_intent
from turtlequant.position_manager import (
    ExitDecision,
    Position,
    PositionManager,
    StatePersistenceError,
    make_position,
)
from turtlequant.probability_engine import compute_probability
from turtlequant.risk_controls import RiskControls
from turtlequant.vol_surface import VolSurface

logger = logging.getLogger("turtlequant_bot")

SpotSource = Callable[[Iterable[str]], dict[str, float]]


@dataclass(frozen=True)
class TraderConfig:
    execution_mode: str  # "paper" | "shadow" | "live"
    assets: tuple[str, ...]
    entry_threshold: float = 0.05
    calibration_rmse: float = 0.05
    max_daily_loss: float = 50.0
    max_market_data_age_secs: float = 90.0
    dry_run: bool = False
    min_entry_price: float = 0.02  # skip near-certain markets on either side
    max_entry_price: float = 0.98
    reentry_cooldown_secs: float = 2 * 3600

    @property
    def persist(self) -> bool:
        return not self.dry_run


@dataclass(frozen=True)
class EntryPlan:
    """Sized entry against the executable book, including the taker fee."""

    size_usd: float
    executable_price: float  # average fill price plus fee per share
    edge: float  # model_prob - executable_price
    fill: FillEstimate
    estimated_fee: float


def plan_entry(
    book: OrderBook, fee: FeeSchedule, model_prob: float, mid_edge: float, positions: PositionManager
) -> EntryPlan:
    """Size a YES entry: Kelly on the mid edge to find the fill, then on the fee-inclusive edge."""
    preliminary = estimate_buy_fill(book, positions.kelly_size(mid_edge, model_prob, book.best_ask))
    estimated_fee = fee.fee(preliminary.filled_shares, preliminary.avg_price)
    executable_price = (
        (preliminary.filled_usd + estimated_fee) / preliminary.filled_shares
        if preliminary.filled_shares > 0
        else 0.0
    )
    edge = model_prob - executable_price
    size_usd = positions.kelly_size(edge, model_prob, executable_price)
    return EntryPlan(size_usd, executable_price, edge, estimate_buy_fill(book, size_usd), estimated_fee)


class Trader:
    def __init__(
        self,
        config: TraderConfig,
        *,
        state_dir: Path,
        scanner: MarketScanner,
        vol_surfaces: dict[str, VolSurface],
        positions: PositionManager,
        risk: RiskControls,
        executor: ExecutionClient,
        intents: OrderIntentLedger | None = None,
        spot_source: SpotSource = fetch_latest_closes,
        notify_entry: Callable[..., Any] | None = None,
        notify_exit: Callable[..., Any] | None = None,
        running: Callable[[], bool] = lambda: True,
    ) -> None:
        self.config = config
        self.state_dir = state_dir
        self.scanner = scanner
        self.vol_surfaces = vol_surfaces
        self.positions = positions
        self.risk = risk
        self.executor = executor
        self.intents = intents
        self.spot_source = spot_source
        self.notify_entry = notify_entry or (lambda *_a, **_k: None)
        self.notify_exit = notify_exit or (lambda *_a, **_k: None)
        self.running = running
        self.reprice_errors = 0  # data-plane errors since the last scan summary
        self._reconcile_warned_at: dict[int, float] = {}

    @property
    def live(self) -> bool:
        return self.config.execution_mode == "live"

    # ------------------------------------------------------------------
    # Events, gate, live journal
    # ------------------------------------------------------------------

    def record(self, entry: dict[str, object]) -> None:
        if not self.config.persist:
            return
        try:
            append_history(self.state_dir, entry)
        except OSError as exc:
            raise StatePersistenceError(f"history was not persisted: {exc}") from exc

    def check_entry_gate(self, market_data_at: datetime | None) -> bool:
        """Evaluate the entry gate; log and record only when its state changes."""
        allowed, reason = self.risk.entries_allowed(
            self.positions.marked_equity(),
            max_daily_loss=self.config.max_daily_loss,
            market_data_at=market_data_at,
            max_market_data_age_secs=self.config.max_market_data_age_secs,
            unreconciled_orders=len(self.intents.outstanding()) if self.intents is not None else 0,
        )
        if self.risk.record_entry_gate(reason):
            if reason:
                logger.warning("[ENTRY_HALTED] %s", reason)
            else:
                logger.info("[ENTRY_RESUMED] entry gate open")
            self.record({"event": "entry_gate", "halted": bool(reason), "reason": reason, "ts": _now_iso()})
        return allowed

    def journal_result(self, intent_id: int | None, result: ExecutionResult) -> None:
        """Record the broker's answer on its intent; an unsent order is terminal."""
        if intent_id is None or self.intents is None:
            return
        if not result.sent:
            self.intents.fail(intent_id, result.error or result.status)
            return
        self.intents.submitted(intent_id, result.order_id, result.raw)
        if result.broker_failure:
            logger.warning(
                "[ORDER_AMBIGUOUS] intent %d %s: entries halt until it is reconciled",
                intent_id,
                result.error or result.status,
            )

    def market_has_open_intent(self, market_id: str) -> bool:
        return self.intents is not None and bool(self.intents.outstanding(market_id))

    def reconcile_in_process(self) -> None:
        """Retry ambiguous broker actions; a confirmed fill updates positions."""
        if self.intents is None:
            return
        for intent in self.intents.outstanding():
            if not intent.order_id:
                continue  # no broker order id: needs an operator (scripts/order_intents.py)
            try:
                reconcile_intent(intent, self.executor, self.positions)
            except ReconciliationError as exc:
                if time.time() - self._reconcile_warned_at.get(intent.id, 0.0) >= 300:
                    logger.warning("[UNRECONCILED] %s", exc)
                    self._reconcile_warned_at[intent.id] = time.time()
                continue
            self.intents.reconcile(intent.id)
            self.record(
                {
                    "event": "intent_reconciled",
                    "intent_id": intent.id,
                    "market_id": intent.market_id,
                    "side": intent.side,
                    "order_id": intent.order_id,
                    "ts": _now_iso(),
                }
            )

    # ------------------------------------------------------------------
    # Reprice pass
    # ------------------------------------------------------------------

    def reprice_positions(self) -> None:
        """Settle resolved positions and evaluate exits for the rest."""
        self.reconcile_in_process()
        open_positions = self.positions.all_positions()
        if not open_positions:
            return
        latest = self.spot_source(ASSET_TO_SYMBOL[p.asset] for p in open_positions)
        spots = {asset: latest.get(symbol) for asset, symbol in ASSET_TO_SYMBOL.items()}
        logger.info("Repricing %d open position(s)", len(open_positions))
        for pos in open_positions:
            try:
                logger.info("[REPRICE] %s K=%.0f exp=%s", pos.asset.upper(), pos.strike, pos.expiry_iso[:10])
                if pos.status == "pending_redemption" or datetime.now(UTC) >= pos.expiry:
                    self.settle(pos)
                else:
                    self._reprice(pos, spots.get(pos.asset))
            except StatePersistenceError:
                raise
            except Exception as exc:
                # Data-plane error: logged and counted, never a broker failure.
                logger.warning("Reprice failed for %s: %s", pos.market_id[:16], exc)
                self.reprice_errors += 1

    def _reprice(self, pos: Position, spot: float | None) -> None:
        vs = self.vol_surfaces.get(pos.asset)
        if spot is None or vs is None:
            return
        params = MarketParams(
            asset=pos.asset, strike=pos.strike, expiry=pos.expiry, option_type=OptionType(pos.option_type)
        )
        model_prob = compute_probability(params, spot, vs.get_iv(spot, pos.strike, pos.expiry))
        # Fall back only to Gamma's live bid/ask. A stale last trade or a
        # persisted mark is never an executable exit price: with neither source
        # there is no bid, and the position holds.
        gamma_bid, gamma_ask = self.scanner.fetch_market_quote(pos.market_id) or (0.0, 0.0)
        book = self.executor.get_order_book(pos.yes_token_id, fallback_bid=gamma_bid, fallback_ask=gamma_ask)
        if book.best_bid > 0 or book.best_ask > 0:
            self.positions.record_market_data(
                pos.market_id, yes_price=book.mid, bid=book.best_bid, ask=book.best_ask, observed_at=datetime.now(UTC)
            )
        self.evaluate_exit(pos, book=book, model_prob=model_prob, log_hold=True)

    def settle(self, pos: Position) -> None:
        """Realise an expired position once Gamma confirms its resolution."""
        resolved_price = (
            pos.resolution_price
            if pos.status == "pending_redemption"
            else self.scanner.fetch_resolution(pos.market_id, pos.yes_token_id)
        )
        if resolved_price is None:
            overdue_hours = (datetime.now(UTC) - pos.expiry).total_seconds() / 3600
            logger.log(
                logging.WARNING if overdue_hours > 24 else logging.INFO,
                "[EXPIRED_PENDING] Awaiting confirmed resolution for %s (%.1fh past expiry)",
                pos.market_id[:16],
                overdue_hours,
            )
            return
        if self.live:
            # Live payouts only exist once the CTF redemption lands; until that
            # is automated, hold the claim as pending.
            if pos.status != "pending_redemption":
                logger.info(
                    "[PENDING_REDEMPTION] %s K=%.0f exp=%s resolved=%.4f",
                    pos.asset.upper(), pos.strike, pos.expiry_iso[:10], resolved_price,
                )
                self.positions.mark_pending_redemption(pos.market_id, resolved_price)
                self.record(
                    {
                        "event": "pending_redemption",
                        "market_id": pos.market_id,
                        "asset": pos.asset,
                        "strike": pos.strike,
                        "resolution_price": resolved_price,
                        "ts": _now_iso(),
                    }
                )
            return
        if self.config.dry_run:
            logger.info("[RESOLVED] %s would settle at %.4f (dry-run)", pos.market_id[:16], resolved_price)
            return
        shares = pos.token_size
        closed, pnl = self.positions.settle_position(pos.market_id, resolved_price)
        self.risk.record_realized_pnl(pnl)
        self.record(
            {
                "event": "close",
                "market_id": pos.market_id,
                "asset": pos.asset,
                "strike": pos.strike,
                "reason": "resolved",
                "yes_price": resolved_price,
                "resolution_price": resolved_price,
                "filled_shares": shares,
                "remaining_shares": 0.0,
                "complete": True,
                "pnl": pnl,
                "ts": _now_iso(),
            }
        )
        if closed and closed.fill_confirmed:
            self.notify_exit(closed, resolved_price, pnl, "resolved")

    # ------------------------------------------------------------------
    # Exits (shared by the reprice and scan passes)
    # ------------------------------------------------------------------

    def evaluate_exit(
        self, pos: Position, *, book: OrderBook, model_prob: float, log_hold: bool = False
    ) -> ExitDecision:
        """Decide on the book's bid and, if the decision says so, sell."""
        decision = self.positions.exit_decision(pos.market_id, model_prob, book.best_bid, now=datetime.now(UTC))
        if decision.should_exit:
            self.exit_position(pos, book=book, model_prob=model_prob, decision=decision)
        elif log_hold:
            logger.info(
                "[HOLD] %s K=%.0f exp=%s model_p=%.4f mkt_p=%.4f edge=%.4f entry_edge=%.4f ttl=%.1fh",
                pos.asset.upper(), pos.strike, pos.expiry_iso[:10], model_prob, book.best_bid,
                decision.current_edge, decision.entry_edge, decision.hours_to_expiry or 0.0,
            )
        return decision

    def exit_position(
        self, pos: Position, *, book: OrderBook, model_prob: float, decision: ExitDecision
    ) -> ExecutionResult | None:
        """Sell the position into ``book`` and account for the fill."""
        reason = decision.reason or "edge_reversed"
        if self.config.dry_run:
            logger.info(
                "[DRY_RUN] Would exit %s reason=%s model_p=%.4f bid=%.4f",
                pos.market_id[:16], reason, model_prob, book.best_bid,
            )
            return None
        if self.market_has_open_intent(pos.market_id):
            logger.info("[EXIT_BLOCKED] %s has an unreconciled order intent", pos.market_id[:16])
            return None
        shares = pos.token_size if pos.token_size > 0 else pos.size_usd / pos.entry_price
        intent_id = (
            self.intents.pending(pos.market_id, pos.yes_token_id, "SELL", shares)
            if self.intents is not None and self.live
            else None
        )
        fee = self.executor.get_market_fee(pos.condition_id, pos.yes_token_id)
        result = self.executor.sell_yes(
            pos.yes_token_id, shares, book, fee=fee if fee is not None else DEFAULT_CRYPTO_FEE
        )
        self.journal_result(intent_id, result)
        if result.sent or result.success:
            self.record(
                {
                    "event": "order",
                    "market_id": pos.market_id,
                    "asset": pos.asset,
                    "reason": reason,
                    **result.to_history(),
                    "ts": _now_iso(),
                }
            )
        if not result.success:
            if not result.broker_failure:
                # No bids to sell into: a liquidity outcome, not a broker fault.
                # Keep holding and retry on the next reprice.
                logger.info(
                    "[EXIT_UNFILLED] %s %s: %s", pos.market_id[:16], reason, result.error or "no executable depth"
                )
                return result
            self.risk.record_failure(result.error or result.status)
            self.record(
                {
                    "event": "failed_order",
                    "market_id": pos.market_id,
                    "asset": pos.asset,
                    "side": "SELL",
                    "reason": reason,
                    "remaining_shares": shares,
                    "remaining_size_usd": pos.size_usd,
                    "unhedged_exposure": True,
                    "error": result.error or result.status,
                    "ts": _now_iso(),
                }
            )
            return result
        filled_price = result.avg_price if result.avg_price > 0 else book.best_bid
        closed, pnl = self.positions.close_position(
            pos.market_id,
            exit_price=filled_price,
            reason=reason,
            filled_shares=result.filled_shares,
            exit_fee_usd=result.fee_usd,
        )
        if intent_id is not None:
            self.intents.reconcile(intent_id)
        self.risk.record_realized_pnl(pnl)
        self.risk.record_success(self.positions.marked_equity())
        self.record(
            {
                "event": "close" if result.complete else "partial_close",
                "market_id": pos.market_id,
                "asset": pos.asset,
                "strike": pos.strike,
                "reason": reason,
                "model_prob": model_prob,
                "yes_price": filled_price,
                "bid": book.best_bid,
                "ask": book.best_ask,
                "current_edge": decision.current_edge,
                "entry_edge": decision.entry_edge,
                "hours_to_expiry": decision.hours_to_expiry,
                "filled_shares": result.filled_shares,
                "remaining_shares": max(0.0, shares - result.filled_shares),
                "complete": result.complete,
                "pnl": pnl,
                "ts": _now_iso(),
            }
        )
        if closed and closed.fill_confirmed and result.complete:
            self.notify_exit(closed, filled_price, pnl, reason)
        return result

    # ------------------------------------------------------------------
    # Scan pass
    # ------------------------------------------------------------------

    def scan(self) -> dict[str, object] | None:
        """Fetch markets, update held positions, look for entries; returns the scan summary."""
        try:
            markets = self.scanner.get_active_markets()
            logger.info("Scan: %d markets found", len(markets))
        except Exception as exc:
            # No markets means no entries; the breaker is for broker faults.
            logger.warning("Market scan failed: %s", exc)
            return None

        stats: dict[str, object] = {
            "event": "scan_summary",
            "markets_passed_filters": len(markets),
            "scanner_funnel": dict(self.scanner.last_scan_counts),
            "parse_attempted": 0,
            "parsed_markets": 0,
            "unclassified_markets": 0,
            "asset_skipped": 0,
            "spot_missing": 0,
            "implausible_strike": 0,
            "market_errors": 0,
            "vol_sources": {},
            "mid_edge_candidates": 0,
            "executable_edge_candidates": 0,
            "ask_erased_edge": 0,
            "book_sources": {},
            "ts": _now_iso(),
        }
        latest = self.spot_source(ASSET_TO_SYMBOL[a] for a in self.config.assets)
        spots = {asset: latest.get(ASSET_TO_SYMBOL[asset]) for asset in self.config.assets}
        # Staleness is measured from when Gamma actually served the list; a
        # cached list during an outage keeps its old timestamp.
        market_data_at = self.scanner.markets_fetched_at
        self.check_entry_gate(market_data_at)

        for market in markets:
            if not self.running():
                break
            try:
                self.process_market(market, spots, market_data_at, stats)
            except StatePersistenceError:
                raise
            except Exception as exc:
                logger.warning("Market processing error (%s): %s", market.market_id[:16], exc)
                _inc(stats, "market_errors")

        stats["reprice_errors"] = self.reprice_errors
        self.reprice_errors = 0
        self.risk.record_scan(errors=int(stats["market_errors"]), attempted=int(stats["parse_attempted"]))
        self.record(stats)
        return stats

    def process_market(
        self,
        market: ActiveMarket,
        spots: dict[str, float | None],
        market_data_at: datetime | None,
        stats: dict[str, object],
    ) -> None:
        _inc(stats, "parse_attempted")
        params = parse_market(market.question, market.resolution_time)
        if params is None:
            _inc(stats, "unclassified_markets")
            return
        _inc(stats, "parsed_markets")
        if params.asset not in self.config.assets:
            _inc(stats, "asset_skipped")
            return
        spot = spots.get(params.asset)
        if spot is None or spot <= 0:
            _inc(stats, "spot_missing")
            return
        if not strike_is_plausible(params.strike, spot):
            # A strike far from spot means the question was misparsed (wrong
            # units or not a price market) — never a real edge.
            _inc(stats, "implausible_strike")
            logger.info("[PARSE_REJECTED] K=%g vs spot %.2f: %s", params.strike, spot, market.question[:100])
            return

        vs = self.vol_surfaces[params.asset]
        sigma = vs.get_iv(spot, params.strike, params.expiry)
        vol_source = vs.last_source
        _inc(stats, "vol_sources", vol_source)
        model_prob = compute_probability(params, spot, sigma)

        pos = self.positions.get_position(market.market_id)
        if pos is not None:
            self.positions.record_market_data(
                market.market_id,
                yes_token_id=market.yes_token_id,
                condition_id=market.condition_id,
                yes_price=market.yes_price,
                bid=market.bid,
                ask=market.ask,
                observed_at=datetime.now(UTC),
            )
            book = self.executor.get_order_book(
                pos.yes_token_id, fallback_bid=market.bid, fallback_ask=market.ask
            )
            self.evaluate_exit(pos, book=book, model_prob=model_prob)
            return

        self.try_enter(market, params, model_prob, sigma, vol_source, market_data_at, stats)

    def try_enter(
        self,
        market: ActiveMarket,
        params: MarketParams,
        model_prob: float,
        sigma: float,
        vol_source: str,
        market_data_at: datetime | None,
        stats: dict[str, object],
    ) -> ExecutionResult | None:
        cfg = self.config
        if self.positions.closed_within(market.market_id, cfg.reentry_cooldown_secs):
            logger.debug("Cooldown active for %s — skip re-entry", market.market_id[:16])
            return None
        if not self.check_entry_gate(market_data_at):
            return None
        yes_price = market.yes_price
        mid_edge = model_prob - yes_price
        if mid_edge < cfg.entry_threshold:
            return None
        _inc(stats, "mid_edge_candidates")
        if yes_price <= cfg.min_entry_price or yes_price >= cfg.max_entry_price:
            return None

        book = self.executor.get_order_book(market.yes_token_id, fallback_bid=market.bid, fallback_ask=market.ask)
        _inc(stats, "book_sources", book.source)
        fee = self.executor.get_market_fee(market.condition_id, market.yes_token_id)
        if self.live and fee is None:
            logger.warning("[ENTRY_REJECTED] Missing CLOB fee rate for %s", market.market_id[:16])
            return None
        fee = fee if fee is not None else DEFAULT_CRYPTO_FEE
        plan = plan_entry(book, fee, model_prob, mid_edge, self.positions)
        if plan.size_usd < 1.0 or not plan.fill.complete:
            return None
        if not self.positions.has_expiry_headroom(params.expiry, plan.size_usd):
            logger.info("Per-expiry cap reached for %s — skip", params.expiry.strftime("%Y-%m-%d"))
            return None

        erased = plan.edge < cfg.entry_threshold
        self.record(
            {
                "event": "signal_evaluation",
                "parsed": True,
                "market_id": market.market_id,
                "asset": params.asset,
                "strike": params.strike,
                "expiry": params.expiry.isoformat(),
                "option_type": params.option_type.value,
                "model_prob": model_prob,
                # Diagnostics only: a 1.645*RMSE haircut on top of the entry
                # threshold silently raised the bar from 5% to ~13% in July.
                "conservative_prob": max(0.0, model_prob - 1.645 * cfg.calibration_rmse),
                "mid_price": yes_price,
                "executable_price": plan.executable_price,
                "mid_edge": mid_edge,
                "ask_edge": plan.edge,
                "entry_threshold": cfg.entry_threshold,
                "ask_erased_edge": erased,
                "requested_size_usd": plan.size_usd,
                "estimated_fill_ratio": min(1.0, plan.fill.filled_usd / plan.size_usd),
                "estimated_avg_price": plan.fill.avg_price,
                "estimated_slippage": plan.fill.avg_price - yes_price if plan.fill.avg_price > 0 else 0.0,
                "estimated_complete": plan.fill.complete,
                "fee_rate": fee.rate,
                "fee_exponent": fee.exponent,
                "estimated_fee": plan.estimated_fee,
                "vol_source": vol_source,
                "sigma": sigma,
                "book_source": book.source,
                "quote": book.to_dict(),
                "ts": _now_iso(),
            }
        )
        if cfg.execution_mode != "paper":
            # Paper simulates fills only; shadow and live also journal the quote
            # each candidate was judged against (the shadow-soak metrics).
            self.record(
                {
                    "event": "shadow_quote",
                    "market_id": market.market_id,
                    "asset": params.asset,
                    "model_prob": model_prob,
                    "mid_price": yes_price,
                    "bid": book.best_bid,
                    "ask": book.best_ask,
                    "edge": plan.edge,
                    "reason": "ask_erased_edge" if erased else "executable_edge",
                    "book_source": book.source,
                    "vol_source": vol_source,
                    "ts": _now_iso(),
                }
            )
        if erased:
            _inc(stats, "ask_erased_edge")
            return None
        _inc(stats, "executable_edge_candidates")
        if cfg.dry_run:
            logger.info(
                "[DRY_RUN] Would buy %s $%.2f at %.4f (model_p=%.4f edge=%.4f)",
                market.market_id[:16], plan.size_usd, plan.executable_price, model_prob, plan.edge,
            )
            return None
        return self._buy(market, params, model_prob, sigma, vol_source, book, fee, plan)

    def _buy(
        self,
        market: ActiveMarket,
        params: MarketParams,
        model_prob: float,
        sigma: float,
        vol_source: str,
        book: OrderBook,
        fee: FeeSchedule,
        plan: EntryPlan,
    ) -> ExecutionResult:
        intent_id = (
            self.intents.pending(
                market.market_id, market.yes_token_id, "BUY", plan.size_usd,
                {
                    "question": market.question, "asset": params.asset,
                    "strike": params.strike, "expiry_iso": params.expiry.isoformat(),
                    "option_type": params.option_type.value, "model_prob": model_prob,
                    "condition_id": market.condition_id,
                },
            )
            if self.intents is not None and self.live
            else None
        )
        result = self.executor.buy_yes(
            market.yes_token_id,
            plan.size_usd,
            book,
            max_price=min(0.99, model_prob - self.config.entry_threshold),
            fee=fee,
        )
        self.journal_result(intent_id, result)
        self.record(
            {"event": "order", "market_id": market.market_id, "asset": params.asset, **result.to_history(), "ts": _now_iso()}
        )
        if not result.success:
            if result.broker_failure:
                self.risk.record_failure(result.error or result.status)
            self.record(
                {
                    "event": "failed_order",
                    "market_id": market.market_id,
                    "asset": params.asset,
                    "side": "BUY",
                    "error": result.error or result.status,
                    "ts": _now_iso(),
                }
            )
            return result

        pos = make_position(
            market_id=market.market_id,
            question=market.question,
            asset=params.asset,
            strike=params.strike,
            expiry=params.expiry,
            option_type=params.option_type.value,
            yes_token_id=market.yes_token_id,
            yes_price=result.avg_price,
            size_usd=result.filled_usd,
            model_prob=model_prob,
            token_size=result.filled_shares,
            condition_id=market.condition_id,
        )
        self.positions.open_position(pos)
        self.positions.confirm_fill(
            market.market_id,
            result.avg_price,
            yes_token_id=market.yes_token_id,
            size_usd=result.filled_usd,
            token_size=result.filled_shares,
            bid=book.best_bid,
            ask=book.best_ask,
            fee_usd=result.fee_usd,
        )
        if intent_id is not None:
            self.intents.reconcile(intent_id)
        self.risk.record_success(self.positions.marked_equity())
        self.record(
            {
                "event": "open",
                "market_id": market.market_id,
                "question": market.question[:120],
                "asset": params.asset,
                "strike": params.strike,
                "expiry": params.expiry.isoformat(),
                "option_type": params.option_type.value,
                "model_prob": model_prob,
                "yes_price": result.avg_price,
                "bid": book.best_bid,
                "ask": book.best_ask,
                "mid_price": market.yes_price,
                "edge": plan.edge,
                "size_usd": result.filled_usd,
                "requested_size_usd": plan.size_usd,
                "filled_shares": result.filled_shares,
                "complete": result.complete,
                "slippage": result.avg_price - market.yes_price,
                "sigma": sigma,
                "vol_source": vol_source,
                "book_source": book.source,
                "yes_token_id": market.yes_token_id,
                "fill_confirmed": True,
                "ts": _now_iso(),
            }
        )
        self.notify_entry(pos, model_prob=model_prob, bid=book.best_bid, ask=book.best_ask, sigma=sigma)
        return result


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _inc(stats: dict[str, object], key: str, subkey: str | None = None) -> None:
    if subkey is None:
        stats[key] = int(stats.get(key, 0)) + 1  # type: ignore[arg-type]
        return
    bucket = stats.setdefault(key, {})
    if isinstance(bucket, dict):
        bucket[subkey] = int(bucket.get(subkey, 0)) + 1
