from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from turtlequant.history import MARKS_JSONL, append_history

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_models.py"
_SPEC = importlib.util.spec_from_file_location("evaluate_models", _PATH)
evaluate_models = importlib.util.module_from_spec(_SPEC)
sys.modules["evaluate_models"] = evaluate_models  # dataclasses resolve their module here
_SPEC.loader.exec_module(evaluate_models)


def _snapshot(ts, rows):
    return {"event": "market_marks", "ts": ts.isoformat(), "pricing_model": "legacy", "rows": rows}


def _row(market_id, expiry, bid, ask, pl, ps):
    return {"id": market_id, "a": "btc", "t": "european", "k": 80000, "exp": expiry.isoformat(),
            "bid": bid, "ask": ask, "pl": pl, "ps": ps, "v": 0.6, "src": "deribit"}


def test_marks_are_routed_to_their_own_file_and_scored(tmp_path):
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    expiry = t0 + timedelta(days=2)
    # m1: legacy says 0.60 vs ask 0.41 (buy YES); smile agrees with the market. Resolves NO.
    # m2: both models see a NO edge (bid 0.50 vs model 0.30). Resolves NO.
    append_history(tmp_path, _snapshot(t0, [_row("m1", expiry, 0.39, 0.41, 0.60, 0.41),
                                            _row("m2", expiry, 0.50, 0.52, 0.30, 0.30)]))
    append_history(tmp_path, _snapshot(t0 + timedelta(hours=1), [_row("m1", expiry, 0.30, 0.32, 0.55, 0.30)]))
    assert (tmp_path / MARKS_JSONL).exists()

    observations = evaluate_models.load_observations(tmp_path)
    report = evaluate_models.evaluate(observations, {"m1": 0.0, "m2": 0.0})

    legacy, smile, market = report["legacy"], report["smile"], report["market"]
    assert legacy["observations"] == 3 and legacy["markets"] == 2
    assert legacy["brier"] == pytest.approx((0.60**2 + 0.30**2 + 0.55**2) / 3)
    assert smile["brier"] < legacy["brier"]
    # Legacy buys YES on m1 once (first snapshot) at 0.41 and loses it plus the fee.
    assert legacy["yes_trades"] == 1
    assert legacy["yes_net_per_share"] == pytest.approx(-0.41 - 0.07 * 0.41 * 0.59)
    # Both buy NO on m2 at 1 - 0.50 and win 1.
    assert legacy["no_trades"] == smile["no_trades"] == 1
    assert smile["no_net_per_share"] == pytest.approx(1 - 0.50 - 0.07 * 0.50 * 0.50)
    assert smile["yes_trades"] == 0
    assert market["yes_trades"] == market["no_trades"] == 0


def test_snapshots_after_expiry_and_unresolved_markets_are_ignored(tmp_path):
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    append_history(tmp_path, _snapshot(t0, [_row("m1", t0 - timedelta(hours=1), 0.4, 0.5, 0.9, 0.9),
                                            _row("m2", t0 + timedelta(days=1), 0.4, 0.5, 0.9, None)]))

    report = evaluate_models.evaluate(evaluate_models.load_observations(tmp_path), {"m1": 1.0})

    assert report["legacy"]["observations"] == 0


def test_trader_writes_marks_snapshots_on_its_interval(tmp_path):
    from test_trader import FakeVol, make_trader

    trader = make_trader(tmp_path, vol=FakeVol(smile=(0.6, -0.00002, 84_500.0)), marks_interval_secs=900)
    trader.scan()
    trader.scan()  # within the interval: no second snapshot

    lines = (tmp_path / MARKS_JSONL).read_text().splitlines()
    assert len(lines) == 1
    (row,) = json.loads(lines[0])["rows"]
    assert row["id"] == "m-1" and row["ps"] > row["pl"]
