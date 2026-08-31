"""Abstract data-source interface. Every market (crypto today; TASI/Saudi
and a US-equities source later) plugs into the SAME indicator engine by
implementing this interface -- nothing in vwap.py, macd.py, adx.py,
ichimoku.py, support_resistance.py, trade_zone.py, or engine.py should ever
import a source-specific module directly.

A new market means: write one class implementing DataSource, register it
in sources/registry.py under a market name, and set `"market"` in
config.json (or DASHBOARD_MARKET) to that name. No other file changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

REQUIRED_OHLCV_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "time")


@dataclass(frozen=True, slots=True)
class MarketHours:
    """Describes when a market trades, so session-dependent calculations
    (e.g. VWAP resetting at the start of each session rather than at UTC
    midnight) can be adapted per source without changing their call sites.
    Not yet consumed by any indicator -- crypto is_24_7=True makes today's
    calendar-day VWAP reset correct as-is; a future session-based source
    is expected to use session_open/session_close/timezone/trading_days to
    reset VWAP at its own session boundaries instead.
    """

    is_24_7: bool
    timezone: str                                  # IANA name, e.g. "UTC", "Asia/Riyadh", "America/New_York"
    session_open: str | None = None                 # "HH:MM" in `timezone`, None if is_24_7
    session_close: str | None = None                # "HH:MM" in `timezone`, None if is_24_7
    trading_days: tuple[str, ...] | None = None      # e.g. ("Mon", ..., "Fri"), None if is_24_7


class DataSource(ABC):
    """Minimal contract the indicator engine depends on. Every method must
    be safe to call repeatedly (the engine does not assume caching)."""

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str,
                  start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
        """Returns a DataFrame with exactly the columns in
        REQUIRED_OHLCV_COLUMNS, sorted ascending by timestamp, no duplicate
        timestamps. `timestamp` is int64 UTC milliseconds; `time` is the
        equivalent tz-aware UTC pandas Timestamp column (kept alongside
        timestamp because every indicator module reads df["time"] for
        display/date-bucketing). `start`/`end` are optional UTC datetime
        bounds (inclusive); omitting them returns all available history up
        to the most recent bar this source has.
        """
        raise NotImplementedError

    @abstractmethod
    def get_available_symbols(self) -> tuple[str, ...]:
        """All symbols this source can serve, sorted."""
        raise NotImplementedError

    @abstractmethod
    def market_hours(self) -> MarketHours:
        """Static description of this market's trading calendar."""
        raise NotImplementedError


def validate_ohlcv(df: pd.DataFrame, source_name: str) -> None:
    """Shared guard a DataSource implementation can call before returning,
    so a malformed source fails loudly at the boundary instead of producing
    silently-wrong indicator values downstream."""
    missing = [c for c in REQUIRED_OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{source_name}.get_ohlcv is missing required columns: {missing}")
    if not df["timestamp"].is_monotonic_increasing:
        raise ValueError(f"{source_name}.get_ohlcv must return rows sorted ascending by timestamp")
    if df["timestamp"].duplicated().any():
        raise ValueError(f"{source_name}.get_ohlcv returned duplicate timestamps")


__all__ = ["DataSource", "MarketHours", "REQUIRED_OHLCV_COLUMNS", "validate_ohlcv"]
