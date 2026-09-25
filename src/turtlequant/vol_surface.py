"""Vol surface — Deribit IV fetcher + strike/expiry interpolation + realized vol fallback.

Primary: Deribit mark_iv per instrument, interpolated to (strike, expiry).
Fallback: 30-day realized vol from Binance daily closes (always available).

Deribit IV refresh: every 5 minutes (stays well within rate limits).
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import requests

from turtlequant.http import REQUEST_TIMEOUT, retrying_session

logger = logging.getLogger(__name__)

DERIBIT_API_BASE = "https://www.deribit.com/api/v2/public"
DERIBIT_API_PRIVATE = "https://www.deribit.com/api/v2/private"
_DERIBIT_REFRESH_SECS = 300  # 5 minutes
_DERIBIT_FAILURE_RETRY_SECS = 60
_DERIBIT_WARNING_COOLDOWN_SECS = 300
_REALIZED_LOOKBACK_DAYS = 30
_TOKEN_EXPIRY_SECS = 850  # Deribit tokens last ~900s; refresh before expiry

_ASSET_TO_DERIBIT_CCY: dict[str, str] = {
    "btc": "BTC",
    "eth": "ETH",
    "sol": "SOL",
}


@dataclass
class IVPoint:
    """A single implied-vol data point from Deribit."""

    strike: float  # USD
    expiry: datetime  # UTC
    mark_iv: float  # annualized (e.g., 0.65 = 65%)
    option_type: str  # "C" or "P"
    moneyness: float = 0.0  # K / S0 at time of fetch (legacy lookup)
    forward: float = 0.0  # Deribit underlying (futures) price for this expiry


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
    # Deribit points older than this are not used (0 = no limit, the legacy
    # behaviour); get_iv then reports last_source "stale".
    max_age_secs: float = 0.0
    _iv_points: list[IVPoint] = field(default_factory=list, repr=False)
    _last_deribit_fetch: float = field(default=0.0, repr=False)
    _realized_vol_cache: dict[str, float] = field(default_factory=dict, repr=False)
    _session: requests.Session = field(default_factory=retrying_session, repr=False)
    _access_token: str | None = field(default=None, repr=False)
    _token_fetched_at: float = field(default=0.0, repr=False)
    _last_deribit_attempt: float = field(default=0.0, repr=False)
    _last_warning_at: dict[str, float] = field(default_factory=dict, repr=False)
    _forwards: dict[float, float] = field(default_factory=dict, repr=False)  # expiry ts -> futures price
    last_source: str = field(default="unknown", init=False)

    def get_iv(self, spot: float, strike: float, expiry: datetime) -> float:
        """Return annualized implied vol for (strike, expiry).

        Refreshes Deribit data if stale. Falls back to realized vol if
        Deribit data is unavailable or no points bracket the request.
        """
        self._maybe_refresh_deribit(spot)
        stale = self._iv_points and self._is_stale()
        if self._iv_points and not stale:
            iv = self._interpolate(spot, strike, expiry)
            if iv is not None:
                self.last_source = "deribit"
                return iv
        # Fallback
        rv = self._get_realized_vol()
        self.last_source = "stale" if stale else "wide_fallback" if rv == 0.80 else "realized"
        logger.info(
            "Using realized vol fallback for %s: σ=%.3f (no Deribit data for K=%.0f)",
            self.asset.upper(),
            rv,
            strike,
        )
        return rv

    def smile(self, spot: float, strike: float, expiry: datetime) -> tuple[float, float, float] | None:
        """``(sigma, dsigma/dK, forward)`` from the forward-moneyness smile, or None.

        Strikes are stored as-is and moneyness is K/F with each expiry's own
        Deribit futures price, computed at query time (sticky strike), so the
        lookup does not drift with the spot seen when the surface was fetched.
        """
        self._maybe_refresh_deribit(spot)
        if not self._iv_points or self._is_stale():
            return None
        forward = self.forward(spot, expiry)
        sigma = self._interpolate_forward(strike, expiry)
        if forward is None or sigma is None:
            return None
        h = 0.01 * strike
        up, down = self._interpolate_forward(strike + h, expiry), self._interpolate_forward(strike - h, expiry)
        slope = (up - down) / (2 * h) if up is not None and down is not None else 0.0
        return sigma, slope, forward

    def forward(self, spot: float, expiry: datetime) -> float | None:
        """Forward for ``expiry``: log-linear in T through spot and the Deribit futures curve."""
        now = time.time()
        curve = sorted(((ts - now) / (365 * 86400), f) for ts, f in self._forwards.items() if ts > now and f > 0)
        if spot <= 0 or not curve:
            return None
        t = max((expiry.timestamp() - now) / (365 * 86400), 0.0)
        points = [(0.0, spot), *curve]
        for (t0, f0), (t1, f1) in zip(points, points[1:]):
            if t0 <= t <= t1:
                w = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                return float(np.exp((1 - w) * np.log(f0) + w * np.log(f1)))
        t_last, f_last = points[-1]
        return spot * (f_last / spot) ** (t / t_last)  # beyond the last expiry: same carry

    def _is_stale(self) -> bool:
        return self.max_age_secs > 0 and time.time() - self._last_deribit_fetch > self.max_age_secs

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
        """Exchange client_id/secret for a short-lived access token.

        Sent as a JSON-RPC POST body so the secret never appears in a URL
        (proxy/access logs, exception messages).
        """
        try:
            resp = self._session.post(
                f"{DERIBIT_API_BASE}/auth",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "public/auth",
                    "params": {
                        "grant_type": "client_credentials",
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            token = resp.json().get("result", {}).get("access_token")
            if token:
                logger.info("Deribit: authenticated as %s", client_id)
            return token
        except Exception as exc:
            self._log_warning(
                "auth", "Deribit auth failed: %s", _redact_url_secret(str(exc))
            )
            return None

    # ------------------------------------------------------------------
    # Deribit fetch + interpolation
    # ------------------------------------------------------------------

    def _maybe_refresh_deribit(self, spot: float) -> None:
        age = time.time() - self._last_deribit_fetch
        if age < _DERIBIT_REFRESH_SECS and self._iv_points:
            return
        attempt_age = time.time() - self._last_deribit_attempt
        if attempt_age < _DERIBIT_FAILURE_RETRY_SECS:
            return
        ccy = _ASSET_TO_DERIBIT_CCY.get(self.asset)
        if ccy is None:
            return
        self._last_deribit_attempt = time.time()
        try:
            resp = self._get(
                f"{DERIBIT_API_BASE}/get_book_summary_by_currency",
                params={"currency": ccy, "kind": "option"},
                headers=self._get_auth_header(),
            )
            resp.raise_for_status()
            data = resp.json().get("result", [])
            points: list[IVPoint] = []
            forwards: dict[float, float] = {}
            for item in data:
                iv = (
                    _safe_float(item.get("mark_iv"))
                    or _safe_float(item.get("bid_iv"))
                    or 0.0
                )
                if iv <= 0:
                    continue
                instr = item.get("instrument_name", "")
                parsed = _parse_deribit_instrument(instr)
                if parsed is None:
                    continue
                strike_d, expiry_d, option_type = parsed
                moneyness = strike_d / spot if spot > 0 else 1.0
                forward = _safe_float(item.get("underlying_price")) or 0.0
                if forward > 0:
                    forwards[expiry_d.timestamp()] = forward
                points.append(
                    IVPoint(
                        strike=strike_d,
                        expiry=expiry_d,
                        mark_iv=iv / 100.0,  # Deribit returns percent
                        option_type=option_type,
                        moneyness=moneyness,
                        forward=forward,
                    )
                )
            if points:
                self._iv_points = points
                self._forwards = forwards
                self._last_deribit_fetch = time.time()
                logger.info(
                    "Deribit: loaded %d IV points for %s",
                    len(points),
                    self.asset.upper(),
                )
            else:
                self._log_warning(
                    "empty",
                    "Deribit returned 0 usable IV points for %s",
                    self.asset.upper(),
                )
        except Exception as exc:
            self._log_warning(
                "iv", "Deribit IV fetch failed for %s: %s", self.asset.upper(), exc
            )

    def _get(self, url: str, **kwargs: object) -> requests.Response:
        return self._session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)

    def _log_warning(self, key: str, msg: str, *args: object) -> None:
        now = time.time()
        last = self._last_warning_at.get(key, 0.0)
        if now - last >= _DERIBIT_WARNING_COOLDOWN_SECS:
            logger.warning(msg, *args)
            self._last_warning_at[key] = now
        else:
            logger.debug(msg, *args)

    def _interpolate(
        self, spot: float, strike: float, expiry: datetime
    ) -> float | None:
        """Legacy lookup: OTM wing and moneyness relative to spot, frozen at fetch time."""
        buckets: dict[float, list[tuple[float, float]]] = {}
        for p in self._iv_points:
            if (p.strike >= spot and p.option_type == "C") or (p.strike < spot and p.option_type == "P"):
                buckets.setdefault(p.expiry.timestamp(), []).append((p.moneyness, p.mark_iv))
        target = strike / spot if spot > 0 else 1.0
        return _interp_surface(buckets, lambda _bucket: target, expiry)

    def _interpolate_forward(self, strike: float, expiry: datetime) -> float | None:
        """OTM wing and moneyness relative to each expiry's own forward (K/F)."""
        buckets: dict[float, list[tuple[float, float]]] = {}
        forwards: dict[float, float] = {}
        for p in self._iv_points:
            if p.forward <= 0 or (p.strike >= p.forward) != (p.option_type == "C"):
                continue
            key = p.expiry.timestamp()
            buckets.setdefault(key, []).append((p.strike / p.forward, p.mark_iv))
            forwards[key] = p.forward
        return _interp_surface(buckets, lambda bucket: strike / forwards[bucket], expiry)

    # ------------------------------------------------------------------
    # Realized vol fallback
    # ------------------------------------------------------------------

    def _get_realized_vol(self) -> float:
        """30-day realized vol from Binance daily closes."""
        cache_key = f"{self.asset}_{datetime.now(UTC).date()}"
        if cache_key in self._realized_vol_cache:
            return self._realized_vol_cache[cache_key]

        from turtlequant.data.binance import ASSET_TO_SYMBOL

        symbol = ASSET_TO_SYMBOL.get(self.asset)
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
            logger.warning(
                "Realized vol fetch failed for %s: %s", self.asset.upper(), exc
            )
            return 0.80


