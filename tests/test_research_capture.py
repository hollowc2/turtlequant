import gzip
import json
from datetime import UTC, datetime, timedelta

import pytest

from turtlequant.clob_execution import BookLevel, FeeSchedule, OrderBook
from turtlequant.research_capture import MinuteEvidence, ResearchCapture, rule_start, write_gzip

ASOF = datetime(2026,11,1,12,tzinfo=UTC)


def row(mid="m", *, start="2026-11-01T11:57:00Z"):
    return {"id":mid,"a":"btc","t":"barrier_down","k":90.,"exp":"2026-12-01T05:00:00+00:00",
            "valued_at":ASOF.isoformat(),"condition_id":"cond","yes_token_id":"y","no_token_id":"n",
            "pl":.4,"ps":.2,"candidate_p_yes":.3,
            "contract":{"createdAt":start,"description":
              'Any Binance 1 minute candle for BTC/USDT from the creation of this market. Price action before creation will not be considered.'}}


def snapshot(rows):
    return {"ts":ASOF.isoformat(),"rows":rows,"entry_window_hours":[168,2160]}


def test_rule_window_uses_creation_or_ET_month_and_rejects_unknown_sources():
    r = row()
    assert rule_start(r)[0] == datetime(2026,11,1,11,57,tzinfo=UTC)
    r["contract"]["description"] = "Any Binance 1 minute candle for BTC/USDT during the month specified in the title"
    # November starts in EDT even though its end date is in EST.
    assert rule_start(r)[0] == datetime(2026,11,1,4,tzinfo=UTC)
    r["contract"]["description"] = "Any OKX candle"
    assert rule_start(r)[0] is None
    r=row(start="invalid")
    assert rule_start(r) == (None,"invalid_creation_timestamp")


def test_touch_requires_complete_source_coverage_and_handles_partial_creation_candle(tmp_path):
    evidence = MinuteEvidence(tmp_path)
    r=row()
    assert evidence.touch(r,ASOF)["state"] == "unknown"
    start=int(datetime(2026,11,1,11,57,tzinfo=UTC).timestamp()*1000)
    evidence.db.executemany("INSERT INTO candles VALUES (?,?,?,?)",[("btc",start,105,95),("btc",start+60000,104,94)])
    evidence.db.commit()
    assert evidence.touch(r,ASOF)["state"] == "unknown"  # third minute missing
    evidence.db.execute("INSERT INTO candles VALUES (?,?,?,?)",("btc",start+120000,103,93))
    evidence.db.commit()
    result=evidence.touch(r,ASOF)
    assert result["state"] == "no_touch_in_completed_candles" and result["complete_minute_coverage"]
    evidence.db.execute("UPDATE candles SET low=89 WHERE ts=?",(start,))
    evidence.db.commit()
    assert evidence.touch(r,ASOF)["state"] == "confirmed_touched"
    r["contract"]["createdAt"]="2026-11-01T11:57:30Z"
    result=evidence.touch(r,ASOF)
    assert result["state"] == "unknown" and result["partial_start_candle_ambiguous"]
    # Evidence learned later must not leak into the snapshot's valuation time.
    evidence.db.execute("INSERT INTO candles VALUES (?,?,?,?)",("btc",start+180000,100,80))
    evidence.db.commit()
    assert evidence.touch(r,ASOF)["state"] == "unknown"
    assert evidence.touch(r,ASOF+timedelta(minutes=1))["state"] == "confirmed_touched"


