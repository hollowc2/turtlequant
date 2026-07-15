"""CLOB execution and bid/ask-aware fill modeling for TurtleQuant."""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

POLYMARKET_CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_CRYPTO_TAKER_FEE_RATE = 0.07
_BOOK_RETRIES = 2
_AMOUNT_SCALE = Decimal("1000000")
_BPS = Decimal("10000")


def _polymarket_env() -> tuple[str, str, str, str, int, str]:
    """Return (private_key, api_key, api_secret, api_passphrase, signature_type, funder)."""
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PRIVATE_KEY", "")
    api_key = (
        os.getenv("POLYMARKET_API_KEY")
        or os.getenv("CLOB_API_KEY")
        or os.getenv("API_KEY", "")
    )
    api_secret = (
        os.getenv("POLYMARKET_API_SECRET")
        or os.getenv("CLOB_API_SECRET")
        or os.getenv("SECRET", "")
    )
    api_passphrase = (
        os.getenv("POLYMARKET_API_PASSPHRASE")
        or os.getenv("CLOB_API_PASSPHRASE")
        or os.getenv("PASSPHRASE", "")
    )
    signature_type = int(
        os.getenv("POLYMARKET_SIGNATURE_TYPE", os.getenv("SIGNATURE_TYPE", "0"))
    )
    funder = ""
    if signature_type:
        funder = (
            os.getenv("POLYMARKET_FUNDER")
            or os.getenv("FUNDER_ADDRESS")
            or os.getenv("DEPOSIT_WALLET_ADDRESS", "")
        )
    return private_key, api_key, api_secret, api_passphrase, signature_type, funder
