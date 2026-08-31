"""Live Bybit DataSource: fetches recent candles directly from the public
REST kline endpoint (GET /v5/market/kline, category=linear) instead of
reading the static backtest/data_15m/ parquet cache. Implements the exact
same DataSource interface as BybitCryptoSource, so engine.py needs zero
changes -- only config.json's "market" (or DASHBOARD_MARKET) selects which
one runs.

Resilience: every successful fetch is written to a small local parquet
cache (indicators/cache/{symbol}_{timeframe}.parquet). If a live fetch
fails (network error, exhausted retries, bad response), get_ohlcv logs a
clear warning and falls back to that last-known-good cache instead of
raising -- so one transient Bybit hiccup does not take down the whole
dashboard run. The returned DataFrame carries this in `df.attrs`:
  attrs["source"]     = "live" or "cache"
  attrs["fetched_at"] = ISO timestamp of when that data was actually fetched
  attrs["stale"]      = True only when cache was used as a fallback
`df.attrs` is a real pandas feature for exactly this kind of sidecar
metadata -- it does not change the DataFrame's columns or the interface
contract, so callers that only care about OHLCV are unaffected, while
run_dashboard.py can inspect it to log/report staleness per symbol.

Rate limiting follows the same pattern already used by
backtest/fetch_15m.py / fetch_oi.py / fetch_funding.py in this project:
a small delay between requests plus exponential backoff on 429s.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from ..data import UnsupportedTimeframe, discover_symbols
from .base import DataSource, MarketHours, validate_ohlcv

logger = logging.getLogger("indicators.bybit_live")

BYBIT_MARKET_URL = "https://api.bybit.com"
CATEGORY = "linear"
BARS_TO_FETCH = 300          # generous window: covers Ichimoku's 52+26=78-bar need plus buffer
REQUEST_TIMEOUT = 15
REQUEST_DELAY_SECONDS = 0.2
MAX_RETRIES = 5
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"

# Only timeframes Bybit's kline endpoint natively serves -- no client-side
# resampling here (unlike BybitCryptoSource, which resamples from a fixed
# 15m base). Extend this map if a future config asks for another native
# Bybit interval.
_TIMEFRAME_TO_BYBIT_INTERVAL = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "1w": "W",
}


class BybitLiveCryptoSource(DataSource):
    def __init__(self) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, symbol: str, timeframe: str) -> Path:
        return CACHE_DIR / f"{symbol}_{timeframe}.parquet"

    def _meta_path(self, symbol: str, timeframe: str) -> Path:
        return CACHE_DIR / f"{symbol}_{timeframe}.meta.json"

    def _get_with_retry(self, params: dict) -> dict:
        for attempt in range(1, MAX_RETRIES + 1):
            resp = requests.get(f"{BYBIT_MARKET_URL}/v5/market/kline", params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"Bybit rate-limited (429) - backing off {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("retCode") != 0:
                raise RuntimeError(f"Bybit kline error: {payload.get('retMsg')}")
            return payload
        raise RuntimeError(f"gave up after {MAX_RETRIES} retries (persistent rate limiting)")

    def _fetch_live(self, symbol: str, timeframe: str) -> pd.DataFrame:
        interval = _TIMEFRAME_TO_BYBIT_INTERVAL.get(timeframe)
        if interval is None:
            raise UnsupportedTimeframe(
                f"BybitLiveCryptoSource has no native Bybit interval for timeframe {timeframe!r} "
                f"(supported: {sorted(_TIMEFRAME_TO_BYBIT_INTERVAL)})")

        params = {"category": CATEGORY, "symbol": symbol, "interval": interval, "limit": BARS_TO_FETCH}
        payload = self._get_with_retry(params)
        rows = payload.get("result", {}).get("list", [])
        if not rows:
            raise RuntimeError(f"Bybit returned no candles for {symbol} {timeframe}")

        rows = sorted(rows, key=lambda r: int(r[0]))  # Bybit returns newest-first; we want oldest-first
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])
        df = df.drop(columns=["turnover"])
        df["timestamp"] = df["timestamp"].astype("int64")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype("float64")
        df = df.drop_duplicates(subset="timestamp", keep="first").sort_values("timestamp").reset_index(drop=True)
        df["time"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    def _load_cache(self, symbol: str, timeframe: str) -> tuple[pd.DataFrame, str] | None:
        """Returns (df, fetched_at_iso) from the last successful fetch, or
        None if no cache exists yet. `.attrs` does NOT survive a parquet
        round-trip, so the fetch timestamp is kept in a tiny JSON sidecar
        instead of relying on DataFrame metadata."""
        path = self._cache_path(symbol, timeframe)
        meta_path = self._meta_path(symbol, timeframe)
        if not path.exists():
            return None
        df = pd.read_parquet(path)
        fetched_at = "unknown"
        if meta_path.exists():
            fetched_at = json.loads(meta_path.read_text(encoding="utf-8")).get("fetched_at", "unknown")
        return df, fetched_at

    def _save_cache(self, symbol: str, timeframe: str, df: pd.DataFrame, fetched_at: str) -> None:
        try:
            df.to_parquet(self._cache_path(symbol, timeframe))
            self._meta_path(symbol, timeframe).write_text(json.dumps({"fetched_at": fetched_at}), encoding="utf-8")
        except OSError as exc:
            logger.warning(f"could not write cache for {symbol} {timeframe}: {exc}")

    def get_ohlcv(self, symbol: str, timeframe: str,
                  start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
        try:
            df = self._fetch_live(symbol, timeframe)
            fetched_at = datetime.now(timezone.utc).isoformat()
            df.attrs["source"] = "live"
            df.attrs["fetched_at"] = fetched_at
            df.attrs["stale"] = False
            self._save_cache(symbol, timeframe, df, fetched_at)
        except Exception as exc:
            logger.warning(f"live fetch failed for {symbol} {timeframe} ({exc}); falling back to local cache")
            cached = self._load_cache(symbol, timeframe)
            if cached is None:
                raise RuntimeError(
                    f"no live data and no cache available for {symbol} {timeframe}") from exc
            df, fetched_at = cached
            df.attrs["source"] = "cache"
            df.attrs["fetched_at"] = fetched_at
            df.attrs["stale"] = True
            logger.warning(f"{symbol} {timeframe}: USING STALE CACHED DATA from {fetched_at}")

        if start is not None:
            start_ts = pd.Timestamp(start, tz="UTC") if start.tzinfo is None else pd.Timestamp(start)
            df = df[df["time"] >= start_ts]
        if end is not None:
            end_ts = pd.Timestamp(end, tz="UTC") if end.tzinfo is None else pd.Timestamp(end)
            df = df[df["time"] <= end_ts]
        df = df.reset_index(drop=True)

        validate_ohlcv(df, "BybitLiveCryptoSource")
        time.sleep(REQUEST_DELAY_SECONDS)  # be polite to the public endpoint between successive calls
        return df

    def get_available_symbols(self) -> tuple[str, ...]:
        return discover_symbols()

    def market_hours(self) -> MarketHours:
        return MarketHours(is_24_7=True, timezone="UTC")


__all__ = ["BybitLiveCryptoSource"]
