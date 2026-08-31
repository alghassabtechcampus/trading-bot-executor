"""Current (and, for now, only) concrete DataSource: Bybit linear-futures
15m parquet cache (backtest/data_15m/*.parquet), resampled to whatever
timeframe is requested. This is exactly the loading logic that used to
live directly in indicators/data.py before the multi-market refactor --
moved here unchanged so the engine depends on the DataSource interface
instead of a crypto-specific module.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..data import load_15m, resample
from .base import DataSource, MarketHours, validate_ohlcv


class BybitCryptoSource(DataSource):
    """Crypto is 24/7 with no session boundaries, so market_hours() is
    trivial; get_ohlcv resamples the 15m parquet cache on demand."""

    def get_ohlcv(self, symbol: str, timeframe: str,
                  start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
        df = resample(load_15m(symbol), timeframe)
        if start is not None:
            start_ts = pd.Timestamp(start, tz="UTC") if start.tzinfo is None else pd.Timestamp(start)
            df = df[df["time"] >= start_ts]
        if end is not None:
            end_ts = pd.Timestamp(end, tz="UTC") if end.tzinfo is None else pd.Timestamp(end)
            df = df[df["time"] <= end_ts]
        df = df.reset_index(drop=True)
        validate_ohlcv(df, "BybitCryptoSource")
        return df

    def get_available_symbols(self) -> tuple[str, ...]:
        from ..data import discover_symbols
        return discover_symbols()

    def market_hours(self) -> MarketHours:
        return MarketHours(is_24_7=True, timezone="UTC")


__all__ = ["BybitCryptoSource"]
