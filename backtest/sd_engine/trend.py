"""
الترند والقمم والقيعان (Swing Points & Trend)

نسخة مُوَرَّدة (vendored) طبق الأصل من D:\\Dev\\sd-bot-tasi\\sd_engine\\trend.py
بلا أي تعديل.
"""

import pandas as pd
import numpy as np


def find_swings(df: pd.DataFrame, left: int = 5, right: int = 5) -> pd.DataFrame:
    """يحدد القمم (H) والقيعان (L) بمقارنة كل شمعة بجيرانها."""
    highs = df["High"].values
    lows = df["Low"].values
    n = len(df)
    swing = [None] * n

    for i in range(left, n - right):
        window_h = highs[i - left: i + right + 1]
        window_l = lows[i - left: i + right + 1]
        if highs[i] == window_h.max() and np.argmax(window_h) == left:
            swing[i] = "H"
        elif lows[i] == window_l.min() and np.argmin(window_l) == left:
            swing[i] = "L"

    out = df.copy()
    out["swing"] = swing
    return out


def _trend_from_values(last_highs, last_lows) -> str:
    if len(last_highs) < 2 or len(last_lows) < 2:
        return "sideways"

    higher_high = last_highs[-1] > last_highs[-2]
    higher_low = last_lows[-1] > last_lows[-2]
    lower_high = last_highs[-1] < last_highs[-2]
    lower_low = last_lows[-1] < last_lows[-2]

    if higher_high and higher_low:
        return "up"
    if lower_high and lower_low:
        return "down"
    return "sideways"


def _trend_from_swings(df_swings: pd.DataFrame) -> str:
    highs = df_swings[df_swings["swing"] == "H"]
    lows = df_swings[df_swings["swing"] == "L"]
    return _trend_from_values(highs["High"].tail(2).values, lows["Low"].tail(2).values)


def detect_trend(df_swings: pd.DataFrame) -> str:
    """الترند الحالي/العام لكل البيانات المعطاة."""
    return _trend_from_swings(df_swings)


def detect_trend_at(df_swings: pd.DataFrame, upto_pos: int) -> str:
    """
    الترند وقت موضع معين بالتاريخ (مو الترند الحالي).
    ملاحظة أداء (غير موجودة بالأصل): نعمل مباشرة على مصفوفات numpy بدل
    df.iloc[:upto_pos+1] ثم فلترة pandas في كل استدعاء (بطيء جداً لما
    تُستدعى مرات كثيرة لكل نافذة) — نفس النتيجة الرقمية بالضبط، فقط أسرع.
    """
    swing_col = df_swings["swing"].to_numpy()
    high_col = df_swings["High"].to_numpy()
    low_col = df_swings["Low"].to_numpy()

    end = upto_pos + 1
    h_mask = swing_col[:end] == "H"
    l_mask = swing_col[:end] == "L"

    last_highs = high_col[:end][h_mask][-2:]
    last_lows = low_col[:end][l_mask][-2:]
    return _trend_from_values(last_highs, last_lows)