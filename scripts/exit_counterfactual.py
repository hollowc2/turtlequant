#!/usr/bin/env python3
"""Would holding to resolution have beaten the bot's exits? (read-only)

For every sale the bot made before resolution (close / partial_close with
reason edge_reversed, edge_decayed, time_cleanup, ev_exit, ...), compare the
sale proceeds net of the taker fee with what the same shares paid at
resolution (Gamma outcomePrices of the token held). Positive "hold - sell"
means the exit gave value away; this is the evidence for --exit-rule ev.

Usage:
    uv run python scripts/exit_counterfactual.py --state-dir /opt/turtlequant/state
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from turtlequant.clob_execution import DEFAULT_CRYPTO_FEE
from turtlequant.history import load_history
from turtlequant.market_scanner import MarketScanner

NOT_EXITS = {"resolved", "broker_recovery"}

# (market_id, token_id, outcome) -> payout of that token, or None if unresolved
Resolver = Callable[[str, str, str], float | None]


@dataclass(frozen=True)
class Counterfactual:
    market_id: str
    reason: str
    outcome: str
    shares: float
    exit_price: float
    exit_fee: float
    payout: float

    @property
    def sold_for(self) -> float:
        return self.shares * self.exit_price - self.exit_fee

    @property
    def hold_value(self) -> float:
        return self.shares * self.payout

    @property
    def hold_minus_sell(self) -> float:
        return self.hold_value - self.sold_for


def counterfactuals(events: Iterable[dict], resolve: Resolver) -> tuple[list[Counterfactual], int]:
    """Counterfactual per pre-resolution sale, and how many are still unresolved."""
    opens: dict[str, dict] = {}
    last_sell: dict[str, dict] = {}
    rows: list[Counterfactual] = []
    unresolved = 0
    for event in events:
        kind, market_id = event.get("event"), str(event.get("market_id", ""))
        if kind == "open":
            opens[market_id] = event
        elif kind == "order" and event.get("side") == "SELL" and event.get("success"):
            last_sell[market_id] = event
        elif kind in ("close", "partial_close") and event.get("reason") not in NOT_EXITS:
            opened = opens.get(market_id, {})
            outcome = str(event.get("outcome") or opened.get("outcome") or "YES")
            token_id = str(opened.get("token_id") or (opened.get("yes_token_id") if outcome == "YES" else "") or "")
            price = float(event.get("yes_price") or 0.0)
            shares = event.get("filled_shares")
            if shares is None and opened.get("yes_price"):
                shares = float(opened.get("size_usd") or 0.0) / float(opened["yes_price"])
            if not shares or price <= 0:
                continue
            order = last_sell.pop(market_id, None)
            fee = (
                float(order["fee_usd"])
                if order and order.get("fee_usd") is not None
                else DEFAULT_CRYPTO_FEE.fee(float(shares), price)
            )
            payout = resolve(market_id, token_id, outcome)
            if payout is None:
                unresolved += 1
                continue
            rows.append(
                Counterfactual(market_id, str(event.get("reason")), outcome, float(shares), price, fee, payout)
            )
    return rows, unresolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare the bot's exits with holding to resolution")
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    events = load_history(args.state_dir)
    if not events:
        print(f"no history in {args.state_dir}", file=sys.stderr)
        return 1
    scanner = MarketScanner()
    cache: dict[tuple[str, str, str], float | None] = {}

    def resolve(market_id: str, token_id: str, outcome: str) -> float | None:
        key = (market_id, token_id, outcome)
        if key not in cache:
            cache[key] = scanner.fetch_resolution(market_id, token_id, outcome)
            time.sleep(0.05)
        return cache[key]

    rows, unresolved = counterfactuals(events, resolve)
    print(f"{len(rows)} exits on resolved markets, {unresolved} on markets not resolved yet\n")
    print(f"{'reason':16s}{'n':>5s}{'sold for':>11s}{'hold value':>12s}{'hold - sell':>13s}{'hold better':>13s}")
    by_reason: dict[str, list[Counterfactual]] = {}
    for row in rows:
        by_reason.setdefault(row.reason, []).append(row)
    for reason, group in sorted(by_reason.items()) + [("ALL", rows)]:
        if not group:
            continue
        better = sum(1 for r in group if r.hold_minus_sell > 0)
        print(
            f"{reason:16s}{len(group):>5d}{sum(r.sold_for for r in group):>11.2f}"
            f"{sum(r.hold_value for r in group):>12.2f}{sum(r.hold_minus_sell for r in group):>+13.2f}"
            f"{better:>8d}/{len(group):<4d}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