_BOOK_RETRY_BACKOFF_SECS = 0.5
_BOOK_WARNING_COOLDOWN_SECS = 5 * 60


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    token_id: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    source: str = "clob"

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def mid(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2.0
        return self.best_bid or self.best_ask

    @property
    def spread(self) -> float:
        if self.best_bid <= 0 or self.best_ask <= 0:
            return 0.0
        return max(0.0, self.best_ask - self.best_bid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "bid": self.best_bid,
            "ask": self.best_ask,
            "mid": self.mid,
            "spread": self.spread,
            "observed_at": self.observed_at,
            "source": self.source,
            "bid_depth": [asdict(level) for level in self.bids[:10]],
            "ask_depth": [asdict(level) for level in self.asks[:10]],
        }


@dataclass(frozen=True)
class FillEstimate:
    side: OrderSide
    requested_usd: float = 0.0
    requested_shares: float = 0.0
    filled_usd: float = 0.0
    filled_shares: float = 0.0
    avg_price: float = 0.0
    complete: bool = False
    levels_used: int = 0

    @property
    def unfilled_usd(self) -> float:
        return max(0.0, self.requested_usd - self.filled_usd)

    @property
    def unfilled_shares(self) -> float:
        return max(0.0, self.requested_shares - self.filled_shares)


@dataclass(frozen=True)
class ExecutionResult:
    side: OrderSide
    token_id: str
    requested_usd: float = 0.0
    requested_shares: float = 0.0
    filled_usd: float = 0.0
    filled_shares: float = 0.0
    avg_price: float = 0.0
    fee_usd: float | None = None
    complete: bool = False
    success: bool = False
    status: str = "not_sent"
    order_id: str = ""
    error: str = ""
    quote: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_history(self) -> dict[str, Any]:
        return {
            "side": self.side.value,
            "token_id": self.token_id,
            "requested_usd": self.requested_usd,
            "requested_shares": self.requested_shares,
            "filled_usd": self.filled_usd,
            "filled_shares": self.filled_shares,
            "avg_fill_price": self.avg_price,
            "fee_usd": self.fee_usd,
            "complete": self.complete,
            "success": self.success,
            "status": self.status,
            "order_id": self.order_id,
            "error": self.error,
            "quote": self.quote,
            "raw": self.raw,
        }


def estimate_buy_fill(book: OrderBook, amount_usd: float) -> FillEstimate:
    remaining = max(0.0, amount_usd)
    filled_usd = 0.0
    shares = 0.0
    levels_used = 0
    for level in sorted((lvl for lvl in book.asks if lvl.price > 0 and lvl.size > 0), key=lambda lvl: lvl.price):
        if remaining <= 1e-9:
            break
        level_capacity_usd = level.price * level.size
        take_usd = min(remaining, level_capacity_usd)
        filled_usd += take_usd
        shares += take_usd / level.price
        remaining -= take_usd
        levels_used += 1
    avg_price = filled_usd / shares if shares > 0 else 0.0
    return FillEstimate(
        side=OrderSide.BUY,
        requested_usd=amount_usd,
        filled_usd=filled_usd,
        filled_shares=shares,
        avg_price=avg_price,
        complete=remaining <= 1e-6,
        levels_used=levels_used,
    )


def estimate_sell_fill(book: OrderBook, shares: float) -> FillEstimate:
    remaining = max(0.0, shares)
    filled_usd = 0.0
    filled_shares = 0.0
    levels_used = 0
    for level in sorted((lvl for lvl in book.bids if lvl.price > 0 and lvl.size > 0), key=lambda lvl: lvl.price, reverse=True):
        if remaining <= 1e-9:
            break
        take_shares = min(remaining, level.size)
        filled_shares += take_shares
        filled_usd += take_shares * level.price
        remaining -= take_shares
        levels_used += 1
    avg_price = filled_usd / filled_shares if filled_shares > 0 else 0.0
    return FillEstimate(
        side=OrderSide.SELL,
        requested_shares=shares,
        filled_usd=filled_usd,
        filled_shares=filled_shares,
        avg_price=avg_price,
        complete=remaining <= 1e-6,
        levels_used=levels_used,
    )


def synthetic_book(token_id: str, bid: float, ask: float, source: str = "synthetic") -> OrderBook:
    bid = max(0.0, float(bid or 0.0))
    ask = max(0.0, float(ask or 0.0))
    return OrderBook(
        token_id=token_id,
        bids=[BookLevel(bid, 1_000_000.0)] if bid > 0 else [],
        asks=[BookLevel(ask, 1_000_000.0)] if ask > 0 else [],
        source=source,
    )


class ExecutionClient:
    """Paper, shadow, or live CLOB execution facade."""

    def __init__(
        self,
        *,
        mode: str = "paper",
        clob_client: Any | None = None,
        host: str = POLYMARKET_CLOB_HOST,
        chain_id: int = 137,
        allow_live: bool = False,
    ) -> None:
        self.mode = mode
        self.host = host
        self.chain_id = chain_id
        self.allow_live = allow_live
        self._client = clob_client if clob_client is not None else self._build_clob_client()
        self._last_book_warning_at: dict[str, float] = {}

    @classmethod
    def from_env(cls, *, mode: str, allow_live: bool = False) -> "ExecutionClient":
        return cls(
            mode=mode,
            host=os.getenv("POLYMARKET_CLOB_HOST", POLYMARKET_CLOB_HOST),
            chain_id=int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
            allow_live=allow_live,
        )

    def get_order_book(self, token_id: str, fallback_bid: float = 0.0, fallback_ask: float = 0.0) -> OrderBook:
        if self._client is not None:
            last_exc: Exception | None = None
            for attempt in range(_BOOK_RETRIES + 1):
                try:
                    raw = self._client.get_order_book(token_id)
                    book = _parse_clob_book(token_id, raw)
                    if book.best_bid > 0 or book.best_ask > 0:
                        return book
                except Exception as exc:
                    last_exc = exc
                    if attempt >= _BOOK_RETRIES:
                        break
                    time.sleep(_BOOK_RETRY_BACKOFF_SECS * (attempt + 1))
            if last_exc is not None:
                self._log_book_warning(token_id, "CLOB order book fetch failed for %s: %s", token_id[:16], last_exc)
        return synthetic_book(token_id, fallback_bid, fallback_ask)

    def get_market_fee_rate(self, market_id: str, token_id: str = "") -> float | None:
        """Return the market's taker fee rate as a fraction, or None if unknown.

        SDK releases expose this either as ``get_fee_rate`` (basis points) or
        market-info data.  Unknown fees must be handled by the entry gate, not
        replaced with an old global default.
        """
        if self._client is None:
            return None
        lookups = (
            ("get_fee_rate_bps", token_id or market_id),
            ("get_fee_rate", token_id or market_id),
            ("get_clob_market_info", market_id),
            ("getClobMarketInfo", market_id),
        )
        for method_name, identifier in lookups:
            if not identifier:
                continue
            method = getattr(self._client, method_name, None)
            if not callable(method):
                continue
            try:
                raw = method(identifier)
                rate = _fee_rate_fraction(raw)
            except Exception as exc:
                logger.warning("CLOB fee-rate lookup failed for %s via %s: %s", market_id[:16], method_name, exc)
                continue
            if rate is not None:
                return rate
        return None

    def get_order(self, order_id: str) -> Any:
        if self._client is None:
            raise RuntimeError("CLOB client is unavailable")
        return self._client.get_order(order_id)

    def get_trades(self, token_id: str) -> Any:
        """Expose trade history for operator investigation; recovery remains order-evidence only."""
        if self._client is None:
            raise RuntimeError("CLOB client is unavailable")
        try:
            from py_clob_client_v2 import TradeParams
            return self._client.get_trades(TradeParams(asset_id=token_id))
        except ImportError:
            return self._client.get_trades(token_id)

    def _log_book_warning(self, token_id: str, msg: str, *args: object) -> None:
        now = time.time()
        last = self._last_book_warning_at.get(token_id, 0.0)
        if now - last >= _BOOK_WARNING_COOLDOWN_SECS:
            logger.warning(msg, *args)
            self._last_book_warning_at[token_id] = now
        else:
            logger.debug(msg, *args)

    def buy_yes(
        self, token_id: str, amount_usd: float, book: OrderBook, *,
        max_price: float | None = None, fee_rate: float = 0.0,
    ) -> ExecutionResult:
        estimate = estimate_buy_fill(book, amount_usd)
        if self.mode != "live":
            return _paper_result(token_id, estimate, "shadow" if self.mode == "shadow" else "paper", book, fee_rate)
        return self._post_market_order(
            token_id, OrderSide.BUY, amount_usd=amount_usd, shares=0.0,
            estimate=estimate, book=book, limit_price=max_price,
        )

    def sell_yes(
        self, token_id: str, shares: float, book: OrderBook, *,
        min_price: float | None = None, fee_rate: float = 0.0,
    ) -> ExecutionResult:
        estimate = estimate_sell_fill(book, shares)
        if self.mode != "live":
            return _paper_result(token_id, estimate, "shadow" if self.mode == "shadow" else "paper", book, fee_rate)
        return self._post_market_order(
            token_id, OrderSide.SELL, amount_usd=0.0, shares=shares,
            estimate=estimate, book=book, limit_price=min_price or estimate.avg_price,
        )

    def _build_clob_client(self) -> Any | None:
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient
        except ImportError:
            return None

        private_key, api_key, api_secret, api_passphrase, signature_type, funder = _polymarket_env()

        if self.mode == "live" and not private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY or PRIVATE_KEY is required for live CLOB execution")
        if not private_key:
            return ClobClient(host=self.host, chain_id=self.chain_id)

        kwargs: dict[str, Any] = {
            "host": self.host,
            "chain_id": self.chain_id,
            "key": private_key,
        }
        if signature_type:
            kwargs["signature_type"] = signature_type
        if funder:
            kwargs["funder"] = funder

        if api_key and api_secret and api_passphrase:
            kwargs["creds"] = ApiCreds(
                api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase
            )
            return ClobClient(**kwargs)

        client = ClobClient(**kwargs)
        creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)
        logger.info("Derived CLOB API credentials from private key")
        return client

    def _post_market_order(
        self,
        token_id: str,
        side: OrderSide,
        *,
        amount_usd: float,
        shares: float,
        estimate: FillEstimate,
        book: OrderBook,
        limit_price: float | None,
    ) -> ExecutionResult:
        if not self.allow_live:
            return _failed_result(token_id, estimate, book, "live execution requires --i-accept-live-risk")
        if self._client is None:
            return _failed_result(token_id, estimate, book, "py_clob_client_v2 is not installed or configured")
        if book.source != "clob":
            return _failed_result(token_id, estimate, book, "live orders require a real CLOB book")
        if estimate.filled_shares <= 0 or estimate.avg_price <= 0:
            return _failed_result(token_id, estimate, book, "no executable depth")
        if limit_price is None or not 0.0 < limit_price < 1.0:
            return _failed_result(token_id, estimate, book, "live order requires a valid worst-price limit")

        try:
            from py_clob_client_v2 import MarketOrderArgs, OrderType, Side
        except ImportError as exc:
            return _failed_result(token_id, estimate, book, str(exc))

        clob_side = Side.BUY if side == OrderSide.BUY else Side.SELL
        amount = amount_usd if side == OrderSide.BUY else shares
        try:
            raw = self._client.create_and_post_market_order(
                order_args=MarketOrderArgs(
                    token_id=token_id,
                    amount=amount,
                    side=clob_side,
                    price=limit_price,
                    order_type=OrderType.FAK,
                ),
                order_type=OrderType.FAK,
            )
        except Exception as exc:
            return _failed_result(token_id, estimate, book, str(exc))

        try:
            parsed = _parse_order_response(raw, side, amount_usd, shares)
        except RuntimeError as exc:
            return _failed_result(token_id, estimate, book, str(exc), status="pending_reconciliation", raw=raw)
        return ExecutionResult(
            side=side,
            token_id=token_id,
            requested_usd=amount_usd,
            requested_shares=shares,
            filled_usd=parsed.filled_usd,
            filled_shares=parsed.filled_shares,
            avg_price=parsed.avg_price,
            complete=parsed.complete,
            success=parsed.filled_shares > 0,
            status=str(_dict_get(raw, "status", "posted")),
            order_id=str(_dict_get(raw, "orderID", _dict_get(raw, "order_id", ""))),
            quote=book.to_dict(),
            raw=raw if isinstance(raw, dict) else {"response": str(raw)},
        )


