from __future__ import annotations

import pytest

import turtlequant.market_parser as market_parser


@pytest.fixture(autouse=True)
def _isolate_unclassified_corpus(tmp_path, monkeypatch):
    """parse_market appends misses to a CWD file; keep tests from writing into the repo."""
    monkeypatch.setattr(market_parser, "_CORPUS_FILE", tmp_path / "unclassified_markets.jsonl")
