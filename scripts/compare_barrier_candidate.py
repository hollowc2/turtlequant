#!/usr/bin/env python3
"""Offline, capture-conditioned comparison of removing terminal touch scaling.

Reads the frozen evidence JSON collected during the Nov 1 investigation.
No network, orders, state changes or bot imports. Outputs JSON to stdout.
Missing historical spot prevents a full candidate replay. Unclipped logged
smile probabilities can instead be algebraically unscaled; this is labeled
reconstruction throughout, with the original recorded fill costs held fixed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from turtlequant.barrier_research import forward_touch_probability
from turtlequant.market_parser import MarketParams, OptionType
from turtlequant.probability_engine import smile_probability, terminal_above_probability


def reconstruct_touch(event: dict) -> tuple[float | None, str | None]:
    """Invert only the unclipped terminal-ratio scaling in smile-mode logs."""
    if event.get("pricing_model") != "smile":
        return None, "not_smile_mode_sigma"
    kind = event.get("option_type")
    if kind not in ("barrier", "barrier_down"):
        return None, "not_barrier"
    required = ("forward", "strike", "sigma", "dsigma_dk", "model_prob_smile", "expiry", "ts")
    if any(event.get(k) is None for k in required):
        return None, "missing_inputs"
    ps = event["model_prob_smile"]
    if not 1e-6 < ps < 1-1e-6:
        return None, "clipped_or_already_touched"
    T = (datetime.fromisoformat(event["expiry"]) - datetime.fromisoformat(event["ts"])).total_seconds() / (365*86400)
    if T <= 0:
        return None, "expired"
    F, K, sigma, slope = (event[k] for k in ("forward", "strike", "sigma", "dsigma_dk"))
    flat = terminal_above_probability(F, K, T, sigma)
    skew = terminal_above_probability(F, K, T, sigma, slope)
    if kind == "barrier_down":
        flat, skew = 1-flat, 1-skew
    if flat <= 1e-9 or skew <= 0:
        return None, "uninvertible_terminal"
    candidate = ps * flat / skew
    if not 1e-6 <= candidate <= 1-1e-6 + 1e-10:
        return None, "inversion_out_of_bounds"
    return min(1-1e-6, candidate), None


def compare(evidence: dict) -> dict:
    resolutions = {}
    for mid, raw in evidence["markets"].items():
        if not raw.get("closed") or str(raw.get("umaResolutionStatus", "")).lower() != "resolved":
            continue
        outcomes = [str(o).lower() for o in json.loads(raw["outcomes"])]
        resolutions[mid] = float(json.loads(raw["outcomePrices"])[outcomes.index("yes")])
    excluded = Counter()
    rows = []
    cutoff = datetime.fromisoformat(evidence["cutoff"])
    for e in evidence["diagnostics"]:
        if e.get("event") != "signal_evaluation":
            continue
        ts, expiry = datetime.fromisoformat(e["ts"]), datetime.fromisoformat(e["expiry"])
        hours = (expiry-ts).total_seconds()/3600
        if ts > cutoff or not 168 <= hours <= 2160:
            excluded["outside_window"] += 1
            continue
        p, reason = reconstruct_touch(e)
        if reason:
            excluded[reason] += 1
            continue
        mid = str(e["market_id"])
        if mid not in resolutions:
            excluded["unresolved"] += 1
            continue
        side = e["outcome"]
        legacy = e["model_prob_legacy"]
        conservative = min(legacy, p) if side == "YES" else 1-max(legacy, p)
        # Recover the original fee-inclusive cost without substituting Gamma
        # quotes or pretending the candidate's Kelly size would be identical.
        cost = e["model_prob"]-e["ask_edge"]
        payout = resolutions[mid] if side == "YES" else 1-resolutions[mid]
        rows.append({"id":mid, "ts":e["ts"], "type":e["option_type"], "side":side,
                     "baseline_p_yes":e["model_prob_smile"], "candidate_p_yes_reconstructed":p,
                     "market_p_yes":e["mid_price"] if side == "YES" else 1-e["mid_price"],
                     "payout_yes":resolutions[mid], "fee_inclusive_cost_same_fill":cost,
                     "baseline_edge":e["ask_edge"], "candidate_edge_same_fill":conservative-cost,
                     "baseline_qualifies":e["ask_edge"] >= e["entry_threshold"],
                     "candidate_qualifies_same_fill":conservative-cost >= e["entry_threshold"],
                     "hypothetical_net_per_share_same_fill":payout-cost,
                     "book_source":e.get("book_source")})
    rows.sort(key=lambda r:r["ts"])
    scores = {}
    for kind in ("all", "barrier", "barrier_down"):
        selected = [r for r in rows if kind == "all" or r["type"] == kind]
        groups = defaultdict(list)
        for r in selected:
            groups[r["id"]].append(r)
        scores[kind] = {"events":len(selected),"markets":len(groups)}
        for name, key in (("baseline","baseline_p_yes"),("candidate","candidate_p_yes_reconstructed"),
                          ("market", "market_p_yes")):
            scores[kind][name+"_brier_event_weighted"] = statistics.mean(
                (r[key]-r["payout_yes"])**2 for r in selected) if selected else None
            scores[kind][name+"_brier_equal_market"] = statistics.mean(
                statistics.mean((r[key]-r["payout_yes"])**2 for r in rr)
                for rr in groups.values()) if groups else None
    trades = {}
    for name, flag in (("baseline","baseline_qualifies"),("candidate","candidate_qualifies_same_fill")):
        first = {}
        for r in rows:
            if r[flag] and r["id"] not in first:
                first[r["id"]] = r
        values = [r["hypothetical_net_per_share_same_fill"] for r in first.values()]
        trades[name] = {"count":len(values), "wins":sum(v>0 for v in values),
                        "mean_net_per_share_same_fill":statistics.mean(values) if values else None,
                        "first_captured_qualifying_events":list(first.values())}
    # Attribution at the baseline entry time, including profitable entries
    # blocked by the candidate. Candidate may qualify later in that market.
    removed = [r for r in trades["baseline"]["first_captured_qualifying_events"]
               if not r["candidate_qualifies_same_fill"]]
    return {"decision":"insufficient evidence", "candidate":"forward_flat_touch_v1",
            "matched_scores":scores, "excluded":dict(excluded), "trades":trades,
            "baseline_entries_blocked_at_same_time":removed,
            "matched_events":rows,
            "limitations":[
                "Development evidence from one shared expiry, selected by the baseline consensus/candidate gates.",
                "Reconstructed probabilities, not full historical replay: marks omit spot, forward and slope.",
                "Recorded side, depth and fee-inclusive cost held fixed; no new sizing, caps, re-entry or exit simulation.",
                "Only captured signal_evaluation events are eligible; alternative-only opportunities are missing.",
                "Event counts are dependent; equal-market scores still share an asset path and expiry.",
                "Clipped/already-touched observations cannot be inverted and are excluded.",
                "No NAV return, drawdown, dollar P&L or confidence interval is inferred from these samples.",
            ]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text())
    result = compare(evidence)
    as_of = datetime(2026,10,5,14,9,30,tzinfo=UTC)
    params = MarketParams("btc",85000.,datetime(2026,11,1,4,tzinfo=UTC),OptionType.BARRIER_DOWN)
    # Fixed historical valuation time for baseline and candidate alike.
    from unittest.mock import patch
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return as_of if tz else as_of.replace(tzinfo=None)
    with patch("turtlequant.probability_engine.datetime", Clock):
        result["boundary_comparison"] = [
            {"spot":s,"baseline":smile_probability(params,s,86687.69867972919,.34213325551769125,-1.9543445011753236e-6),
             "candidate":forward_touch_probability(params,s,86687.69867972919,.34213325551769125,as_of=as_of)}
            for s in (85000.01,85000.000001,85000.)
        ]
    paths = [Path(__file__),Path("src/turtlequant/barrier_research.py"),Path("src/turtlequant/probability_engine.py")]
    result["provenance"] = {"evidence":str(args.evidence),
        "evidence_sha256":hashlib.sha256(args.evidence.read_bytes()).hexdigest(),
        "code_sha256":{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        "git_sha":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
        "git_status":subprocess.check_output(["git","status","--short"],text=True),
        "python":sys.version,"cutoff":evidence["cutoff"]}
    print(json.dumps(result,indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
