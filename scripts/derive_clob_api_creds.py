#!/usr/bin/env python3
"""Print POLYMARKET_API_* env lines derived from PRIVATE_KEY (CLOB v2)."""

from __future__ import annotations

import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> int:
    from py_clob_client_v2 import ClobClient

    private_key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PRIVATE_KEY", "")
    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY or PRIVATE_KEY required", file=sys.stderr)
        return 1

    signature_type = int(
        os.getenv("POLYMARKET_SIGNATURE_TYPE", os.getenv("SIGNATURE_TYPE", "0"))
    )
    funder = ""
    if signature_type:
        funder = os.getenv("POLYMARKET_FUNDER") or os.getenv("FUNDER_ADDRESS", "")

    kwargs: dict = {
        "host": os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com"),
        "chain_id": 137,
        "key": private_key,
    }
    if signature_type:
        kwargs["signature_type"] = signature_type
    if signature_type and funder:
        kwargs["funder"] = funder

    client = ClobClient(**kwargs)
    creds = client.create_or_derive_api_key()
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
