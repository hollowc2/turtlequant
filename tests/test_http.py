from __future__ import annotations

from turtlequant.http import REQUEST_TIMEOUT, retrying_session


def test_retrying_session_uses_bounded_get_retries():
    adapter = retrying_session().get_adapter("https://example.com")
    retry = adapter.max_retries

    assert REQUEST_TIMEOUT == (2.0, 3.0)
    assert retry.total == 2
    assert retry.respect_retry_after_header
    assert 429 in retry.status_forcelist
