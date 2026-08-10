"""
مؤشرات مساعدة: ATR (لحساب الوقف) ونسبة حجم الانطلاق (لتقييم جودة المنطقة).

نسخة مُوَرَّدة (vendored) طبق الأصل من D:\\Dev\\sd-bot-tasi\\sd_engine\\indicators.py
بلا أي تعديل.
"""

import pandas as pd


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR القياسي: متوسط متحرك لـ True Range عبر period شمعة."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(window=period, min_periods=period).mean()


def breakout_volume_ratio(df: pd.DataFrame, zone: dict, lookback: int = 20, breakout_span: int = 3) -> float | None:
    """
    نسبة حجم شموع الخروج من المنطقة (حتى breakout_span شمعة بعد نهايتها) إلى
    متوسط حجم آخر lookback شمعة قبل بداية المنطقة. None لو البيانات غير كافية.
    """
    if "Volume" not in df.columns:
        return None

    start = zone["start_pos"]
    if start < lookback:
        return None
    avg_before = df["Volume"].iloc[start - lookback: start].mean()
    if not avg_before or avg_before <= 0:
        return None

    breakout_start = zone["end_pos"] + 1
    breakout_end = min(breakout_start + breakout_span, len(df))
    if breakout_start >= breakout_end:
        return None
    breakout_vol = df["Volume"].iloc[breakout_start:breakout_end].mean()
    if pd.isna(breakout_vol):
        return None

    return float(breakout_vol / avg_before)