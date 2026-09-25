from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from turtlequant.market_parser import parse_market, strike_is_plausible


def test_parse_market_scales_k_suffix_strike():
    params = parse_market(
        "Will BTC be above $75k by June 30?", datetime.now(UTC) + timedelta(days=30)
    )

    assert params is not None
    assert params.strike == 75_000.0


def test_parse_market_keeps_plain_strike_value():
    params = parse_market(
        "Will Ethereum dip to $1,500 by December 31, 2026?",
        datetime.now(UTC) + timedelta(days=30),
    )

    assert params is not None
    assert params.strike == 1_500.0


_EXPIRY = datetime.now(UTC) + timedelta(days=60)


@pytest.mark.parametrize(
    "question",
    [
        "Will Bitcoin's market cap be above $2T by December 31?",
        "Will Bitcoin dominance be above 60% on December 31?",
        "Will BTC ETF inflows be above $500M on October 1?",
        "Will the Bitcoin hashrate be above 1,000 EH/s by December?",
        "Will Bitcoin reach $100k or dip to $70k first?",
        "Will Bitcoin dip to $70k before it hits $100k?",
        "Will Bitcoin be between $80,000 and $85,000 on September 30?",
        "Will Solana flip Ethereum by December 31?",
    ],
)
def test_non_price_threshold_questions_are_rejected(question):
    assert parse_market(question, _EXPIRY) is None


@pytest.mark.parametrize(
    ("question", "strike"),
    [
        ("Will Bitcoin reach $1 million by 2030?", 1_000_000.0),
        ("Will Bitcoin hit $1.5M before 2030?", 1_500_000.0),
        ("Will Bitcoin reach $150k by December 31, 2026?", 150_000.0),
        ("Will XRP reach $3.00 in September?", 3.0),
        ("Will the price of Bitcoin be above $82,000 on September 25?", 82_000.0),
        ("Ethereum above 2,600 on September 23, 8PM ET?", 2_600.0),
    ],
)
def test_strike_suffixes_scale_correctly(question, strike):
    params = parse_market(question, _EXPIRY)

    assert params is not None
    assert params.strike == strike


def test_strike_plausibility_band_rejects_misparsed_strikes():
    assert strike_is_plausible(82_000, 84_000)
    assert strike_is_plausible(40_000, 84_000)
    assert not strike_is_plausible(1_000_000, 84_000)
    assert not strike_is_plausible(2, 84_000)
    assert not strike_is_plausible(80_000, 0)


def test_unclassified_corpus_records_each_question_once(tmp_path):
    import json

    import turtlequant.market_parser as market_parser

    corpus = tmp_path / "state" / "unclassified_markets.jsonl"
    corpus.parent.mkdir()
    corpus.write_text(json.dumps({"question": "Will it rain in Paris?", "ts": "old"}) + "\n")
    market_parser.set_corpus_file(corpus)
    try:
        for _ in range(3):
            parse_market("Will it rain in Paris?")
            parse_market("Who wins the 2028 election?")
    finally:
        market_parser.set_corpus_file(None)

    questions = [json.loads(line)["question"] for line in corpus.read_text().splitlines()]
    assert questions == ["Will it rain in Paris?", "Who wins the 2028 election?"]
