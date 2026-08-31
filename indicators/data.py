"""OHLCV loading and generic timeframe resampling for the dashboard engine.

Single source of truth: backtest/data_15m/*.parquet (Bybit linear/USDT-
perpetual, fetched for the Reversal Scanner experiment). Deliberately NOT
backtest/data_long/ (Bybit SPOT) -- mixing spot price with a derivatives-
only concept caused a real bug earlier in this project (see the Open
Interest Z-score experiment's fix); every dashboard indicator here is
computed on one consistent instrument.

Design difference from the backtest scripts in backtest/analysis/: those
scripts drop the last, still-forming bucket of any resample to avoid
lookahead in a historical simulation. This module is for a LIVE dashboard
reading of "where things stand right now", so it deliberately KEEPS the
final (possibly still-forming) bucket -- that partial bar's data (price,
running VWAP, etc.) is exactly what an operator wants to see. Every
indicator function that uses this data still only reads a bar's own
OHLCV and earlier bars, so nothing here invents future information; it
simply doesn't discard the freshest real information available.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRICE_DIR = PROJECT_ROOT / "backtest" / "data_15m"
DATA_LONG_DIR = PROJECT_ROOT / "backtest" / "data_long"

# The 9 canonical pairs this dashboard covers. A plain constant (not read
# from backtest/data_long/'s CSV filenames, despite the directory constant
# above still existing for the optional "crypto_static" source) so
# indicators/ has zero runtime dependency on any file outside this
# package -- required for it to deploy as a fully standalone service.
SYMBOLS = ("ADAUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT",
           "LINKUSDT", "LTCUSDT", "SOLUSDT", "XRPUSDT")

FIFTEEN_MIN_MS = 15 * 60_000
_TIMEFRAME_RE = re.compile(r"^(\d+)([mhdw])$")
_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


class UnsupportedTimeframe(ValueError):
    pass


def timeframe_to_ms(timeframe: str) -> int:
    match = _TIMEFRAME_RE.match(timeframe.strip().lower())
    if not match:
        raise UnsupportedTimeframe(f"unrecognized timeframe: {timeframe!r} (expected e.g. '15m', '4h', '1d', '1w')")
    amount, unit = match.groups()
    ms = int(amount) * _UNIT_MS[unit]
    if ms % FIFTEEN_MIN_MS != 0:
        raise UnsupportedTimeframe(f"timeframe {timeframe!r} is not a multiple of the 15m source resolution")
    return ms


def discover_symbols() -> tuple[str, ...]:
    """The 9 canonical pairs this dashboard covers (see SYMBOLS above)."""
    return SYMBOLS


def load_15m(symbol: str) -> pd.DataFrame:
    df = pd.read_parquet(PRICE_DIR / f"{symbol}_15m.parquet")
    return df.drop_duplicates(subset="timestamp", keep="first").sort_values("timestamp").reset_index(drop=True)


def resample(df15: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample 15m OHLCV to any coarser timeframe. Keeps the final bucket
    even if partially formed (see module docstring)."""
    bucket_ms = timeframe_to_ms(timeframe)
    if bucket_ms == FIFTEEN_MIN_MS:
        out = df15.copy()
    else:
        df = df15.copy()
        df["bucket"] = (df["timestamp"] // bucket_ms) * bucket_ms
        grouped = df.groupby("bucket")
        agg = grouped.agg(open=("open", "first"), high=("high", "max"),
                           low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))
        out = agg.reset_index().rename(columns={"bucket": "timestamp"})
    out["time"] = pd.to_datetime(out["timestamp"], unit="ms", utc=True)
    return out


def load_timeframe(symbol: str, timeframe: str) -> pd.DataFrame:
    return resample(load_15m(symbol), timeframe)


__all__ = ["timeframe_to_ms", "discover_symbols", "load_15m", "resample", "load_timeframe",
           "UnsupportedTimeframe", "PRICE_DIR", "DATA_LONG_DIR", "SYMBOLS"]
