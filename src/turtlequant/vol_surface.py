"""Vol surface — Deribit IV fetcher + strike/expiry interpolation + realized vol fallback.

Primary: Deribit mark_iv per instrument, interpolated to (strike, expiry).
Fallback: 30-day realized vol from Binance daily closes (always available).

Deribit IV refresh: every 5 minutes (stays well within rate limits).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import requests

logger = logging.getLogger(__name__)

DERIBIT_API_BASE = "https://www.deribit.com/api/v2/public"
DERIBIT_API_PRIVATE = "https://www.deribit.com/api/v2/private"
_DERIBIT_REFRESH_SECS = 300  # 5 minutes
_REALIZED_LOOKBACK_DAYS = 30
_TOKEN_EXPIRY_SECS = 850  # Deribit tokens last ~900s; refresh before expiry

_ASSET_TO_DERIBIT_CCY: dict[str, str] = {
    "btc": "BTC",
    "eth": "ETH",
    "sol": "SOL",
}

# Binance daily intervals map (asset → symbol)
_ASSET_TO_SYMBOL: dict[str, str] = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}


@dataclass
class IVPoint:
    """A single implied-vol data point from Deribit."""

    strike: float  # USD
    expiry: datetime  # UTC
    mark_iv: float  # annualized (e.g., 0.65 = 65%)
    moneyness: float = 0.0  # K / S0 at time of fetch


@dataclass
class VolSurface:
    """Manages IV data for a single asset.

    Usage:
        vs = VolSurface("btc")
        sigma = vs.get_iv(spot=65_000, strike=75_000, expiry=dt)

    Deribit credentials are read from env vars DERIBIT_CLIENT_ID /
    DERIBIT_CLIENT_SECRET. If absent, public (unauthenticated) requests
    are used — which still work, but at lower rate limits.
    """

    asset: str  # "btc", "eth", "sol"
    _iv_points: list[IVPoint] = field(default_factory=list, repr=False)
    _last_deribit_fetch: float = field(default=0.0, repr=False)
    _realized_vol_cache: dict[str, float] = field(default_factory=dict, repr=False)
    _session: requests.Session = field(default_factory=requests.Session, repr=False)
    _access_token: str | None = field(default=None, repr=False)
    _token_fetched_at: float = field(default=0.0, repr=False)

    def get_iv(self, spot: float, strike: float, expiry: datetime) -> float:
        """Return annualized implied vol for (strike, expiry).

        Refreshes Deribit data if stale. Falls back to realized vol if
        Deribit data is unavailable or no points bracket the request.
        """
        self._maybe_refresh_deribit(spot)
        if self._iv_points:
            iv = self._interpolate(spot, strike, expiry)
            if iv is not None:
                return iv
        # Fallback
        rv = self._get_realized_vol()
        logger.info(
            "Using realized vol fallback for %s: σ=%.3f (no Deribit data for K=%.0f)",
            self.asset.upper(),
            rv,
            strike,
        )
        return rv

    # ------------------------------------------------------------------
    # Deribit authentication
    # ------------------------------------------------------------------

    def _get_auth_header(self) -> dict[str, str]:
        """Return Authorization header if credentials available, else empty dict."""
        client_id = os.getenv("DERIBIT_CLIENT_ID", "")
        client_secret = os.getenv("DERIBIT_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            return {}

        # Refresh token if missing or near-expired
        age = time.time() - self._token_fetched_at
        if self._access_token is None or age > _TOKEN_EXPIRY_SECS:
            token = self._fetch_access_token(client_id, client_secret)
            if token:
                self._access_token = token
                self._token_fetched_at = time.time()

        if self._access_token:
            return {"Authorization": f"Bearer {self._access_token}"}
        return {}

    def _fetch_access_token(self, client_id: str, client_secret: str) -> str | None:
        """Exchange client_id/secret for a short-lived access token."""
        try:
            resp = self._session.get(
                f"{DERIBIT_API_BASE}/auth",
                params={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=10,
            )
            resp.raise_for_status()
            token = resp.json().get("result", {}).get("access_token")
            if token:
                logger.info("Deribit: authenticated as %s", client_id)
            return token
        except Exception as exc:
            logger.warning("Deribit auth failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Deribit fetch + interpolation
    # ------------------------------------------------------------------

    def _maybe_refresh_deribit(self, spot: float) -> None:
        age = time.time() - self._last_deribit_fetch
        if age < _DERIBIT_REFRESH_SECS and self._iv_points:
            return
        ccy = _ASSET_TO_DERIBIT_CCY.get(self.asset)
        if ccy is None:
            return
        try:
            resp = self._session.get(
                f"{DERIBIT_API_BASE}/get_book_summary_by_currency",
                params={"currency": ccy, "kind": "option"},
                headers=self._get_auth_header(),
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("result", [])
            points: list[IVPoint] = []
            for item in data:
                iv = _safe_float(item.get("mark_iv")) or _safe_float(item.get("bid_iv")) or 0.0
                if iv <= 0:
                    continue
                instr = item.get("instrument_name", "")
                parsed = _parse_deribit_instrument(instr)
                if parsed is None:
                    continue
                strike_d, expiry_d = parsed
                moneyness = strike_d / spot if spot > 0 else 1.0
                points.append(
                    IVPoint(
                        strike=strike_d,
                        expiry=expiry_d,
                        mark_iv=iv / 100.0,  # Deribit returns percent
                        moneyness=moneyness,
                    )
                )
            if points:
                self._iv_points = points
                self._last_deribit_fetch = time.time()
                logger.info("Deribit: loaded %d IV points for %s", len(points), self.asset.upper())
            else:
                logger.warning("Deribit returned 0 usable IV points for %s", self.asset.upper())
        except Exception as exc:
            logger.warning("Deribit IV fetch failed for %s: %s", self.asset.upper(), exc)

    def _interpolate(self, spot: float, strike: float, expiry: datetime) -> float | None:
        """Log-linear interpolation in moneyness + linear in sqrt(T)."""
        points = self._iv_points
        if not points:
            return None

        target_moneyness = strike / spot if spot > 0 else 1.0
        target_expiry_ts = expiry.timestamp()
        now_ts = datetime.now(UTC).timestamp()
        target_T = max((target_expiry_ts - now_ts) / (365 * 86400), 1e-6)
        target_sqrtT = np.sqrt(target_T)

        # Group points by expiry bucket (within 3-day tolerance)
        expiry_buckets: dict[float, list[IVPoint]] = {}
        for p in points:
            T_p = max((p.expiry.timestamp() - now_ts) / (365 * 86400), 1e-6)
            sqrtT_p = np.sqrt(T_p)
            # Round to nearest 0.01 in sqrt(T) space for bucketing
            bucket = round(sqrtT_p, 2)
            expiry_buckets.setdefault(bucket, []).append(p)

        if not expiry_buckets:
            return None

        sqrtT_keys = sorted(expiry_buckets.keys())

        # Find the two bracketing expiry buckets
        below = [k for k in sqrtT_keys if k <= target_sqrtT]
        above = [k for k in sqrtT_keys if k > target_sqrtT]

        if not below and not above:
            return None

        def _strike_interp(pts: list[IVPoint], target_m: float) -> float | None:
            """Log-linear interpolation in moneyness."""
            if not pts:
                return None
            # Sort by moneyness
            pts_sorted = sorted(pts, key=lambda p: p.moneyness)
            ms = [p.moneyness for p in pts_sorted]
            ivs = [p.mark_iv for p in pts_sorted]

            if target_m <= ms[0]:
                return ivs[0]
            if target_m >= ms[-1]:
                return ivs[-1]

            for i in range(len(ms) - 1):
                if ms[i] <= target_m <= ms[i + 1]:
                    # Log-linear in moneyness
                    log_m0, log_m1 = np.log(ms[i]), np.log(ms[i + 1])
                    log_target = np.log(target_m)
                    if log_m1 == log_m0:
                        return (ivs[i] + ivs[i + 1]) / 2.0
                    w = (log_target - log_m0) / (log_m1 - log_m0)
                    return ivs[i] + w * (ivs[i + 1] - ivs[i])
            return None

        iv_at_T: dict[float, float] = {}
        for k in below[-1:] + above[:1]:
            v = _strike_interp(expiry_buckets[k], target_moneyness)
            if v is not None:
                iv_at_T[k] = v

        if not iv_at_T:
            return None
        if len(iv_at_T) == 1:
            return list(iv_at_T.values())[0]

        # Linear interpolation in sqrt(T)
        k0, k1 = sorted(iv_at_T.keys())
        if k1 == k0:
            return iv_at_T[k0]
        w = (target_sqrtT - k0) / (k1 - k0)
        w = max(0.0, min(1.0, w))
        return iv_at_T[k0] + w * (iv_at_T[k1] - iv_at_T[k0])

    # ------------------------------------------------------------------
    # Realized vol fallback
    # ------------------------------------------------------------------

    def _get_realized_vol(self) -> float:
        """30-day realized vol from Binance daily closes."""
        cache_key = f"{self.asset}_{datetime.now(UTC).date()}"
        if cache_key in self._realized_vol_cache:
            return self._realized_vol_cache[cache_key]

        symbol = _ASSET_TO_SYMBOL.get(self.asset)
        if symbol is None:
            return 0.80  # wide fallback

        try:
            from turtlequant.data.binance import fetch_klines

            end_ms = int(datetime.now(UTC).timestamp() * 1000)
            start_ms = end_ms - _REALIZED_LOOKBACK_DAYS * 86_400_000
            df = fetch_klines(symbol, "1d", start_ms, end_ms)
            if df.empty:
                return 0.80
            closes = df["close"].astype(float).values
            if len(closes) < 5:
                return 0.80
            log_returns = np.log(closes[1:] / closes[:-1])
            rv = float(np.std(log_returns) * np.sqrt(365))
            self._realized_vol_cache[cache_key] = rv
            logger.info("Realized vol for %s: %.3f (30d)", self.asset.upper(), rv)
            return rv
        except Exception as exc:
            logger.warning("Realized vol fetch failed for %s: %s", self.asset.upper(), exc)
            return 0.80


# ---------------------------------------------------------------------------
# Deribit instrument name parser
# ---------------------------------------------------------------------------


def _parse_deribit_instrument(name: str) -> tuple[float, datetime] | None:
    """Parse 'BTC-30MAR25-75000-C' → (75000.0, datetime(2025, 3, 30, UTC)).

    Handles calls and puts (we only care about strike + expiry).
    """
    # Format: ASSET-DDMMMYY-STRIKE-TYPE
    # e.g.: BTC-30MAR25-75000-C
    parts = name.split("-")
    if len(parts) != 4:
        return None
    _asset, date_str, strike_str, _option_type = parts

    # Parse strike
    try:
        strike = float(strike_str)
    except ValueError:
        return None

    # Parse date: DDMMMYY (e.g., "30MAR25")
    try:
        # Try full year first (if Deribit ever uses DDMMMYYYY)
        for fmt in ("%d%b%y", "%d%b%Y"):
            try:
                dt = datetime.strptime(date_str.upper(), fmt)
                # Deribit options expire at 08:00 UTC
                dt = dt.replace(hour=8, tzinfo=UTC)
                return strike, dt
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _safe_float(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
        return f if not (f != f) else None  # NaN check
    except (TypeError, ValueError):
        return None
