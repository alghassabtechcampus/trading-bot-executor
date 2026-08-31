"""Average True Range, standard Wilder 14-period smoothing.

True Range[t] = max(high[t]-low[t], |high[t]-close[t-1]|, |low[t]-close[t-1]|)
ATR = Wilder's smoothed moving average of True Range (alpha = 1/period).

Not a directional indicator (no bullish/bearish state) -- this is a pure
volatility measure used by trade_zone.py to size the stop/target distance.
"""

from __future__ import annotations

import pandas as pd


def compute(df: pd.DataFrame, period: int = 14) -> float | None:
    if len(df) < period + 1:
        return None
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    value = atr.iloc[-1]
    return float(value) if pd.notna(value) else None


__all__ = ["compute"]
