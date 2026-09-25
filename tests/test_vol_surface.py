from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from turtlequant.vol_surface import IVPoint, VolSurface
from turtlequant.vol_surface import _redact_url_secret


def test_redact_deribit_client_secret_from_error_url():
    message = (
        "523 Server Error for url: "
        "https://www.deribit.com/api/v2/public/auth?grant_type=client_credentials"
        "&client_id=abc&client_secret=supersecret"
    )

    assert "supersecret" not in _redact_url_secret(message)
    assert "client_secret=<redacted>" in _redact_url_secret(message)


def test_get_iv_records_wide_fallback_source_for_unsupported_asset():
    surface = VolSurface(asset="doge")

    sigma = surface.get_iv(
        spot=1.0,
        strike=1.2,
        expiry=datetime(2026, 12, 31, tzinfo=UTC),
    )

    assert sigma == 0.80
    assert surface.last_source == "wide_fallback"


def test_interpolate_uses_otm_wing_and_total_variance():
    now = datetime.now(UTC)
    surface = VolSurface(asset="btc")
    surface._iv_points = [
        IVPoint(80, now + timedelta(days=20), 0.90, "C", moneyness=0.8),
        IVPoint(100, now + timedelta(days=20), 0.20, "C", moneyness=1.0),
        IVPoint(100, now + timedelta(days=20), 0.90, "P", moneyness=1.0),
        IVPoint(100, now + timedelta(days=60), 0.40, "C", moneyness=1.0),
        IVPoint(100, now + timedelta(days=60), 0.90, "P", moneyness=1.0),
    ]

    iv = surface._interpolate(100, 100, now + timedelta(days=40))
    wing_iv = surface._interpolate(100, 90, now + timedelta(days=20))

    assert iv == pytest.approx((0.13) ** 0.5, rel=1e-4)
    assert wing_iv == pytest.approx(0.20)


def test_deribit_auth_sends_secret_in_post_body_not_url():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"access_token": "tok"}}

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse()

        def get(self, url, **kwargs):  # pragma: no cover - must not be used for auth
            raise AssertionError("auth must not use GET")

    session = FakeSession()
    surface = VolSurface(asset="btc", _session=session)

    assert surface._fetch_access_token("cid", "supersecret") == "tok"
    (url, kwargs), = session.calls
    assert "supersecret" not in url
    assert kwargs["json"]["method"] == "public/auth"
    assert kwargs["json"]["params"]["client_secret"] == "supersecret"


def _smile_surface():
    now = datetime.now(UTC)
    surface = VolSurface(asset="btc")
    expiry = now + timedelta(days=30)
    # A put-skewed smile around a 101 forward: IV falls as strike rises.
    surface._iv_points = [
        IVPoint(80, expiry, 0.80, "P", forward=101.0),
        IVPoint(90, expiry, 0.70, "P", forward=101.0),
        IVPoint(110, expiry, 0.55, "C", forward=101.0),
        IVPoint(120, expiry, 0.50, "C", forward=101.0),
        IVPoint(100, expiry, 0.10, "C", forward=101.0),  # ITM call: not on the OTM wing
    ]
    surface._forwards = {expiry.timestamp(): 101.0}
    surface._last_deribit_fetch = surface._last_deribit_attempt = time.time()
    return surface, expiry


def test_smile_lookup_uses_forward_moneyness_and_reports_slope():
    surface, expiry = _smile_surface()

    sigma, slope, forward = surface.smile(100.0, 100.0, expiry)

    assert forward == pytest.approx(100.0 * (101.0 / 100.0) ** 1, rel=1e-3)
    assert 0.55 < sigma < 0.70  # between the 90 put and 110 call, ignoring the ITM call
    assert slope < 0  # put skew


def test_forward_extrapolates_carry_beyond_last_expiry():
    surface, expiry = _smile_surface()
    later = expiry + timedelta(days=30)

    assert surface.forward(100.0, later) == pytest.approx(100.0 * 1.01**2, rel=1e-2)


def test_stale_surface_is_not_used_when_max_age_is_set():
    surface, expiry = _smile_surface()
    surface._last_deribit_fetch = time.time() - 3600
    surface._last_deribit_attempt = time.time()  # no refetch in the test
    surface._realized_vol_cache[f"btc_{datetime.now(UTC).date()}"] = 0.45

    assert surface.get_iv(100.0, 100.0, expiry) != 0.45  # no limit: legacy uses old points
    assert surface.last_source == "deribit"
    surface.max_age_secs = 900
    assert surface.get_iv(100.0, 100.0, expiry) == 0.45
    assert surface.last_source == "stale"
    assert surface.smile(100.0, 100.0, expiry) is None
