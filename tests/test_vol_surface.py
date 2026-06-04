from __future__ import annotations

from datetime import UTC, datetime

from turtlequant.vol_surface import VolSurface
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