# ---------------------------------------------------------------------------
# Surface interpolation
# ---------------------------------------------------------------------------


def _interp_smile(points: list[tuple[float, float]], target_m: float) -> float | None:
    """Log-linear interpolation in moneyness; flat beyond the quoted range."""
    if not points:
        return None
    pts = sorted(points)
    ms = [m for m, _ in pts]
    ivs = [iv for _, iv in pts]
    if target_m <= ms[0]:
        return ivs[0]
    if target_m >= ms[-1]:
        return ivs[-1]
    for i in range(len(ms) - 1):
        if ms[i] <= target_m <= ms[i + 1]:
            log_m0, log_m1 = np.log(ms[i]), np.log(ms[i + 1])
            if log_m1 == log_m0:
                return (ivs[i] + ivs[i + 1]) / 2.0
            w = (np.log(target_m) - log_m0) / (log_m1 - log_m0)
            return float(ivs[i] + w * (ivs[i + 1] - ivs[i]))
    return None


def _interp_surface(
    buckets: dict[float, list[tuple[float, float]]],
    target_of: Callable[[float], float],
    expiry: datetime,
) -> float | None:
    """Smile per bracketing expiry, then linear in total variance (sigma^2 T)."""
    if not buckets:
        return None
    target_ts = expiry.timestamp()
    now_ts = datetime.now(UTC).timestamp()
    target_T = max((target_ts - now_ts) / (365 * 86400), 1e-6)
    keys = sorted(buckets)
    below = [k for k in keys if k <= target_ts]
    above = [k for k in keys if k > target_ts]
    iv_at_T: dict[float, float] = {}
    for k in below[-1:] + above[:1]:
        v = _interp_smile(buckets[k], target_of(k))
        if v is not None:
            iv_at_T[k] = v
    if not iv_at_T:
        return None
    if len(iv_at_T) == 1:
        return next(iter(iv_at_T.values()))
    k0, k1 = sorted(iv_at_T)
    T0 = max((k0 - now_ts) / (365 * 86400), 1e-6)
    T1 = max((k1 - now_ts) / (365 * 86400), 1e-6)
    w = max(0.0, min(1.0, (target_T - T0) / (T1 - T0)))
    total_variance = (1.0 - w) * iv_at_T[k0] ** 2 * T0 + w * iv_at_T[k1] ** 2 * T1
    return float(np.sqrt(max(total_variance, 0.0) / target_T))


# ---------------------------------------------------------------------------
# Deribit instrument name parser
# ---------------------------------------------------------------------------


def _parse_deribit_instrument(name: str) -> tuple[float, datetime, str] | None:
    """Parse 'BTC-30MAR25-75000-C' → (75000.0, expiry, "C")."""
    # Format: ASSET-DDMMMYY-STRIKE-TYPE
    # e.g.: BTC-30MAR25-75000-C
    parts = name.split("-")
    if len(parts) != 4:
        return None
    _asset, date_str, strike_str, option_type = parts
    if option_type not in {"C", "P"}:
        return None

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
                return strike, dt, option_type
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


def _redact_url_secret(message: str) -> str:
    """Avoid writing Deribit client_secret query values into logs."""
    return re.sub(r"(client_secret=)[^&\s]+", r"\1<redacted>", message)