def _paper_result(
    token_id: str, estimate: FillEstimate, status: str, book: OrderBook, fee_rate: float
) -> ExecutionResult:
    return ExecutionResult(
        side=estimate.side,
        token_id=token_id,
        requested_usd=estimate.requested_usd,
        requested_shares=estimate.requested_shares,
        filled_usd=estimate.filled_usd,
        filled_shares=estimate.filled_shares,
        avg_price=estimate.avg_price,
        fee_usd=taker_fee(estimate.filled_shares, estimate.avg_price, fee_rate),
        complete=estimate.complete,
        success=estimate.filled_shares > 0,
        status=status,
        quote=book.to_dict(),
    )


def _failed_result(
    token_id: str, estimate: FillEstimate, book: OrderBook, error: str, *, status: str = "failed", raw: Any = None
) -> ExecutionResult:
    return ExecutionResult(
        side=estimate.side,
        token_id=token_id,
        requested_usd=estimate.requested_usd,
        requested_shares=estimate.requested_shares,
        filled_usd=0.0,
        filled_shares=0.0,
        avg_price=0.0,
        complete=False,
        success=False,
        status=status,
        error=error,
        quote=book.to_dict(),
        raw=raw if isinstance(raw, dict) else {},
    )


def _parse_clob_book(token_id: str, raw: Any) -> OrderBook:
    bids = _parse_levels(_dict_get(raw, "bids", []), reverse=True)
    asks = _parse_levels(_dict_get(raw, "asks", []), reverse=False)
    return OrderBook(token_id=token_id, bids=bids, asks=asks, source="clob")


