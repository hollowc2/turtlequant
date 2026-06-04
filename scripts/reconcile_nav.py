#!/usr/bin/env python3
"""Compare TurtleQuant internal NAV to CLOB collateral + open position marks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

DEFAULT_STATE = Path(os.getenv("STATE_DIR", "/opt/turtlequant/state"))


def _clob_balance_usd() -> tuple[float, float]:
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    from turtlequant.clob_execution import _polymarket_env

    private_key, api_key, api_secret, api_passphrase, signature_type, funder = _polymarket_env()
    if not private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY or PRIVATE_KEY required")

    kwargs: dict = {
        "host": os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com"),
        "chain_id": 137,
        "key": private_key,
    }
    if signature_type:
        kwargs["signature_type"] = signature_type
    if funder:
        kwargs["funder"] = funder

    client = ClobClient(**kwargs)
    if api_key and api_secret and api_passphrase:
        from py_clob_client_v2 import ApiCreds

        client.set_api_creds(
            ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
        )
    else:
        client.set_api_creds(client.create_or_derive_api_key())

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)
    client.update_balance_allowance(params)
    raw = client.get_balance_allowance(params)
    bal = float(raw.get("balance", 0)) / 1_000_000
    allowances = raw.get("allowances") or {}
    if isinstance(allowances, dict) and allowances:
        min_alw = min(float(v) for v in allowances.values()) / 1_000_000
    else:
        alw_raw = raw.get("allowance", 0)
        min_alw = float(alw_raw) / 1_000_000 if alw_raw else 0.0
    return bal, min_alw


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile turtlequant NAV vs wallet + marks")
    parser.add_argument(
        "--positions",
        type=Path,
        default=DEFAULT_STATE / "turtlequant-positions.json",
    )
    args = parser.parse_args()

    if not args.positions.is_file():
        print(f"ERROR: positions file not found: {args.positions}")
        return 1

    data = json.loads(args.positions.read_text())
    nav = float(data.get("nav", 0))
    total_pnl = float(data.get("total_pnl", 0))
    positions = data.get("positions") or []

    marks = 0.0
    for pos in positions:
        shares = float(pos.get("token_size") or 0)
        bid = float(pos.get("last_bid") or pos.get("last_yes_price") or 0)
        marks += shares * bid

    try:
        clob_bal, min_alw = _clob_balance_usd()
    except Exception as exc:
        print(f"CLOB balance unavailable: {exc}")
        clob_bal, min_alw = 0.0, 0.0

    external = clob_bal + marks
    drift = nav - external

    print(f"Internal NAV (positions file) : ${nav:,.2f}")
    print(f"  realized P&L field            : ${total_pnl:,.2f}")
    print(f"  open positions              : {len(positions)}")
    print(f"CLOB collateral (pUSD)        : ${clob_bal:,.2f}")
    print(f"CLOB min allowance            : ${min_alw:,.2f}")
    print(f"Open position mark (bid)      : ${marks:,.2f}")
    print(f"External estimate             : ${external:,.2f}")
    print(f"Drift (nav - external)        : ${drift:,.2f}")

    if abs(drift) > max(5.0, 0.05 * nav) and nav > 0:
        print("\nWARNING: drift exceeds 5% of NAV — investigate before trusting internal NAV.")
        return 1
    print("\nOK: NAV within tolerance of CLOB + marks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
