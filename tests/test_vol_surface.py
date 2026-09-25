from __future__ import annotations

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
        IVPoint(80, now + timedelta(days=20), 0.90, "C"),
        IVPoint(100, now + timedelta(days=20), 0.20, "C"),
        IVPoint(100, now + timedelta(days=20), 0.90, "P"),
        IVPoint(100, now + timedelta(days=60), 0.40, "C"),
        IVPoint(100, now + timedelta(days=60), 0.90, "P"),
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
