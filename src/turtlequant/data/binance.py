from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests

# DATA_SOURCE controls which exchange is used for OHLCV fetching.
# auto    — try Binance; on 451 geo-block fall back to OKX, then Gate.io
# binance — always Binance (use when your server is not geo-blocked)
# bybit   — always Bybit  (globally accessible on most hosts, years of history)
# okx     — always OKX    (EU-accessible, full history back to 2022)
# gateio  — always Gate.io (limited history: ~34d for 5m, ~104d for 15m)
DATA_SOURCE: str = os.getenv("DATA_SOURCE", "auto").lower()

BASE_URL = "https://api.binance.com/api/v3/klines"

# Bybit — globally accessible on most hosts, deep history (5m back to 2020+)
BYBIT_BASE_URL = "https://api.bybit.com/v5/market/kline"
BYBIT_INTERVAL_MAP = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "1d": "D",
}
BYBIT_MAX_LIMIT = 1000

# OKX — EU-accessible, full history back to 2022, 300 candles/request
OKX_BASE_URL = "https://www.okx.com/api/v5/market/history-candles"
OKX_INTERVAL_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "1d": "1D",
}
OKX_MAX_LIMIT = 300

# Gate.io fallback (global availability, real exchange OHLCV)
GATEIO_BASE_URL = "https://api.gateio.ws/api/v4/spot/candlesticks"
GATEIO_INTERVAL_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "1d": "1d",
}
GATEIO_MAX_LIMIT = 1000
# Gate.io enforces a max lookback of 10 000 candles per interval.
# Depths (approx): 5m→34d, 15m→104d, 1h→416d, 4h→1666d
GATEIO_MAX_CANDLES = 9_900  # stay slightly under the hard limit
_INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "1d": 86400,
}

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
INTERVALS = ["5m", "15m", "1h", "4h"]
START = datetime(2022, 1, 1, tzinfo=UTC)
MAX_LIMIT = 1000


