"""Read-only enrichment of queued market snapshots, independent of trade gates.

One bounded daemon queue, a separate public CLOB client, Binance minute
candles, and compressed append-only research shards. No order methods are
called. Missing data, sampling and failures are explicit evidence fields.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import queue
import shutil
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from turtlequant.clob_execution import ExecutionClient
from turtlequant.http import REQUEST_TIMEOUT

log = logging.getLogger(__name__)
BINANCE_KLINES = "https://data-api.binance.vision/api/v3/klines"
MINUTE = 60_000


def rule_start(row: dict) -> tuple[datetime | None, str]:
    """Only recognise the two Binance monthly rule templates we inspected."""
    contract = row.get("contract", {})
    text = (contract.get("description") or "").lower()
    symbol = {"btc":"btc/usdt", "eth":"eth/usdt"}.get(row["a"])
    if not symbol or "binance" not in text or symbol not in text or "1 minute candle" not in text:
        return None, "unrecognised_resolution_source_or_rules"
    if "from the creation of this market" in text and contract.get("createdAt"):
        try:
            start = datetime.fromisoformat(contract["createdAt"].replace("Z", "+00:00"))
            return (start, "creation") if start.tzinfo else (None, "invalid_creation_timestamp")
        except ValueError:
            return None, "invalid_creation_timestamp"
    if "during the month specified in the title" in text:
        last = datetime.fromisoformat(row["exp"]).astimezone(ZoneInfo("America/New_York"))-timedelta(seconds=1)
        return last.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC), "calendar_month_ET"
    return None, "unrecognised_counting_window"


class MinuteEvidence:
    """Resume bounded backfill from the first missing Binance minute, never a proxy."""
    def __init__(self, directory: Path, session=None):
        self.directory = directory
        self.session = session or requests.Session()  # public GETs, no credentials, no hidden retries
        self.db = sqlite3.connect(directory / "binance-minutes.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS candles (asset TEXT, ts INTEGER, high REAL, low REAL, PRIMARY KEY(asset,ts))")

    def first_missing(self, asset: str, start: int, end: int) -> int:
        cursor = start
        for (ts,) in self.db.execute("SELECT ts FROM candles WHERE asset=? AND ts>=? AND ts<? ORDER BY ts", (asset,start,end)):
            if ts != cursor:
                break
            cursor += MINUTE
        return cursor

    def update(self, rows: list[dict], as_of: datetime, pages_per_asset=3) -> dict:
        end = int(as_of.timestamp()*1000)//MINUTE*MINUTE  # only completed final candles
        starts = {}
        for row in rows:
            if row["t"] not in ("barrier", "barrier_down"):
                continue
            start, _ = rule_start(row)
            if start is not None and start < as_of:
                starts[row["a"]] = min(starts.get(row["a"],start),start)
        status = {}
        for asset, start_dt in starts.items():
            start = int(start_dt.timestamp()*1000)//MINUTE*MINUTE
            fetched = 0
            try:
                for _ in range(pages_per_asset):
                    cursor = self.first_missing(asset,start,end)
                    if cursor >= end:
                        break
                    response = self.session.get(BINANCE_KLINES, params={
                        "symbol":asset.upper()+"USDT", "interval":"1m", "startTime":cursor,
                        "endTime":end-1,"limit":1000,
                    },timeout=REQUEST_TIMEOUT)
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload,list) or not payload:
                        raise ValueError("empty or malformed Binance page")
                    validated = []
                    previous = -1
                    for candle in payload:
                        ts, close_ts = int(candle[0]), int(candle[6])
                        op, hi, lo, cl = map(float,candle[1:5])
                        if (ts % MINUTE or ts <= previous or ts < cursor or ts >= end or close_ts != ts+MINUTE-1
                                or not all(math.isfinite(v) and v>0 for v in (op,hi,lo,cl))
                                or not lo <= min(op,cl) <= max(op,cl) <= hi):
                            raise ValueError("invalid Binance OHLC or minute boundaries")
                        previous = ts
                        validated.append((asset,ts,hi,lo))
                    raw = {"provider":BINANCE_KLINES,"asset":asset,"retrieved_at":datetime.now(UTC).isoformat(),
                           "requested_start_ms":cursor,"requested_end_ms":end-1,"candles":payload}
                    raw["payload_sha256"] = hashlib.sha256(json.dumps(payload,separators=(",",":")).encode()).hexdigest()
                    write_gzip(self.directory / f"binance-raw-{asset}-{cursor}-{raw['payload_sha256'][:16]}.json.gz",raw)
                    self.db.executemany("INSERT OR IGNORE INTO candles VALUES (?,?,?,?)",validated)
                    self.db.commit()
                    fetched += len(validated)
                    if self.first_missing(asset,start,end) == cursor:
                        raise ValueError("Binance page did not fill the earliest gap")
                status[asset] = {"fetched":fetched,"first_missing_ms":self.first_missing(asset,start,end),"required_end_ms":end}
            except Exception as exc:
                status[asset] = {"fetched":fetched,"error":type(exc).__name__,"coverage":"unknown_or_partial"}
        return status

    def touch(self, row: dict, as_of: datetime) -> dict:
        start, reason = rule_start(row)
        result = {"state":"unknown","rule_window":reason,"source":BINANCE_KLINES,"interval":"1m",
                  "window_start":start.isoformat() if start else None,"as_of":as_of.isoformat()}
        if row["t"] not in ("barrier", "barrier_down"):
            result["state"] = "not_applicable"
            return result
        if start is None or start >= as_of:
            return result
        start_ms = int(start.timestamp()*1000)
        first = start_ms//MINUTE*MINUTE
        end = int(as_of.timestamp()*1000)//MINUTE*MINUTE
        result["verified_through_exclusive"] = datetime.fromtimestamp(end/1000,UTC).isoformat()
        candles = list(self.db.execute("SELECT ts,high,low FROM candles WHERE asset=? AND ts>=? AND ts<? ORDER BY ts", (row["a"],first,end)))
        # A partial first candle can have touched before market creation; never
        # call that a confirmed touch. A partial current candle is also unknown.
        hits = [ts for ts,hi,lo in candles if ts>=start_ms and
                (hi>=row["k"] if row["t"]=="barrier" else lo<=row["k"])]
        if hits:
            result.update(state="confirmed_touched",witness_minute_open_ms=min(hits))
            return result
        complete = len(candles)==max(0,(end-first)//MINUTE) and self.first_missing(row["a"],first,end)>=end
        partial_start_hit = any(ts<start_ms and (hi>=row["k"] if row["t"]=="barrier" else lo<=row["k"]) for ts,hi,lo in candles)
        if candles and complete and not partial_start_hit:
            result["state"] = "no_touch_in_completed_candles"
        result["complete_minute_coverage"] = complete
        result["partial_start_candle_ambiguous"] = partial_start_hit
        result["current_incomplete_minute_unknown"] = end < int(as_of.timestamp()*1000)
        return result


def write_gzip(path: Path, value: dict) -> None:
    """One atomic compressed object per file; crashes cannot tear earlier evidence."""
    if path.exists():
        return
    temp = path.with_suffix(path.suffix+".tmp")
    with gzip.open(temp,"wt",encoding="utf-8") as handle:
        handle.write(json.dumps(value,separators=(",",":"),allow_nan=False)+"\n")
    temp.replace(path)


class ResearchCapture:
    """Worker accepts snapshots without waiting for network or touching trading state."""
    def __init__(self, state_dir: Path, *, max_books=20, max_bytes=2*1024**3):
        self.directory = state_dir / "research"
        self.directory.mkdir(parents=True,exist_ok=True)
        self.max_books, self.max_bytes = max_books, max_bytes
        self.pending = queue.Queue(maxsize=1)
        self.dropped = 0
        self.completed = 0
        self.book_cursor = 0
        self.run_started_at = datetime.now(UTC).isoformat()
        source_dir = Path(__file__).resolve().parent
        self.code_hashes = {name:hashlib.sha256((source_dir/name).read_bytes()).hexdigest() for name in (
            "research_capture.py","trader.py","probability_engine.py","barrier_research.py","vol_surface.py",
        )}
        self.thread = threading.Thread(target=self._run, name="research-capture",daemon=True)
        self.thread.start()

    def submit(self, snapshot: dict) -> None:
        try:
            # Snapshot rows are never mutated after scan. The worker makes a
            # deep copy before enrichment; submit itself does no I/O.
            self.pending.put_nowait(snapshot)
        except queue.Full:
            self.dropped += 1
            log.warning("Research queue full; snapshot dropped (%d total)",self.dropped)

    def write_status(self, **fields) -> None:
        status = {"schema_version":1,"updated_at":datetime.now(UTC).isoformat(),
                  "run_started_at":self.run_started_at,
                  "completed_snapshots":self.completed,"dropped_snapshots":self.dropped,**fields}
        temp = self.directory / "status.tmp"
        temp.write_text(json.dumps(status,indent=2)+"\n")
        temp.replace(self.directory / "status.json")

    def enrich(self, snapshot: dict, reader, minutes: MinuteEvidence) -> dict:
        snapshot = json.loads(json.dumps(snapshot))
        rows = snapshot["rows"]
        as_of = datetime.fromisoformat(snapshot["ts"])
        lo, hi = snapshot["entry_window_hours"]
        eligible = [r for r in rows if (h:=(datetime.fromisoformat(r["exp"])-as_of).total_seconds()/3600)>=lo and (not hi or h<=hi)]
        snapshot["minute_backfill"] = minutes.update(eligible,as_of)
        # Round robin samples every eligible market, not just current-model
        # signals. Keep explicit omissions so sampling never becomes zero fills.
        eligible.sort(key=lambda r:r["id"])
        selected = []
        if eligible:
            offset = self.book_cursor % len(eligible)
            ordered = eligible[offset:]+eligible[:offset]
            selected = ordered[:self.max_books]
            self.book_cursor += len(selected)
        chosen = {r["id"] for r in selected}
        for row in rows:
            row["prior_touch"] = minutes.touch(row,datetime.fromisoformat(row.get("valued_at") or snapshot["ts"]))
            row["books"] = None
            row["book_capture"] = "not_sampled" if row in eligible else "outside_entry_window"
            if row["id"] not in chosen:
                continue
            row["book_capture"] = "attempted"
            row["books"] = {}
            for outcome, key in (("YES","yes_token_id"),("NO","no_token_id")):
                token = row.get(key)
                if not token:
                    row["books"][outcome] = {"status":"missing_token"}
                    continue
                try:
                    book = reader.get_order_book(token)  # no mirrored/synthetic fallback prices
                    fee = reader.get_market_fee(row["condition_id"],token)
                    row["books"][outcome] = {"status":"captured" if book.source=="clob" else "unavailable",
                        "quote":book.to_dict(),"fee":None if fee is None else {"rate":fee.rate,"exponent":fee.exponent},
                        "depth_levels_retained_per_side":10,
                        "fee_status":"clob_lookup_may_be_cached_1h" if fee is not None else "unknown",
                        "fee_lookup_at":datetime.now(UTC).isoformat()}
                except Exception as exc:
                    row["books"][outcome] = {"status":"error","error_type":type(exc).__name__}
        snapshot.update(event="research_snapshot",research_schema_version=1,candidate_id="forward_flat_touch_v1",
                        captured_at=datetime.now(UTC).isoformat(),
                        book_sampling={"policy":"round_robin_all_entry_window_markets_before_trade_gates",
                                       "eligible":len(eligible),"selected":len(selected),"max_markets":self.max_books},
                        dropped_snapshots=self.dropped,code_sha256=self.code_hashes,
                        capture_run_started_at=getattr(self,"run_started_at",None))
        return snapshot

    def _run(self) -> None:
        reader = None
        minutes = None
        while True:
            snapshot = self.pending.get()
            try:
                used = sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())
                if used>=self.max_bytes or shutil.disk_usage(self.directory).free < 1024**3:
                    self.dropped += 1
                    self.write_status(state="storage_limit",bytes_used=used)
                    continue
                self.write_status(state="capturing",snapshot_ts=snapshot["ts"])
                if reader is None:
                    reader = ExecutionClient(mode="shadow")  # public data only, never wallet keys
                if minutes is None:
                    minutes = MinuteEvidence(self.directory)
                result = self.enrich(snapshot,reader,minutes)
                result["payload_sha256"] = hashlib.sha256(json.dumps(result,separators=(",",":"),sort_keys=True).encode()).hexdigest()
                key = snapshot['ts'].replace(":", "-")
                path = self.directory / f"snapshot-{key}.json.gz"
                write_gzip(path,result)
                self.completed += 1
                self.write_status(state="collecting",last_snapshot_ts=snapshot["ts"],last_file=path.name,
                                  last_book_sampling=result["book_sampling"],minute_backfill=result["minute_backfill"])
            except Exception as exc:
                self.dropped += 1
                log.warning("Research capture failed: %s",exc)
                try:
                    self.write_status(state="error",error_type=type(exc).__name__)
                except OSError:
                    log.warning("Cannot write research status")
            finally:
                self.pending.task_done()
