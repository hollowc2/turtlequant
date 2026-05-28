"""Market scanner — Gamma API discovery + liquidity/spread/time filters.

Polls the Polymarket Gamma API for active crypto price markets and applies
filters before handing them to the parser/probability engine.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Default filter thresholds (all overridable via MarketScanner constructor)
DEFAULT_MIN_LIQUIDITY = 5_000.0  # USD
DEFAULT_MAX_SPREAD_PCT = 0.03  # 3% of token price
DEFAULT_MIN_HOURS_TO_RESOLUTION = 4.0

# Gamma API paginates at 100; cap at 5 pages (500 markets) — the API orders by
# volume/liquidity descending so the most relevant markets appear first
_PAGE_SIZE = 100
_MAX_PAGES = 5
_REQUEST_TIMEOUT_SECS = 10
_REQUEST_RETRIES = 2
_REQUEST_BACKOFF_SECS = 0.5
_CACHE_TTL_SECS = 15 * 60
_WARNING_COOLDOWN_SECS = 5 * 60
_TRANSIENT_HTTP_STATUSES = {403, 408, 409, 425, 429, 500, 502, 503, 504}


@dataclass
class ActiveMarket:
    """A single Polymarket market that passed all filters."""

    market_id: str
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float  # mid of YES token (0–1)
    bid: float  # best bid on YES token
    ask: float  # best ask on YES token
    spread: float  # ask - bid
    liquidity_usd: float
    resolution_time: datetime  # UTC
    volume_24h: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def spread_pct(self) -> float:
        return self.spread / max(self.yes_price, 0.01)

    @property
    def hours_to_resolution(self) -> float:
        now = datetime.now(UTC)
        delta = self.resolution_time - now
        return delta.total_seconds() / 3600.0


class MarketScanner:
    """Polls the Gamma API and returns filtered ActiveMarket objects."""

    def __init__(
        self,
        min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
        max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
        min_hours_to_resolution: float = DEFAULT_MIN_HOURS_TO_RESOLUTION,
        assets: list[str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.min_liquidity = min_liquidity
        self.max_spread_pct = max_spread_pct
        self.min_hours_to_resolution = min_hours_to_resolution
        # Assets to track, e.g. ["btc", "eth"]; None = all crypto price markets
        self.assets: set[str] = {a.lower() for a in assets} if assets else set()
        self._session = session or self._make_session()
        self._markets_cache: list[dict] = []
        self._markets_cache_at = 0.0
        self._price_cache: dict[str, tuple[float, float]] = {}
        self._last_warning_at: dict[str, float] = {}

    @staticmethod
    def _make_session() -> requests.Session:
        s = requests.Session()
        s.headers.update({"Accept": "application/json", "User-Agent": "turtlequant/0.1"})
        return s

    def get_active_markets(self) -> list[ActiveMarket]:
        """Fetch and filter all active crypto price markets from Gamma API."""
        raw_markets = self._fetch_all_pages()
        result: list[ActiveMarket] = []
        for raw in raw_markets:
            market = self._parse_raw(raw)
            if market is None:
                continue
            if not self._passes_filters(market):
                continue
            result.append(market)
        logger.info("Scanner: %d markets passed filters (fetched %d total)", len(result), len(raw_markets))
        return result

    def _fetch_all_pages(self) -> list[dict]:
        """Fetch pages from the Gamma API (capped at _MAX_PAGES).

        The Gamma API returns markets ordered by volume descending, so the
        most liquid/active markets appear on the first pages. Capping at 5
        pages (500 markets) covers all meaningful price-threshold markets
        without fetching tens of thousands of low-liquidity tail markets.
        """
        results: list[dict] = []
        offset = 0
        page_count = 0
        while page_count < _MAX_PAGES:
            try:
                page = self._get_json(
                    f"{GAMMA_API_BASE}/markets",
                    {
                        "active": "true",
                        "closed": "false",
                        "tag_slug": "crypto",
                        "limit": _PAGE_SIZE,
                        "offset": offset,
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                )
                if not page:
                    break
                results.extend(page)
                page_count += 1
                if len(page) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
                time.sleep(0.1)
            except Exception as exc:
                self._log_api_warning("markets", "Gamma API fetch failed (offset=%d): %s", offset, exc)
                break
        if results:
            self._markets_cache = results
            self._markets_cache_at = time.time()
        elif self._markets_cache and time.time() - self._markets_cache_at <= _CACHE_TTL_SECS:
            logger.info("Gamma API unavailable; using cached market page set (%d markets)", len(self._markets_cache))
            return list(self._markets_cache)
        return results

    def _parse_raw(self, raw: dict) -> ActiveMarket | None:
        """Convert a raw Gamma API dict to ActiveMarket, or None if missing required fields."""
        try:
            question = raw.get("question", "")
            if not question:
                return None

            # Resolution time — authoritative
            res_str = raw.get("endDate") or raw.get("resolutionTime") or raw.get("end_date_iso")
            if not res_str:
                return None
            resolution_time = _parse_iso(res_str)
            if resolution_time is None:
                return None

            # Tokens — Gamma may return list fields or JSON-encoded lists
            # depending on the endpoint.
            tokens = _coerce_list(raw.get("clobTokenIds") or raw.get("clob_token_ids"))
            outcomes = _coerce_list(raw.get("outcomes"))
            yes_token_id = ""
            no_token_id = ""

            if isinstance(tokens, list) and len(tokens) >= 2:
                # Match by outcomes ordering
                if isinstance(outcomes, list) and len(outcomes) >= 2:
                    for i, outcome in enumerate(outcomes):
                        if str(outcome).lower() == "yes" and i < len(tokens):
                            yes_token_id = str(tokens[i])
                        elif str(outcome).lower() == "no" and i < len(tokens):
                            no_token_id = str(tokens[i])
                if not yes_token_id:
                    yes_token_id = str(tokens[0])
                    no_token_id = str(tokens[1]) if len(tokens) > 1 else ""

            # Price / spread
            # Gamma returns bestBid / bestAsk on the YES token (or "price")
            best_bid = float(raw.get("bestBid") or raw.get("best_bid") or 0.0)
            best_ask = float(raw.get("bestAsk") or raw.get("best_ask") or 0.0)
            price = float(raw.get("price") or raw.get("lastTradePrice") or 0.0)

            # Fall back to price as both bid and ask if spread not available
            if best_bid <= 0 and best_ask <= 0 and price > 0:
                best_bid = price
                best_ask = price
            elif best_bid <= 0:
                best_bid = best_ask
            elif best_ask <= 0:
                best_ask = best_bid

            yes_price = (best_bid + best_ask) / 2.0 if (best_bid > 0 or best_ask > 0) else price
            spread = max(0.0, best_ask - best_bid)

            liquidity = float(raw.get("liquidityAmm") or raw.get("volume") or 0.0)
            volume_24h = float(raw.get("volume24hr") or raw.get("volume24h") or 0.0)

            return ActiveMarket(
                market_id=str(raw.get("id", "")),
                condition_id=str(raw.get("conditionId", "")),
                question=question,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                yes_price=yes_price,
                bid=best_bid,
                ask=best_ask,
                spread=spread,
                liquidity_usd=liquidity,
                resolution_time=resolution_time,
                volume_24h=volume_24h,
                raw=raw,
            )
        except Exception as exc:
            logger.debug("Failed to parse market: %s — %s", raw.get("question", "?"), exc)
            return None

    def fetch_market_price(self, market_id: str) -> float | None:
        """Fetch the current (or resolved) YES price for a single market by ID.

        For resolved markets the Gamma API returns the final settlement price
        (1.0 for YES, 0.0 for NO).  Returns None on any error.
        """
        try:
            raw = self._get_json(f"{GAMMA_API_BASE}/markets/{market_id}")
            # Prefer explicit resolution price fields; fall back to last trade price
            for key in ("resolutionPrice", "resolution_price", "price", "lastTradePrice"):
                val = raw.get(key)
                if val is not None:
                    price = float(val)
                    self._price_cache[market_id] = (price, time.time())
                    return price
        except Exception as exc:
            cached = self._price_cache.get(market_id)
            if cached and time.time() - cached[1] <= _CACHE_TTL_SECS:
                logger.info("fetch_market_price(%s) failed; using cached price %.4f", market_id, cached[0])
                return cached[0]
            self._log_api_warning(f"price:{market_id}", "fetch_market_price(%s) failed: %s", market_id, exc)
        return None

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        last_exc: Exception | None = None
        for attempt in range(_REQUEST_RETRIES + 1):
            try:
                resp = self._session.get(url, params=params, timeout=_REQUEST_TIMEOUT_SECS)
                if getattr(resp, "status_code", 200) in _TRANSIENT_HTTP_STATUSES and attempt < _REQUEST_RETRIES:
                    time.sleep(_REQUEST_BACKOFF_SECS * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= _REQUEST_RETRIES:
                    break
                time.sleep(_REQUEST_BACKOFF_SECS * (attempt + 1))
            except Exception:
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"request failed: {url}")

    def _log_api_warning(self, key: str, msg: str, *args: object) -> None:
        now = time.time()
        last = self._last_warning_at.get(key, 0.0)
        if now - last >= _WARNING_COOLDOWN_SECS:
            logger.warning(msg, *args)
            self._last_warning_at[key] = now
        else:
            logger.debug(msg, *args)

    def _passes_filters(self, market: ActiveMarket) -> bool:
        if market.liquidity_usd < self.min_liquidity:
            return False
        if market.yes_price > 0 and market.spread_pct > self.max_spread_pct:
            return False
        if market.hours_to_resolution < self.min_hours_to_resolution:
            return False
        # Asset filter — only apply if assets list was provided
        if self.assets:
            q = market.question.lower()
            if not any(a in q for a in self.assets):
                return False
        return True


def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO-8601 datetime string to UTC datetime."""
    from dateutil import parser as dp

    try:
        dt = dp.parse(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def _coerce_list(value: object) -> list:
    """Normalize Gamma list fields that sometimes arrive as JSON strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []
