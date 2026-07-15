"""Bounded GET sessions for public market-data APIs."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

REQUEST_TIMEOUT = (2.0, 3.0)
_RETRY = Retry(
    total=2,
    connect=2,
    read=1,
    status=2,
    backoff_factor=0.25,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    respect_retry_after_header=True,
)


def retrying_session() -> requests.Session:
    """Return a GET-only retrying session with server-directed backoff."""
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=_RETRY))
    return session