def _bybit_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Fetch OHLCV from Bybit spot API, returning the same schema as fetch_klines.

    Bybit row format (descending): [startTime_ms, open, high, low, close, volume, turnover]
    """
    bybit_interval = BYBIT_INTERVAL_MAP.get(interval)
    if bybit_interval is None:
        raise ValueError(f"Unsupported interval for Bybit: {interval!r}")

    rows: list[list] = []
    # Bybit paginates backwards: pass end as 'end', walk backwards via start.
    # Simpler approach: walk forward using 'start', advancing by limit*interval each page.
    interval_secs = _INTERVAL_SECONDS.get(interval, 300)
    cursor_ms = start_ms

    while cursor_ms < end_ms:
        page_end_ms = min(cursor_ms + BYBIT_MAX_LIMIT * interval_secs * 1000, end_ms)
        params = {
            "category": "spot",
            "symbol": symbol,
            "interval": bybit_interval,
            "start": cursor_ms,
            "end": page_end_ms,
            "limit": BYBIT_MAX_LIMIT,
        }
        resp = requests.get(BYBIT_BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit error: {payload.get('retMsg')}")

        page = payload["result"]["list"]  # newest-first
        if not page:
            break

        rows.extend(reversed(page))  # flip to oldest-first
        last_ts_ms = int(page[0][0])  # page[0] is newest after reversal context
        if last_ts_ms <= cursor_ms:
            break
        cursor_ms = last_ts_ms + interval_secs * 1000
        time.sleep(0.05)

    if not rows:
        return pd.DataFrame()

    # Columns: [startTime_ms, open, high, low, close, volume, turnover]
    df = pd.DataFrame(rows, columns=["open_time_ms", "open", "high", "low", "close", "volume", "quote_asset_volume"])
    df = df.drop_duplicates(subset=["open_time_ms"]).sort_values("open_time_ms").reset_index(drop=True)

    for col in ["open", "high", "low", "close", "volume", "quote_asset_volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["open_time"] = pd.to_datetime(pd.to_numeric(df["open_time_ms"]), unit="ms", utc=True)
    df["close_time"] = pd.NaT
    df["number_of_trades"] = pd.array([pd.NA] * len(df), dtype="Int64")
    df["taker_buy_base_asset_volume"] = float("nan")
    df["taker_buy_quote_asset_volume"] = float("nan")
    df["ignore"] = float("nan")

    return df[
        [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_asset_volume",
            "number_of_trades",
            "taker_buy_base_asset_volume",
            "taker_buy_quote_asset_volume",
            "ignore",
        ]
    ]


def _okx_symbol(symbol: str) -> str:
    """Convert Binance-style symbol (BTCUSDT) to OKX instId (BTC-USDT)."""
    if symbol.endswith("USDT"):
        return symbol[:-4] + "-USDT"
    if symbol.endswith("BTC"):
        return symbol[:-3] + "-BTC"
    raise ValueError(f"Cannot convert symbol to OKX format: {symbol!r}")


def _okx_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Fetch OHLCV from OKX history-candles API, returning the same schema as fetch_klines.

    OKX row format (descending): [ts_ms, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
    Pagination: pass after=<ts_ms> to get candles with ts < after.
    """
    okx_interval = OKX_INTERVAL_MAP.get(interval)
    if okx_interval is None:
        raise ValueError(f"Unsupported interval for OKX: {interval!r}")

    inst_id = _okx_symbol(symbol)
    rows: list[list] = []
    # Walk backwards from end_ms, stopping when we pass start_ms.
    after_ms = end_ms

    while True:
        params = {
            "instId": inst_id,
            "bar": okx_interval,
            "after": str(after_ms),
            "limit": str(OKX_MAX_LIMIT),
        }
        resp = requests.get(OKX_BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX error: {payload.get('msg')}")

        page = payload["data"]  # descending: page[0] is newest, page[-1] is oldest
        if not page:
            break

        rows.extend(page)
        oldest_ts = int(page[-1][0])
        if oldest_ts <= start_ms:
            break
        after_ms = oldest_ts
        time.sleep(0.05)

    if not rows:
        return pd.DataFrame()

    # Filter to requested range and sort ascending
    df = pd.DataFrame(
        rows,
        columns=[
            "open_time_ms",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "vol_ccy",
            "quote_asset_volume",
            "confirm",
        ],
    )
    df["open_time_ms"] = pd.to_numeric(df["open_time_ms"])
    df = df[df["open_time_ms"] >= start_ms]
    df = df.drop_duplicates(subset=["open_time_ms"]).sort_values("open_time_ms").reset_index(drop=True)

    for col in ["open", "high", "low", "close", "volume", "quote_asset_volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["open_time"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)
    df["close_time"] = pd.NaT
    df["number_of_trades"] = pd.array([pd.NA] * len(df), dtype="Int64")
    df["taker_buy_base_asset_volume"] = float("nan")
    df["taker_buy_quote_asset_volume"] = float("nan")
    df["ignore"] = float("nan")

    return df[
        [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_asset_volume",
            "number_of_trades",
            "taker_buy_base_asset_volume",
            "taker_buy_quote_asset_volume",
            "ignore",
        ]
    ]


def _gateio_symbol(symbol: str) -> str:
    """Convert Binance-style symbol (BTCUSDT) to Gate.io style (BTC_USDT)."""
    if symbol.endswith("USDT"):
        return symbol[:-4] + "_USDT"
    if symbol.endswith("BTC"):
        return symbol[:-3] + "_BTC"
    raise ValueError(f"Cannot convert symbol to Gate.io format: {symbol!r}")


def _gateio_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Fetch OHLCV from Gate.io spot API, returning the same schema as fetch_klines.

    Gate.io row format (ascending): [timestamp_sec, quote_vol, close, high, low, open, base_vol, is_closed]
    """
    gate_interval = GATEIO_INTERVAL_MAP.get(interval)
    if gate_interval is None:
        raise ValueError(f"Unsupported interval for Gate.io fallback: {interval!r}")

    gate_symbol = _gateio_symbol(symbol)
    interval_secs = _INTERVAL_SECONDS.get(interval, 300)
    rows: list[list] = []
    end_sec = end_ms // 1000

    # Clamp start to Gate.io's maximum lookback window.
    max_lookback_start = int(time.time()) - GATEIO_MAX_CANDLES * interval_secs
    cursor_sec = max(start_ms // 1000, max_lookback_start)
    if cursor_sec > start_ms // 1000:
        actual = datetime.fromtimestamp(cursor_sec, tz=UTC).date()
        print(
            f"[data] Gate.io {interval}: history capped at {GATEIO_MAX_CANDLES} candles; fetching from {actual}",
            flush=True,
        )

    while cursor_sec < end_sec:
        # Gate.io requires (to - from) / interval_secs < limit — compute a page-sized window.
        page_end = min(cursor_sec + (GATEIO_MAX_LIMIT - 1) * interval_secs, end_sec)
        params = {
            "currency_pair": gate_symbol,
            "interval": gate_interval,
            "from": cursor_sec,
            "to": page_end,
            "limit": GATEIO_MAX_LIMIT,
        }
        resp = requests.get(GATEIO_BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            break

        rows.extend(data)

        last_ts = int(data[-1][0])
        if last_ts <= cursor_sec:
            break
        cursor_sec = last_ts + 1
        time.sleep(0.1)

    if not rows:
        return pd.DataFrame()

    # Parse: [ts_sec, quote_vol, close, high, low, open, base_vol, is_closed]
    df = pd.DataFrame(
        rows,
        columns=[
            "open_time_sec",
            "quote_asset_volume",
            "close",
            "high",
            "low",
            "open",
            "volume",
            "is_closed",
        ],
    )
    df = df.drop_duplicates(subset=["open_time_sec"]).sort_values("open_time_sec").reset_index(drop=True)

    for col in ["open", "high", "low", "close", "volume", "quote_asset_volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["open_time"] = pd.to_datetime(pd.to_numeric(df["open_time_sec"]) * 1000, unit="ms", utc=True)

    # Fill columns not provided by Gate.io so the schema matches Binance output
    df["close_time"] = pd.NaT
    df["number_of_trades"] = pd.array([pd.NA] * len(df), dtype="Int64")
    df["taker_buy_base_asset_volume"] = float("nan")
    df["taker_buy_quote_asset_volume"] = float("nan")
    df["ignore"] = float("nan")

    return df[
        [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_asset_volume",
            "number_of_trades",
            "taker_buy_base_asset_volume",
            "taker_buy_quote_asset_volume",
            "ignore",
        ]
    ]


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    if DATA_SOURCE == "bybit":
        return _bybit_klines(symbol, interval, start_ms, end_ms)
    if DATA_SOURCE == "okx":
        return _okx_klines(symbol, interval, start_ms, end_ms)
    if DATA_SOURCE == "gateio":
        return _gateio_klines(symbol, interval, start_ms, end_ms)

    # "binance" or "auto": try Binance first
    rows: list[list] = []
    cursor = start_ms

    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": MAX_LIMIT,
        }
        try:
            resp = requests.get(BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 451:
                if DATA_SOURCE == "binance":
                    raise RuntimeError(
                        "Binance returned 451 geo-block. Set DATA_SOURCE=okx, DATA_SOURCE=bybit, "
                        "or DATA_SOURCE=gateio in .env."
                    ) from exc
                print("[data] Binance geo-blocked (451) — falling back to OKX", flush=True)
                return _okx_klines(symbol, interval, start_ms, end_ms)
            raise

        data = resp.json()
        if not data:
            break

        rows.extend(data)
        last_open_time = data[-1][0]
        if last_open_time <= cursor:
            break
        cursor = last_open_time + 1
        time.sleep(0.1)

    df = pd.DataFrame(
        rows,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_asset_volume",
            "number_of_trades",
            "taker_buy_base_asset_volume",
            "taker_buy_quote_asset_volume",
            "ignore",
        ],
    )
    if df.empty:
        return df

    df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    num_cols = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_asset_volume",
        "taker_buy_base_asset_volume",
        "taker_buy_quote_asset_volume",
    ]
    for col in num_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    trades_numeric = pd.to_numeric(df["number_of_trades"], errors="coerce")
    df["number_of_trades"] = pd.Series(trades_numeric, index=df.index).astype("Int64")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def main() -> None:
    start_ms = int(START.timestamp() * 1000)
    end_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    for symbol in SYMBOLS:
        asset = symbol.replace("USDT", "").lower()
        for interval in INTERVALS:
            print(f"Fetching {symbol} {interval}...")
            df = fetch_klines(symbol, interval, start_ms, end_ms)
            out = data_dir / f"{asset}_{interval}.parquet"
            df.to_parquet(out, index=False)
            print(f"Saved {len(df):,} candles -> {out}")


if __name__ == "__main__":
    main()
