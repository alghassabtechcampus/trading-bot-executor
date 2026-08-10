"""
يجلب شموع 5 دقائق من Bybit (API عام، بلا مفاتيح) ويخزّنها محلياً
بـ backtest/data/ كملفات CSV، حتى لا تُعاد نفس الطلبات بكل تشغيل.
"""

from __future__ import annotations

import csv
import os
import sys
import time
from typing import Any

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BYBIT_BASE_URL = "https://api.bybit.com"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

REQUEST_TIMEOUT = 15
REQUEST_DELAY_SECONDS = 0.25
MAX_RETRIES = 3


def _cache_path(symbol: str) -> str:
    return os.path.join(DATA_DIR, f"{symbol}_5m.csv")


def _fetch_page(symbol: str, end_ms: int, limit: int = 1000) -> list[list[str]]:
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": "5",
        "end": end_ms,
        "limit": limit,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                f"{BYBIT_BASE_URL}/v5/market/kline",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()

            if payload.get("retCode") != 0:
                raise RuntimeError(f"Bybit error for {symbol}: {payload.get('retMsg')}")

            return payload.get("result", {}).get("list", [])

        except (requests.RequestException, RuntimeError) as exc:
            if attempt == MAX_RETRIES:
                raise
            print(f"  ⚠ retry {attempt}/{MAX_RETRIES} for {symbol}: {exc}")
            time.sleep(1.5 * attempt)

    return []


def fetch_candles(symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """يسحب كل الشموع بين start_ms و end_ms عبر تصفّح للخلف (backward pagination)."""

    all_rows: dict[int, list[str]] = {}
    cursor_end = end_ms

    while True:
        batch = _fetch_page(symbol, cursor_end)
        if not batch:
            break

        for row in batch:
            all_rows[int(row[0])] = row

        oldest_ts = min(int(row[0]) for row in batch)

        if oldest_ts <= start_ms or len(batch) < 1000:
            break

        cursor_end = oldest_ts - 1
        time.sleep(REQUEST_DELAY_SECONDS)

    candles = [
        {
            "timestamp": ts,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        for ts, row in all_rows.items()
        if start_ms <= ts <= end_ms
    ]
    candles.sort(key=lambda c: c["timestamp"])
    return candles


def save_cache(symbol: str, candles: list[dict[str, Any]]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _cache_path(symbol)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            writer.writerow([c["timestamp"], c["open"], c["high"], c["low"], c["close"], c["volume"]])


def load_cache(symbol: str) -> list[dict[str, Any]] | None:
    path = _cache_path(symbol)
    if not os.path.exists(path):
        return None

    candles = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append({
                "timestamp": int(row["timestamp"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
    return candles


def get_candles(
    symbol: str,
    start_ms: int,
    end_ms: int,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    if not force_refresh:
        cached = load_cache(symbol)
        if cached and cached[0]["timestamp"] <= start_ms and cached[-1]["timestamp"] >= end_ms - 5 * 60 * 1000:
            return [c for c in cached if start_ms <= c["timestamp"] <= end_ms]

    print(f"  جلب بيانات {symbol} من Bybit...")
    candles = fetch_candles(symbol, start_ms, end_ms)
    save_cache(symbol, candles)
    print(f"  ✓ {symbol}: {len(candles)} شمعة")
    return candles