"""VWAP (Volume-Weighted Average Price), daily-anchored.

VWAP[t] = cumsum(typical_price * volume) / cumsum(volume), where
typical_price = (high + low + close) / 3, and both cumulative sums reset
at the start of each UTC calendar day. This is the standard intraday VWAP
definition (matches backtest/analysis/indicator_scan.py's ind_vwap, reused
here for consistency with the 20-indicator scan that validated it).

State: bullish if the latest close is above VWAP, bearish if below. A
"volume_confirmed" flag additionally reports whether the latest bar's
volume exceeds its own trailing average (informational only -- the 20-
indicator scan tested this as a signal filter, not as part of the base
state here).
"""

from __future__ import annotations

import pandas as pd


def compute(df: pd.DataFrame, volume_avg_bars: int = 20) -> dict:
    if df.empty:
        return {"value": None, "state": "neutral", "details": {}}

    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]
    day = df["time"].dt.floor("D")
    typical = (high + low + close) / 3
    pv = typical * volume
    vwap = pv.groupby(day).cumsum() / volume.groupby(day).cumsum().replace(0, float("nan"))

    last_close = float(close.iloc[-1])
    last_vwap = float(vwap.iloc[-1]) if pd.notna(vwap.iloc[-1]) else None
    if last_vwap is None:
        return {"value": None, "state": "neutral", "details": {"reason": "insufficient same-day volume"}}

    distance_pct = (last_close - last_vwap) / last_vwap * 100
    state = "bullish" if last_close > last_vwap else ("bearish" if last_close < last_vwap else "neutral")

    avg_volume = float(volume.rolling(volume_avg_bars).mean().iloc[-1]) if len(df) >= volume_avg_bars else None
    last_volume = float(volume.iloc[-1])
    volume_confirmed = (avg_volume is not None) and (last_volume > avg_volume)

    return {
        "value": round(last_vwap, 8),
        "state": state,
        "details": {
            "price": round(last_close, 8),
            "distance_pct": round(distance_pct, 4),
            "last_volume": round(last_volume, 4),
            "avg_volume_20": round(avg_volume, 4) if avg_volume is not None else None,
            "volume_confirmed": volume_confirmed,
        },
    }


__all__ = ["compute"]
