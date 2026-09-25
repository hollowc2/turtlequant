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


# Real questions from Gamma's Crypto Prices events (2026-09-24), one per
# template, with what the parser must return: (option_type, asset, strike) or
# None. Covers every template the bot trades and the look-alikes it must not.
_REAL_QUESTIONS = [
    ("Will the price of Bitcoin be above $82,000 on September 25?", ("european", "btc", 82_000)),
    ("Will the price of Bitcoin be greater than $90,000 on September 26?", ("european", "btc", 90_000)),
    ("Bitcoin above 84,200 on September 25, 12AM ET?", ("european", "btc", 84_200)),
    ("Will Bitcoin reach $87,500 in September?", ("barrier", "btc", 87_500)),
    ("Will Bitcoin reach $130,000 by December 31, 2026?", ("barrier", "btc", 130_000)),
    ("Will Bitcoin reach $92,000 September 21-27?", ("barrier", "btc", 92_000)),
    ("Will Bitcoin hit $150k by September 30?", ("barrier", "btc", 150_000)),
    ("Will Ethereum dip to $2,600 in September?", ("barrier_down", "eth", 2_600)),
    ("Will Bitcoin dip to $40,000 by December 31, 2026?", ("barrier_down", "btc", 40_000)),
    ("Will Bitcoin dip to $80,000 September 21-27?", ("barrier_down", "btc", 80_000)),
    ("Will XRP hit $2 by September 30, 2026?", ("barrier", "xrp", 2)),
    # Not a single-threshold price market on a supported asset.
    ("Bitcoin Up or Down on September 25?", None),
    ("Bitcoin Up or Down - September 24, 11PM ET", None),
    ("Ethereum all time high by December 31, 2026?", None),
    ("Will the price of Bitcoin be between $76,000 and $78,000 on September 25?", None),
    ("Will the price of Bitcoin be less than $72,000 on September 25?", None),  # no put template yet
    ("Will Solana hit $60 or $140 first?", None),
    ("Will Bitcoin Dominance hit 70% before 2027?", None),
    ("Will the Bitcoin Volatility Index dip to 30 by September 30?", None),
    ("Will Anthropic flip BTC by December 31?", None),
    ("Will Milady floor price reach 6 ETH before 2027?", None),
    ("Will Ethena dip to $0.04 in September?", None),
    ("Will Total Crypto Market Cap hit 4T in 2026?", None),
    ("Will Bitcoin outperform Gold in 2026?", None),
    ("Will Bitcoin kimchi premium hit 8% in 2026?", None),
]


@pytest.mark.parametrize(("question", "expected"), _REAL_QUESTIONS)
def test_parser_on_real_polymarket_questions(question, expected):
    params = parse_market(question, datetime.now(UTC) + timedelta(days=30))

    if expected is None:
        assert params is None
    else:
        assert params is not None
        assert (params.option_type.value, params.asset, params.strike) == expected
