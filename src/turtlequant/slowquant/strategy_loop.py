"""SlowQuant strategy loop — main event loop (SlowQuantRunner class).

Pipeline per cycle:
  1. Fetch spot + microstructure data for each asset (Binance 1h + 5m candles)
  2. Recalibrate Merton jump params from 30d 1h returns (every 12 cycles)
  3. Compute vol regime (vol_accel + funding + liquidation composite)
  4. Scan Polymarket markets; filter by regime.min_hours_to_expiry (max 10 days)
  5. BS pre-filter: |BS edge| < 3% → skip MC
  6. MC simulation for candidates passing pre-filter
  7. Score + rank opportunities
  8. Execute top max_trades_per_cycle (paper or raises NotImplementedError for live)
  9. Check exit conditions on all open positions
 10. Sleep for regime.scan_interval_secs
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from turtlequant.data.binance import fetch_klines
from turtlequant.market_parser import parse_market
from turtlequant.market_scanner import ActiveMarket, MarketScanner
from turtlequant.position_manager import PositionManager, make_position
from turtlequant.probability_engine import digital_probability
from turtlequant.vol_surface import VolSurface

from .monte_carlo import FALLBACK_JUMP_PARAMS, JumpParams, calibrate_jump_params
from .monte_carlo import simulate as mc_simulate
from .opportunity_ranker import Opportunity, score_opportunity, should_exit, time_decay_multiplier
from .vol_regime import RegimeState, default_regime, get_regime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ASSET_TO_SYMBOL: dict[str, str] = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}

BS_PREFILTER_THRESHOLD: float = 0.03  # skip MC if |BS edge| < this
MIN_TRADE_USD: float = 1.0  # minimum size worth placing
MAX_HOURS_TO_EXPIRY: float = 240.0  # 10 days — upper bound on scan window
REENTRY_COOLDOWN_SECS: int = 2 * 3600  # 2h — prevent churn after any close

# NAV limits — tighter than TurtleQuant due to short-dated variance
DEFAULT_MAX_PER_MARKET_PCT: float = 0.015  # 1.5%
DEFAULT_MAX_PER_EXPIRY_PCT: float = 0.04  # 4.0%
DEFAULT_MAX_TOTAL_EXPOSURE_PCT: float = 0.20

# Jump recalibration cadence
JUMP_RECALIB_EVERY_N_CYCLES: int = 12
JUMP_CALIB_LOOKBACK_HOURS: int = 30 * 24  # 30 days of 1h bars

HISTORY_FILE_NAME = "slowquant-history.json"

# Polymarket question text uses full asset names ("Bitcoin", "Ethereum"), not tickers.
# Expand asset keys to include both for the scanner's substring filter.
_ASSET_SCANNER_NAMES: dict[str, list[str]] = {
    "btc": ["btc", "bitcoin"],
    "eth": ["eth", "ethereum"],
    "sol": ["sol", "solana"],
    "xrp": ["xrp", "ripple"],
}

# ---------------------------------------------------------------------------
# SlowQuantRunner
# ---------------------------------------------------------------------------


class SlowQuantRunner:
    """Main event loop for the SlowQuant strategy.

    Encapsulates scanner, vol surfaces, position manager, and calibrated
    jump parameters. Call run() to start the loop; set running=False to stop.
    """

    def __init__(
        self,
        assets: list[str],
        state_dir: Path,
        starting_nav: float = 1000.0,
        kelly_fraction: float = 0.25,
        n_sims: int = 50_000,
        min_liquidity: float = 5_000.0,
        max_spread_pct: float = 0.03,
        max_trades_per_cycle: int = 5,
        paper: bool = True,
        dry_run: bool = False,
    ) -> None:
        self.assets = assets
        self.state_dir = state_dir
        self.n_sims = n_sims
        self.max_trades = max_trades_per_cycle
        self.paper = paper
        self.dry_run = dry_run

        # Expand to full names so scanner matches "Bitcoin" / "Ethereum" questions
        scanner_assets = []
        for a in assets:
            scanner_assets.extend(_ASSET_SCANNER_NAMES.get(a, [a]))

        self.scanner = MarketScanner(
            min_liquidity=min_liquidity,
            max_spread_pct=max_spread_pct,
            assets=scanner_assets,
        )
        self.vol_surfaces: dict[str, VolSurface] = {a: VolSurface(asset=a) for a in assets}
        self.pos_mgr = PositionManager(
            starting_nav=starting_nav,
            kelly_fraction=kelly_fraction,
            max_per_market_pct=DEFAULT_MAX_PER_MARKET_PCT,
            max_per_expiry_pct=DEFAULT_MAX_PER_EXPIRY_PCT,
            max_total_exposure_pct=DEFAULT_MAX_TOTAL_EXPOSURE_PCT,
            positions_file=state_dir / "slowquant-positions.json",
        )
        self.jump_params: dict[str, JumpParams] = {a: FALLBACK_JUMP_PARAMS for a in assets}
        self._cycle_count: int = 0
        self.running: bool = True
        self._recently_closed: dict[str, datetime] = {}  # market_id → close time

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main event loop. Runs until self.running is set to False."""
        logger.info(
            "=== SlowQuantRunner started  paper=%s  dry_run=%s  assets=%s  n_sims=%d ===",
            self.paper,
            self.dry_run,
            self.assets,
            self.n_sims,
        )
        regime = default_regime()
        last_scan_time = 0.0

        while self.running:
            now = time.time()
            if now - last_scan_time < regime.scan_interval_secs:
                time.sleep(1)
                continue

            last_scan_time = now
            self._cycle_count += 1

            try:
                regime = self._run_cycle(regime)
            except Exception as exc:
                logger.error("Cycle %d error: %s", self._cycle_count, exc, exc_info=True)
                time.sleep(10)

        logger.info("SlowQuantRunner stopped.")

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def _run_cycle(self, prev_regime: RegimeState) -> RegimeState:
        """Execute one full scan-and-trade cycle.

        Returns the new RegimeState (used to set the next sleep interval).
        """
        # 1. Fetch market data
        spots: dict[str, float] = {}
        prices_1h: dict[str, np.ndarray] = {}
        prices_5m: dict[str, np.ndarray] = {}
        funding_zscore: dict[str, float] = {}
        liq_1h_usd: dict[str, float] = {}
        liq_baseline_usd: dict[str, float] = {}

        for asset in self.assets:
            data = self._fetch_market_data(asset)
            if data is None:
                continue
            spots[asset] = data["spot"]
            prices_1h[asset] = data["prices_1h"]
            prices_5m[asset] = data["prices_5m"]
            funding_zscore[asset] = data["funding_zscore"]
            liq_1h_usd[asset] = data["liq_1h_usd"]
            liq_baseline_usd[asset] = data["liq_baseline_usd"]

        if not spots:
            logger.warning("No spot data available — skipping cycle %d", self._cycle_count)
            return prev_regime

        # 2. Recalibrate jump params every N cycles
        if self._cycle_count % JUMP_RECALIB_EVERY_N_CYCLES == 1:
            for asset in self.assets:
                arr = prices_1h.get(asset)
                if arr is not None and len(arr) > 10:
                    log_rets = np.diff(np.log(arr.astype(float)))
                    self.jump_params[asset] = calibrate_jump_params(log_rets)

        # 3. Compute regime (primary asset drives regime signal)
        primary = self.assets[0]
        regime = get_regime(
            spot_prices_5m=prices_5m.get(primary, np.array([])),
            funding_zscore=funding_zscore.get(primary, 0.0),
            liq_volume_1h_usd=liq_1h_usd.get(primary, 0.0),
            liq_baseline_usd=liq_baseline_usd.get(primary, 1.0),
        )

        # 4. Scan markets — filtered by regime expiry window (max 10 days)
        try:
            markets = self.scanner.get_active_markets()
        except Exception as exc:
            logger.warning("Market scan failed: %s", exc)
            return regime

        in_window = [m for m in markets if regime.min_hours_to_expiry <= m.hours_to_resolution <= MAX_HOURS_TO_EXPIRY]
        logger.info(
            "Cycle %d: %d markets in window [%.0fh–%.0fh]  regime=%s",
            self._cycle_count,
            len(in_window),
            regime.min_hours_to_expiry,
            MAX_HOURS_TO_EXPIRY,
            regime.level.upper(),
        )

        # 5–7. Pipeline: BS prefilter → MC → score
        opportunities: list[Opportunity] = []
        for market in in_window:
            if not self.running:
                break
            opp = self._evaluate_market(market, spots, regime)
            if opp is not None:
                opportunities.append(opp)

        # 8. Execute top N (skip markets already held or in cooldown)
        opportunities.sort(key=lambda o: o.score, reverse=True)
        for opp in opportunities[: self.max_trades]:
            if not self.running:
                break
            if self.pos_mgr.has_position(opp.market.market_id):
                continue
            closed_at = self._recently_closed.get(opp.market.market_id)
            if closed_at and (datetime.now(UTC) - closed_at).total_seconds() < REENTRY_COOLDOWN_SECS:
                logger.debug("Cooldown active for %s — skip re-entry", opp.market.market_id[:16])
                continue
            self._execute_trade(opp)

        # 9. Check exits on all open positions
        self._check_exits(spots, regime)

        return regime

    # ------------------------------------------------------------------
    # Market evaluation pipeline
    # ------------------------------------------------------------------

    def _evaluate_market(
        self,
        market: ActiveMarket,
        spots: dict[str, float],
        regime: RegimeState,
    ) -> Opportunity | None:
        """Run BS prefilter → MC → score for a single market.

        Returns an Opportunity if MC edge clears regime threshold, else None.
        """
        try:
            params = parse_market(market.question, market.resolution_time)
            if params is None:
                return None
            if params.asset not in self.assets:
                return None

            spot = spots.get(params.asset)
            if spot is None or spot <= 0:
                return None

            yes_price = market.yes_price
            if yes_price <= 0.02 or yes_price >= 0.98:
                return None  # near-certain — no edge to capture

            vs = self.vol_surfaces[params.asset]
            sigma = vs.get_iv(spot, params.strike, params.expiry)

            now = datetime.now(UTC)
            T = max((params.expiry - now).total_seconds() / (365 * 86400), 1e-6)

            # BS pre-filter (fast)
            bs_prob = digital_probability(spot, params.strike, T, sigma)
            bs_edge = bs_prob - yes_price
            if abs(bs_edge) < BS_PREFILTER_THRESHOLD:
                return None

            # Full MC simulation
            mc_prob = mc_simulate(
                S0=spot,
                K=params.strike,
                T=T,
                sigma=sigma,
                jump_params=self.jump_params.get(params.asset, FALLBACK_JUMP_PARAMS),
                n_sims=self.n_sims,
            )
            mc_edge = mc_prob - yes_price

            if mc_edge < regime.edge_threshold:
                return None

            hours_left = market.hours_to_resolution
            size = self.pos_mgr.kelly_size(mc_edge, mc_prob, yes_price)
            size *= time_decay_multiplier(hours_left)
            size *= regime.size_multiplier

            if size < MIN_TRADE_USD:
                return None

            if not self.pos_mgr.has_expiry_headroom(params.expiry, size):
                logger.debug("Per-expiry cap reached for %s — skip", params.expiry.strftime("%Y-%m-%d"))
                return None

            opp_score = score_opportunity(mc_edge, market.liquidity_usd, spot, params.strike, hours_left, regime)

            logger.info(
                "[SIGNAL] %s %s K=%.0f exp=%s  bs=%.4f mc=%.4f mkt=%.4f edge=+%.4f  "
                "score=%.4f  size=$%.2f  σ=%.3f  hours=%.1f",
                params.asset.upper(),
                params.option_type.value,
                params.strike,
                params.expiry.strftime("%Y-%m-%d"),
                bs_prob,
                mc_prob,
                yes_price,
                mc_edge,
                opp_score,
                size,
                sigma,
                hours_left,
            )

            return Opportunity(
                market=market,
                params=params,
                sigma=sigma,
                bs_prob=bs_prob,
                bs_edge=bs_edge,
                mc_prob=mc_prob,
                mc_edge=mc_edge,
                score=opp_score,
                recommended_size_usd=size,
            )

        except Exception as exc:
            logger.debug("Evaluate error (%s): %s", market.market_id[:16], exc)
            return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _execute_trade(self, opp: Opportunity) -> None:
        """Place a YES token buy order."""
        if self.dry_run:
            logger.info("[DRY-RUN] Would buy: %s", opp.market.question[:80])
            return

        if not self._buy_yes(opp.market.market_id, opp.market.yes_price, opp.recommended_size_usd):
            return

        pos = make_position(
            market_id=opp.market.market_id,
            question=opp.market.question,
            asset=opp.params.asset,
            strike=opp.params.strike,
            expiry=opp.params.expiry,
            option_type=opp.params.option_type.value,
            yes_token_id=opp.market.yes_token_id,
            yes_price=opp.market.yes_price,
            size_usd=opp.recommended_size_usd,
            model_prob=opp.mc_prob,
        )
        self.pos_mgr.open_position(pos)
        self._append_history(
            {
                "event": "open",
                "market_id": opp.market.market_id,
                "question": opp.market.question[:120],
                "asset": opp.params.asset,
                "strike": opp.params.strike,
                "expiry": opp.params.expiry.isoformat(),
                "option_type": opp.params.option_type.value,
                "bs_prob": round(opp.bs_prob, 6),
                "mc_prob": round(opp.mc_prob, 6),
                "yes_price": round(opp.market.yes_price, 6),
                "edge": round(opp.mc_edge, 6),
                "size_usd": round(opp.recommended_size_usd, 4),
                "sigma": round(opp.sigma, 6),
                "score": round(opp.score, 6),
                "ts": datetime.now(UTC).isoformat(),
            }
        )

    # ------------------------------------------------------------------
    # Exit checking
    # ------------------------------------------------------------------

    def _check_exits(self, spots: dict[str, float], regime: RegimeState) -> None:  # noqa: ARG002
        """Reprice open positions and close those triggering exit conditions."""
        for pos in list(self.pos_mgr.all_positions()):
            try:
                now = datetime.now(UTC)

                # Auto-close positions whose expiry has passed
                if now >= pos.expiry:
                    resolved_price = self.scanner.fetch_market_price(pos.market_id)
                    if resolved_price is None:
                        logger.warning(
                            "[EXPIRED] Could not fetch resolved price for %s — using entry price (no P&L)",
                            pos.market_id[:16],
                        )
                        resolved_price = pos.entry_price
                    logger.info(
                        "[EXPIRED] %s K=%.0f exp=%s resolved=%.4f — closing",
                        pos.asset.upper(),
                        pos.strike,
                        pos.expiry_iso[:10],
                        resolved_price,
                    )
                    if not self.dry_run:
                        self._sell_yes(pos.market_id, pos.yes_token_id, pos.size_usd, resolved_price)
                    _pos, pnl = self.pos_mgr.close_position(pos.market_id, exit_price=resolved_price, reason="expired")
                    self._recently_closed[pos.market_id] = now
                    self._append_history(
                        {
                            "event": "close",
                            "market_id": pos.market_id,
                            "asset": pos.asset,
                            "strike": pos.strike,
                            "reason": "expired",
                            "exit_price": resolved_price,
                            "pnl": pnl,
                            "ts": now.isoformat(),
                        }
                    )
                    continue

                spot = spots.get(pos.asset)
                if spot is None:
                    continue

                vs = self.vol_surfaces.get(pos.asset)
                if vs is None:
                    continue

                sigma = vs.get_iv(spot, pos.strike, pos.expiry)
                T = max((pos.expiry - now).total_seconds() / (365 * 86400), 1e-6)
                hours_left = (pos.expiry - now).total_seconds() / 3600

                # Use fewer sims for exit checks to reduce latency
                current_mc_prob = mc_simulate(
                    S0=spot,
                    K=pos.strike,
                    T=T,
                    sigma=sigma,
                    jump_params=self.jump_params.get(pos.asset, FALLBACK_JUMP_PARAMS),
                    n_sims=min(self.n_sims, 20_000),
                )

                # Proxy market price with entry price; scanner refreshes on next scan
                current_market_price = pos.entry_price

                exit_flag, reason = should_exit(pos, current_mc_prob, current_market_price, hours_left)

                logger.info(
                    "[HOLD] %s K=%.0f exp=%s  mc_p=%.4f entry_p=%.4f edge=%+.4f  hours=%.1f",
                    pos.asset.upper(),
                    pos.strike,
                    pos.expiry_iso[:10],
                    current_mc_prob,
                    pos.entry_price,
                    current_mc_prob - pos.entry_price,
                    hours_left,
                )

                if exit_flag:
                    if not self.dry_run:
                        self._sell_yes(pos.market_id, pos.yes_token_id, pos.size_usd, current_market_price)
                    _pos, pnl = self.pos_mgr.close_position(
                        pos.market_id, exit_price=current_market_price, reason=reason
                    )
                    self._recently_closed[pos.market_id] = now
                    self._append_history(
                        {
                            "event": "close",
                            "market_id": pos.market_id,
                            "asset": pos.asset,
                            "strike": pos.strike,
                            "reason": reason,
                            "mc_prob": round(current_mc_prob, 6),
                            "entry_price": pos.entry_price,
                            "exit_price": current_market_price,
                            "pnl": pnl,
                            "hours_left": round(hours_left, 2),
                            "ts": now.isoformat(),
                        }
                    )

            except Exception as exc:
                logger.warning("Exit check failed for %s: %s", pos.market_id[:16], exc)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_market_data(self, asset: str) -> dict | None:
        """Fetch spot price, 1h/5m candles, and microstructure for one asset.

        Returns a dict with keys:
            spot, prices_1h, prices_5m, funding_zscore, liq_1h_usd, liq_baseline_usd
        Returns None on failure.
        """
        symbol = ASSET_TO_SYMBOL.get(asset)
        if symbol is None:
            return None

        end_ms = int(datetime.now(UTC).timestamp() * 1000)
        try:
            # 30d of 1h candles for jump calibration + regime vol signal
            start_1h = end_ms - JUMP_CALIB_LOOKBACK_HOURS * 3_600_000
            df_1h = fetch_klines(symbol, "1h", start_1h, end_ms)
            if df_1h.empty:
                logger.warning("No 1h data for %s", asset.upper())
                return None

            prices_1h = df_1h["close"].astype(float).values
            spot = float(prices_1h[-1])

            # 2h of 5m candles for short-term vol signal
            start_5m = end_ms - 2 * 3_600_000
            df_5m = fetch_klines(symbol, "5m", start_5m, end_ms)
            prices_5m = df_5m["close"].astype(float).values if not df_5m.empty else prices_1h[-12:]

            # Microstructure signals from enriched 48h of 1h candles
            funding_zscore = 0.0
            liq_1h_usd = 0.0
            liq_baseline_usd = 1.0
            try:
                from polymarket_algo.data import enrich_candles

                start_enrich = end_ms - 48 * 3_600_000
                df_enrich = fetch_klines(symbol, "1h", start_enrich, end_ms)
                if not df_enrich.empty:
                    enriched = enrich_candles(df_enrich, symbol, include_liq=True, include_funding=True)
                    if "funding_zscore" in enriched.columns:
                        last = enriched["funding_zscore"].dropna()
                        if not last.empty:
                            funding_zscore = float(last.iloc[-1])
                    if "liq_short_usd" in enriched.columns:
                        liq_series = enriched["liq_short_usd"].dropna()
                        if not liq_series.empty:
                            liq_1h_usd = float(liq_series.iloc[-1])
                            liq_baseline_usd = float(liq_series.mean()) or 1.0
            except Exception as exc:
                logger.debug("Microstructure enrich skipped for %s: %s", asset.upper(), exc)

            return {
                "spot": spot,
                "prices_1h": prices_1h,
                "prices_5m": prices_5m,
                "funding_zscore": funding_zscore,
                "liq_1h_usd": liq_1h_usd,
                "liq_baseline_usd": liq_baseline_usd,
            }

        except Exception as exc:
            logger.warning("Data fetch failed for %s: %s", asset.upper(), exc)
            return None

    # ------------------------------------------------------------------
    # Order helpers (paper / future live)
    # ------------------------------------------------------------------

    def _buy_yes(self, market_id: str, yes_price: float, size_usd: float) -> bool:
        if self.paper:
            logger.info("[PAPER] BUY YES  market=%.16s  size=$%.2f  price=%.4f", market_id, size_usd, yes_price)
            return True
        raise NotImplementedError("Live order placement not yet implemented for SlowQuant. Use --paper.")

    def _sell_yes(self, market_id: str, yes_token_id: str, size_usd: float, yes_price: float) -> bool:  # noqa: ARG002
        if self.paper:
            logger.info("[PAPER] SELL YES  market=%.16s  size=$%.2f  price=%.4f", market_id, size_usd, yes_price)
            return True
        raise NotImplementedError("Live order placement not yet implemented for SlowQuant. Use --paper.")

    # ------------------------------------------------------------------
    # History persistence
    # ------------------------------------------------------------------

    def _append_history(self, entry: dict) -> None:
        history_file = self.state_dir / HISTORY_FILE_NAME
        try:
            if history_file.exists():
                with history_file.open() as f:
                    history: list[dict] = json.load(f)
            else:
                history = []
            history.append(entry)
            history_file.write_text(json.dumps(history, indent=2))
        except Exception as exc:
            logger.warning("History write failed: %s", exc)