def _parse_levels(raw_levels: Any, *, reverse: bool) -> list[BookLevel]:
    levels: list[BookLevel] = []
    for raw in raw_levels or []:
        price = _dict_get(raw, "price", None)
        size = _dict_get(raw, "size", _dict_get(raw, "amount", None))
        try:
            level = BookLevel(price=float(price), size=float(size))
        except (TypeError, ValueError):
            continue
        if level.price > 0 and level.size > 0:
            levels.append(level)
    return sorted(levels, key=lambda level: level.price, reverse=reverse)


def _parse_order_response(
    raw: Any,
    side: OrderSide,
    requested_usd: float,
    requested_shares: float,
) -> FillEstimate:
    if _dict_get(raw, "success") is not True or str(_dict_get(raw, "status", "")).lower() != "matched":
        raise RuntimeError(_dict_get(raw, "errorMsg", "") or f"unconfirmed status={_dict_get(raw, 'status', None)}")
    making = _scaled_amount(_dict_get(raw, "makingAmount", _dict_get(raw, "making_amount", None)))
    taking = _scaled_amount(_dict_get(raw, "takingAmount", _dict_get(raw, "taking_amount", None)))
    if taking <= 0 or making <= 0:
        raise RuntimeError("matched order response has no positive fill amounts")
    if side == OrderSide.BUY:
        filled_usd = making
        filled_shares = taking
    else:
        filled_shares = making
        filled_usd = taking
    avg_price = filled_usd / filled_shares if filled_shares > 0 else 0.0
    return FillEstimate(
        side=side,
        requested_usd=requested_usd,
        requested_shares=requested_shares,
        filled_usd=filled_usd,
        filled_shares=filled_shares,
        avg_price=avg_price,
        complete=(filled_usd >= requested_usd - 1e-6 if side == OrderSide.BUY else filled_shares >= requested_shares - 1e-6),
    )


