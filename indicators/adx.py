"""ADX (Average Directional Index) with +DI/-DI, standard Wilder 14-period.

+DM[t] = high[t]-high[t-1] if that exceeds low[t-1]-low[t] and is positive, else 0
-DM[t] = low[t-1]-low[t]   if that exceeds high[t]-high[t-1] and is positive, else 0
+DI = 100 * WilderSmooth(+DM, 14) / WilderSmooth(TrueRange, 14)
-DI = 100 * WilderSmooth(-DM, 14) / WilderSmooth(TrueRange, 14)
DX  = 100 * |+DI - -DI| / (+DI + -DI)
ADX = WilderSmooth(DX, 14)

State: "bullish" if ADX > 25 and +DI > -DI (a strong uptrend); "bearish" if
ADX > 25 and -DI > +DI (a strong downtrend); otherwise "neutral" (no
established directional trend by this measure, regardless of which DI is
nominally larger). Uses the same bullish/bearish/neutral vocabulary as
every other indicator module so confluence.py can count it; the more
descriptive "strong_uptrend"/"strong_downtrend" wording lives in `details`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, adjust=False).mean()


def compute(df: pd.DataFrame, period: int = 14, trend_threshold: float = 25.0) -> dict:
    if len(df) < period * 2:
        return {"value": None, "state": "neutral", "details": {"reason": "insufficient history"}}

    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr = _wilder(tr, period).replace(0, np.nan)
    plus_di = 100 * _wilder(pd.Series(plus_dm, index=df.index), period) / atr
    minus_di = 100 * _wilder(pd.Series(minus_dm, index=df.index), period) / atr
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx = _wilder(dx.fillna(0), period)

    last_adx = float(adx.iloc[-1])
    last_plus_di = float(plus_di.iloc[-1]) if pd.notna(plus_di.iloc[-1]) else 0.0
    last_minus_di = float(minus_di.iloc[-1]) if pd.notna(minus_di.iloc[-1]) else 0.0

    if last_adx > trend_threshold and last_plus_di > last_minus_di:
        state, trend_label = "bullish", "strong_uptrend"
    elif last_adx > trend_threshold and last_minus_di > last_plus_di:
        state, trend_label = "bearish", "strong_downtrend"
    else:
        state, trend_label = "neutral", "no_established_trend"

    return {
        "value": round(last_adx, 4),
        "state": state,
        "details": {
            "adx": round(last_adx, 4),
            "plus_di": round(last_plus_di, 4),
            "minus_di": round(last_minus_di, 4),
            "trend_threshold": trend_threshold,
            "trend_label": trend_label,
        },
    }


__all__ = ["compute"]
