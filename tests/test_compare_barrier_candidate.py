import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from turtlequant.barrier_research import forward_touch_probability
from turtlequant.market_parser import MarketParams, OptionType
from turtlequant.probability_engine import smile_probability

spec = importlib.util.spec_from_file_location(
    "compare_barrier_candidate", Path(__file__).resolve().parents[1] / "scripts/compare_barrier_candidate.py"
)
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


@pytest.mark.parametrize("kind,strike,slope", [
    (OptionType.BARRIER, 120., -.002),
    (OptionType.BARRIER_DOWN, 80., -.004),
])
def test_reconstruction_matches_direct_candidate_with_fixed_inputs(monkeypatch, kind, strike, slope):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr("turtlequant.probability_engine.datetime", Clock)
    p = MarketParams("btc", strike, now + timedelta(days=30), kind)
    e = {"pricing_model":"smile", "option_type":kind.value, "forward":101., "strike":strike,
         "sigma":.6,"dsigma_dk":slope, "expiry":p.expiry.isoformat(),"ts":now.isoformat(),
         "model_prob_smile":smile_probability(p,100.,101.,.6,slope)}
    reconstructed, reason = comparison.reconstruct_touch(e)
    assert reason is None
    assert reconstructed == pytest.approx(forward_touch_probability(p,100.,101.,.6,as_of=now))
    assert comparison.reconstruct_touch(dict(e, model_prob_smile=1.))[1] == "clipped_or_already_touched"
    assert comparison.reconstruct_touch(dict(e, pricing_model="legacy"))[1] == "not_smile_mode_sigma"


def test_captured_down_no_entry_is_blocked_and_missing_data_is_not_a_zero_return():
    e = {"event":"signal_evaluation", "pricing_model":"smile", "option_type":"barrier_down",
         "forward":86687.69867972919,"strike":85000.,"sigma":.342133255517691251,
         "dsigma_dk":-1.9543445011753236e-6,"expiry":"2026-11-01T04:00:00+00:00",
         "ts":"2026-10-05T14:09:30.013671+00:00", "model_prob_smile":.8055670757178809,
         "model_prob_legacy":.8333709982920285,"model_prob":.16662900170797146,
         "market_id":"example","outcome":"NO","ask_edge":.05083067170797145,
         "entry_threshold":.05,"mid_price":.1095,"book_source":"clob"}
    evidence = {"cutoff":"2026-11-01T16:06:00+00:00", "diagnostics":[e],
                "markets":{"example":{"closed":True,"umaResolutionStatus":"resolved",
                                      "outcomes":'["Yes","No"]',"outcomePrices":'["1","0"]'}}}
    r = comparison.compare(evidence)
    assert r["trades"]["baseline"]["count"] == 1
    assert r["trades"]["candidate"]["count"] == 0
    assert r["trades"]["candidate"]["mean_net_per_share_same_fill"] is None
    assert r["baseline_entries_blocked_at_same_time"][0]["candidate_edge_same_fill"] == pytest.approx(.0444942466)
    assert r["trades"]["baseline"]["mean_net_per_share_same_fill"] == pytest.approx(-.11579833)
