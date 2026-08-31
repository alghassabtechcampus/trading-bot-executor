"""Ichimoku Cloud (Kumo), standard 9/26/52/26 periods.

Tenkan-sen   = (highest high + lowest low over 9)  / 2
Kijun-sen    = (highest high + lowest low over 26) / 2
Senkou Span A = (Tenkan + Kijun) / 2, plotted 26 periods ahead
Senkou Span B = (highest high + lowest low over 52) / 2, plotted 26 periods ahead
Cloud top/bottom at time t = max/min(Senkou A, Senkou B) as they were
CALCULATED 26 periods ago (i.e. shifted forward 26 bars), matching how the
cloud is actually plotted and avoiding any lookahead -- identical
convention to backtest/analysis/indicator_scan.py's ind_ichimoku.

State: bullish if price is above the cloud, bearish if below, neutral if
price is inside the cloud (no clear side).
"""

from __future__ import annotations

import pandas as pd


def compute(df: pd.DataFrame, tenkan_period: int = 9, kijun_period: int = 26,
            senkou_b_period: int = 52, displacement: int = 26) -> dict:
    min_bars = senkou_b_period + displacement
    if len(df) < min_bars:
        return {"value": None, "state": "neutral", "details": {"reason": "insufficient history"}}

    high, low, close = df["high"], df["low"], df["close"]
    tenkan = (high.rolling(tenkan_period).max() + low.rolling(tenkan_period).min()) / 2
    kijun = (high.rolling(kijun_period).max() + low.rolling(kijun_period).min()) / 2
    senkou_a = (tenkan + kijun) / 2
    senkou_b = (high.rolling(senkou_b_period).max() + low.rolling(senkou_b_period).min()) / 2

    cloud_top = pd.concat([senkou_a, senkou_b], axis=1).max(axis=1).shift(displacement)
    cloud_bottom = pd.concat([senkou_a, senkou_b], axis=1).min(axis=1).shift(displacement)

    last_close = float(close.iloc[-1])
    last_top = cloud_top.iloc[-1]
    last_bottom = cloud_bottom.iloc[-1]
    if pd.isna(last_top) or pd.isna(last_bottom):
        return {"value": None, "state": "neutral", "details": {"reason": "cloud not yet formed"}}
    last_top, last_bottom = float(last_top), float(last_bottom)

    if last_close > last_top:
        state = "bullish"
        distance_pct = (last_close - last_top) / last_top * 100
    elif last_close < last_bottom:
        state = "bearish"
        distance_pct = (last_bottom - last_close) / last_bottom * 100
    else:
        state = "neutral"
        distance_pct = 0.0

    return {
        "value": round(last_close, 8),
        "state": state,
        "details": {
            "cloud_top": round(last_top, 8),
            "cloud_bottom": round(last_bottom, 8),
            "distance_pct": round(distance_pct, 4),
            "tenkan": round(float(tenkan.iloc[-1]), 8),
            "kijun": round(float(kijun.iloc[-1]), 8),
        },
    }


__all__ = ["compute"]
