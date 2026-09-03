"""Fetches the multi-timeframe history the ATR-stop study needs, straight from
Bybit's public kline endpoint -- the same source indicators/sources/bybit_live.py
uses live, so the study runs on exactly the data the dashboard would have seen.

One parquet per (symbol, timeframe) in backtest/data_stopstudy/. Re-running
skips files that already cover the requested window.
"""
from __future__ import annotations

import sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

OUT = Path(__file__).resolve().parent / "data_stopstudy"
SYMBOLS = ["ADAUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT",
           "LINKUSDT", "LTCUSDT", "SOLUSDT", "XRPUSDT"]
# timeframe -> (bybit interval, bar milliseconds, days of history to pull)
SPEC = {
    "15m": ("15", 900_000, 270),
    "1h":  ("60", 3_600_000, 730),
    "4h":  ("240", 14_400_000, 1460),
    "1d":  ("D", 86_400_000, 1825),
    "1w":  ("W", 604_800_000, 3650),
}
URL = "https://api.bybit.com/v5/market/kline"


def fetch(symbol: str, tf: str) -> pd.DataFrame:
    interval, ms, days = SPEC[tf]
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    # Bybit returns the NEWEST <=1000 bars inside [start, end], so paging has
    # to walk BACKWARDS by pulling `end` down to just before the oldest bar
    # received; advancing `start` forward only ever re-returns the same tail.
    rows, cursor_end = {}, end
    while cursor_end > start:
        for attempt in range(6):
            r = requests.get(URL, params={"category": "linear", "symbol": symbol, "interval": interval,
                                          "start": start, "end": cursor_end, "limit": 1000}, timeout=25)
            if r.status_code == 429:
                time.sleep(2 ** attempt); continue
            r.raise_for_status(); break
        p = r.json()
        if p.get("retCode") != 0:
            raise RuntimeError(f"{symbol} {tf}: {p.get('retMsg')}")
        batch = p["result"]["list"]
        if not batch:
            break
        for k in batch:
            rows[int(k[0])] = [float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
        oldest = min(int(k[0]) for k in batch)
        if oldest >= cursor_end:
            break
        cursor_end = oldest - 1
        time.sleep(0.15)
    df = pd.DataFrame([[t, *rows[t]] for t in sorted(rows)],
                      columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for symbol in SYMBOLS:
        for tf in SPEC:
            path = OUT / f"{symbol}_{tf}.parquet"
            if path.exists():
                print(f"skip {path.name}", flush=True); continue
            df = fetch(symbol, tf)
            df.to_parquet(path, index=False)
            print(f"{symbol:<9} {tf:<4} {len(df):>6} bars  "
                  f"{df['time'].iloc[0].date()} -> {df['time'].iloc[-1].date()}", flush=True)
    print("DONE")


if __name__ == "__main__":
    main()
