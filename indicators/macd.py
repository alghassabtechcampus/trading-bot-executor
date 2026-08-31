"""MACD (Moving Average Convergence Divergence), standard 12/26/9.

MACD line = EMA(close, 12) - EMA(close, 26)
Signal line = EMA(MACD line, 9)
Histogram = MACD line - Signal line

State: bullish if MACD is above its signal line, bearish if below. A
`fresh_cross` flag additionally reports whether the cross happened on the
LATEST bar (vs. having been in that state for a while) -- useful for a
dashboard to highlight a just-occurred event separately from a standing
state.
"""

from __future__ import annotations

import pandas as pd


def compute(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal_period: int = 9) -> dict:
    if len(df) < slow + signal_period:
        return {"value": None, "state": "neutral", "details": {"reason": "insufficient history"}}

    close = df["close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line

    last_macd = float(macd_line.iloc[-1])
    last_signal = float(signal_line.iloc[-1])
    last_hist = float(histogram.iloc[-1])
    state = "bullish" if last_macd > last_signal else ("bearish" if last_macd < last_signal else "neutral")

    fresh_cross = False
    if len(df) >= 2:
        prev_macd, prev_signal = float(macd_line.iloc[-2]), float(signal_line.iloc[-2])
        was_above = prev_macd > prev_signal
        is_above = last_macd > last_signal
        fresh_cross = was_above != is_above

    return {
        "value": round(last_macd, 8),
        "state": state,
        "details": {
            "macd_line": round(last_macd, 8),
            "signal_line": round(last_signal, 8),
            "histogram": round(last_hist, 8),
            "fresh_cross": fresh_cross,
        },
    }


__all__ = ["compute"]