def confirmed_fill(raw: Any, side: OrderSide, requested: float) -> FillEstimate:
    """Parse a terminal broker lookup; estimates are deliberately never accepted."""
    status = str(_dict_get(raw, "status", "")).lower()
    if status != "matched":
        raise RuntimeError(_dict_get(raw, "errorMsg", "") or f"unconfirmed status={status or None}")
    normalized = dict(raw) if isinstance(raw, dict) else raw
    if isinstance(normalized, dict):
        normalized.setdefault("success", True)
    return _parse_order_response(
        normalized, side, requested if side == OrderSide.BUY else 0.0, requested if side == OrderSide.SELL else 0.0
    )


def _dict_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _scaled_amount(value: Any) -> float:
    try:
        return float(Decimal(str(value)) / _AMOUNT_SCALE)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("invalid fixed-point fill amount") from exc


def taker_fee(shares: float, price: float, fee_rate: float) -> float:
    """Return the CLOB taker fee in USDC for a confirmed fill."""
    if not all(math.isfinite(value) for value in (shares, price, fee_rate)):
        raise ValueError("fee inputs must be finite")
    if shares < 0 or fee_rate < 0 or not 0 <= price <= 1:
        raise ValueError("invalid fee inputs")
    return shares * fee_rate * price * (1.0 - price)


def _fee_rate_fraction(raw: Any) -> float | None:
    """Normalize SDK fee data (usually basis points) to a rate fraction."""
    fee_data = _dict_get(raw, "fd", None)
    if fee_data is not None:
        try:
            rate = Decimal(str(_dict_get(fee_data, "r"))) * (Decimal(10) ** -int(_dict_get(fee_data, "e")))
            result = float(rate)
            return result if 0 <= result <= 1 else None
        except (InvalidOperation, TypeError, ValueError):
            return None
    if isinstance(raw, (int, float, Decimal, str)):
        value = raw
        is_bps = True
    else:
        value = _dict_get(raw, "base_fee", _dict_get(raw, "fee_rate_bps", None))
        is_bps = value is not None
        if value is None:
            value = _dict_get(raw, "feeRate", _dict_get(raw, "fee_rate", None))
    try:
        rate = Decimal(str(value))
        if is_bps or rate > 1:
            rate /= _BPS
        result = float(rate)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if 0 <= result <= 1 else None
