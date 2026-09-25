#!/usr/bin/env python3
"""Inspect or resolve TurtleQuant's live order-intent journal.

An intent stays outstanding (and halts entries) when the bot could not
confirm what the broker did with an order: a crash before the broker
answered, a timeout, or an unconfirmed status. Check the order and your
trade history at the broker first. Then:

    python scripts/order_intents.py --state-dir /opt/turtlequant/state/live-state list
    python scripts/order_intents.py --state-dir ... resolve 7 failed "no order at broker"
    python scripts/order_intents.py --state-dir ... resolve 7 cancelled "cancelled unfilled"

A filled order should not be resolved here: restart the bot and let
reconciliation apply the confirmed fill to positions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from turtlequant.order_intents import OrderIntentLedger

LEDGER_FILE = "turtlequant-order-intents.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TurtleQuant order-intent journal")
    parser.add_argument("--state-dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Show outstanding intents")
    resolve = sub.add_parser("resolve", help="Mark an outstanding intent failed or cancelled")
    resolve.add_argument("intent_id", type=int)
    resolve.add_argument("status", choices=("failed", "cancelled"))
    resolve.add_argument("note", help="What you checked at the broker")
    args = parser.parse_args(argv)

    path = args.state_dir / LEDGER_FILE
    if not path.exists():
        print(f"no ledger at {path}", file=sys.stderr)
        return 1
    ledger = OrderIntentLedger(path)
    if args.command == "list":
        outstanding = ledger.outstanding()
        for intent in outstanding:
            print(
                f"{intent.id}\t{intent.status}\t{intent.side}\t{intent.requested:g}\t"
                f"market={intent.market_id}\torder={intent.order_id or '-'}"
            )
        if not outstanding:
            print("no outstanding intents")
        return 0
    try:
        ledger.resolve(args.intent_id, args.status, args.note)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"intent {args.intent_id} -> {args.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