def test_backfill_preserves_raw_pages_and_detects_a_missing_minute(tmp_path):
    r=row()
    start=int(datetime(2026,11,1,11,57,tzinfo=UTC).timestamp()*1000)
    def candle(ts):
        return [ts,"100","105","95","101","1",ts+59999]
    class Response:
        def raise_for_status(self): pass
        def json(self): return [candle(start),candle(start+120000)]
    class Session:
        def get(self,*args,**kwargs): return Response()
    evidence=MinuteEvidence(tmp_path,Session())
    result=evidence.update([r],ASOF,pages_per_asset=1)
    assert result["btc"]["first_missing_ms"] == start+60000
    assert evidence.touch(r,ASOF)["state"] == "unknown"
    raw=list(tmp_path.glob("binance-raw-*.json.gz"))
    assert len(raw)==1
    with gzip.open(raw[0],"rt") as h:
        saved=json.load(h)
    assert saved["candles"]==Response().json() and len(saved["payload_sha256"])==64


def test_sampling_captures_both_real_token_books_without_consensus_filter_or_orders(tmp_path):
    # Avoid creating a worker in a deterministic single-threaded test.
    capture=object.__new__(ResearchCapture)
    capture.max_books=1
    capture.book_cursor=capture.dropped=0
    capture.code_hashes={"test":"hash"}
    class Reader:
        def __init__(self): self.tokens=[]
        def get_order_book(self,token):
            self.tokens.append(token)
            return OrderBook(token,bids=[BookLevel(.2,10)],asks=[BookLevel(.3,20)])
        def get_market_fee(self,*args): return FeeSchedule(.07)
        def buy_yes(self,*args): pytest.fail("research must not trade")
    class Minutes:
        def update(self,*args): return {}
        def touch(self,*args): return {"state":"unknown"}
    reader=Reader()
    source=snapshot([row("a"),row("b")])  # legacy and smile disagree; still eligible
    one=capture.enrich(source,reader,Minutes())
    two=capture.enrich(source,reader,Minutes())
    assert reader.tokens==["y","n","y","n"]
    assert one["rows"][0]["book_capture"]=="attempted"
    assert one["rows"][1]["books"] is None
    assert two["rows"][1]["book_capture"]=="attempted"
    assert "books" not in source["rows"][0]  # original marks are immutable
    assert one["book_sampling"]["eligible"]==2


def test_book_errors_and_unknown_fees_are_preserved(tmp_path):
    capture=object.__new__(ResearchCapture)
    capture.max_books=20
    capture.book_cursor=capture.dropped=0
    capture.code_hashes={}
    class Reader:
        def get_order_book(self,token):
            if token=="y":
                raise OSError("unavailable")
            return OrderBook(token,source="synthetic")
        def get_market_fee(self,*args): return None
    class Minutes:
        def update(self,*args): return {}
        def touch(self,*args): return {"state":"unknown"}
    books=capture.enrich(snapshot([row()]),Reader(),Minutes())["rows"][0]["books"]
    assert books["YES"]["status"]=="error"
    assert books["NO"]["status"]=="unavailable" and books["NO"]["fee"] is None


def test_compressed_evidence_is_atomic_and_never_replaced(tmp_path):
    path=tmp_path/"snapshot.json.gz"
    write_gzip(path,{"original":True})
    original=path.read_bytes()
    write_gzip(path,{"original":False})
    assert path.read_bytes()==original
    assert not list(tmp_path.glob("*.tmp"))


def test_marks_include_replay_inputs_and_capture_failure_does_not_change_trades(tmp_path):
    from test_trader import FakeVol, make_trader, events
    from turtlequant.history import MARKS_JSONL
    def fail(_snapshot): raise RuntimeError("capture unavailable")
    trader=make_trader(tmp_path,vol=FakeVol(smile=(.5,-.000001,84500.)))
    trader.research_submit=fail
    result=trader.scan()
    saved=events(tmp_path,MARKS_JSONL)[0]
    r=saved["rows"][0]
    assert saved["schema_version"]==2 and r["spot"]==84000
    assert r["sigma_legacy"]==.6 and r["sigma_smile"]==.5
    assert r["forward"]==84500 and r["dsigma_dk"]==-.000001 and r["valued_at"]
    assert r["candidate_p_yes"] is None  # European market: not a touch candidate
    assert result["research_submit_errors"]==1 and result["market_errors"]==0
    assert trader.positions.has_position("m-1")
