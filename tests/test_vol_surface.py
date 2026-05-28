from __future__ import annotations

from turtlequant.vol_surface import _redact_url_secret


def test_redact_deribit_client_secret_from_error_url():
    message = (
        "523 Server Error for url: "
        "https://www.deribit.com/api/v2/public/auth?grant_type=client_credentials"
        "&client_id=abc&client_secret=supersecret"
    )

    assert "supersecret" not in _redact_url_secret(message)
    assert "client_secret=<redacted>" in _redact_url_secret(message)
