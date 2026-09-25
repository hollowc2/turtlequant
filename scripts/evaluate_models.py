#!/usr/bin/env python3
"""Score the legacy and smile models (and the market) against resolutions.

Reads the bot's ``market_marks`` snapshots (turtlequant-marks.jsonl and its
rotations), looks up each expired market's resolution on Gamma (read-only),
and reports, for p_legacy, p_smile and the market mid:

* Brier score and a reliability table (predicted vs realised by decile);
* the trading hypothesis itself: markets where "model - ask >= threshold"
  (buy YES) or "(1 - model) - (1 - bid) >= threshold" (buy NO), taking the
  first qualifying snapshot per market, and what that trade earned per share
  after the taker fee.

The calibration script (calibrate_turtlequant.py) scores a realized-vol
model on simulated contracts; this scores the models that trade, on the
markets they trade, against real prices.

Usage:
    uv run python scripts/evaluate_models.py --state-dir /opt/turtlequant/state
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from turtlequant.clob_execution import DEFAULT_CRYPTO_FEE
from turtlequant.history import MARKS_JSONL, rotated_paths
from turtlequant.market_scanner import MarketScanner

PREDICTORS = ("legacy", "smile", "market")


@dataclass(frozen=True)
class Observation:
    market_id: str
    ts: datetime
    expiry: datetime
    option_type: str
    bid: float
    ask: float
    legacy: float
    smile: float | None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else self.bid or self.ask

    def prob(self, predictor: str) -> float | None:
        return {"legacy": self.legacy, "smile": self.smile, "market": self.mid}[predictor]


def load_observations(state_dir: Path) -> list[Observation]:
    observations = []
    for path in rotated_paths(state_dir, MARKS_JSONL):
        with path.open() as handle:
            for line in handle:
                try:
                    snapshot = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn final line
                ts = datetime.fromisoformat(snapshot["ts"])
                for row in snapshot.get("rows", []):
                    observations.append(
                        Observation(
                            market_id=str(row["id"]),
                            ts=ts,
                            expiry=datetime.fromisoformat(row["exp"]),
                            option_type=str(row["t"]),
                            bid=float(row.get("bid") or 0.0),
                            ask=float(row.get("ask") or 0.0),
                            legacy=float(row["pl"]),
                            smile=None if row.get("ps") is None else float(row["ps"]),
                        )
                    )
    return observations


def _brier(pairs: list[tuple[float, float]]) -> float | None:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def _reliability(pairs: list[tuple[float, float]]) -> list[tuple[str, int, float, float]]:
    table = []
    for lo in range(10):
        bucket = [(p, y) for p, y in pairs if lo / 10 <= p < (lo + 1) / 10 or (lo == 9 and p == 1.0)]
        if bucket:
            table.append((
                f"{lo / 10:.1f}-{(lo + 1) / 10:.1f}",
                len(bucket),
                sum(p for p, _ in bucket) / len(bucket),
                sum(y for _, y in bucket) / len(bucket),
            ))
    return table


def evaluate(
    observations: Iterable[Observation], resolutions: dict[str, float], *, threshold: float = 0.05
) -> dict[str, dict[str, object]]:
    """Scores per predictor over observations of resolved markets."""
    resolved = sorted(
        (o for o in observations if o.market_id in resolutions and o.ts < o.expiry), key=lambda o: o.ts
    )
    report: dict[str, dict[str, object]] = {}
    for predictor in PREDICTORS:
        pairs = [(o.prob(predictor), resolutions[o.market_id]) for o in resolved if o.prob(predictor) is not None]
        trades: dict[str, tuple[str, float]] = {}  # first qualifying snapshot per market
        for o in resolved:
            p = o.prob(predictor)
            if predictor == "market" or p is None or o.market_id in trades:
                continue
            payout = resolutions[o.market_id]
            if 0.02 < o.ask < 0.98 and p - o.ask >= threshold:
                side, price, value = "yes", o.ask, payout
            elif 0.02 < o.bid < 0.98 and (1 - p) - (1 - o.bid) >= threshold:
                side, price, value = "no", 1 - o.bid, 1 - payout  # buy NO at 1 - bid
            else:
                continue
            trades[o.market_id] = (side, value - price - DEFAULT_CRYPTO_FEE.fee(1.0, price))
        yes = [net for side, net in trades.values() if side == "yes"]
        no = [net for side, net in trades.values() if side == "no"]
        report[predictor] = {
            "observations": len(pairs),
            "markets": len({o.market_id for o in resolved}),
            "brier": _brier(pairs),
            "reliability": _reliability(pairs),
            "yes_trades": len(yes),
            "yes_net_per_share": sum(yes) / len(yes) if yes else None,
            "no_trades": len(no),
            "no_net_per_share": sum(no) / len(no) if no else None,
        }
    return report


def fetch_resolutions(
    market_ids: Iterable[str], fetch: Callable[[str], float | None], *, pause: float = 0.05
) -> dict[str, float]:
    out = {}
    for market_id in market_ids:
        value = fetch(market_id)
        if value is not None:
            out[market_id] = value
        time.sleep(pause)
    return out


def _fmt(value: object) -> str:
    return "—" if value is None else f"{value:+.4f}" if isinstance(value, float) else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score TurtleQuant's models against resolutions")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.05)
    args = parser.parse_args(argv)

    observations = load_observations(args.state_dir)
    if not observations:
        print(f"no market_marks snapshots in {args.state_dir}", file=sys.stderr)
        return 1
    now = datetime.now(UTC)
    expired = sorted({o.market_id for o in observations if o.expiry < now})
    scanner = MarketScanner()
    resolutions = fetch_resolutions(expired, scanner.fetch_resolution)
    print(f"{len(observations)} observations, {len(expired)} expired markets, {len(resolutions)} resolved")

    report = evaluate(observations, resolutions, threshold=args.threshold)
    print(f"\n{'model':8s}{'obs':>7s}{'brier':>9s}{'YES n':>7s}{'YES net/sh':>12s}{'NO n':>6s}{'NO net/sh':>11s}")
    for name, r in report.items():
        brier = "—" if r["brier"] is None else f"{r['brier']:.4f}"
        print(
            f"{name:8s}{r['observations']:>7d}{brier:>9s}{r['yes_trades']:>7d}"
            f"{_fmt(r['yes_net_per_share']):>12s}{r['no_trades']:>6d}{_fmt(r['no_net_per_share']):>11s}"
        )
    for name, r in report.items():
        print(f"\nreliability ({name}): bucket, n, mean predicted, realised")
        for bucket, n, predicted, realised in r["reliability"]:
            print(f"  {bucket}  {n:6d}  {predicted:.3f}  {realised:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
