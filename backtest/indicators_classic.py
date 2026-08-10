"""
مؤشرات متجهة (vectorized) عبر pandas — تُستخدم بالاستراتيجيات الكلاسيكية
الثلاث (Donchian، Bollinger+RSI، Bandtastic). بعكس محرك S&D، هذي المؤشرات
تُحسب دفعة وحدة على كامل السلسلة (لا حاجة لإعادة حساب من الصفر بكل شمعة).

ملاحظة منهجية: RSI/ATR هنا يستخدمان تمهيد وايلدر التقريبي عبر
pandas.ewm(alpha=1/period) بدل التكرار اليدوي المضبوط (بذرة = متوسط بسيط
لأول period قيمة ثم تكرار). الفرق يقتصر عملياً على أول ~period شمعة فقط
من آلاف الشموع بالسلسلة الكاملة — غير مؤثر على النتائج الإجمالية، ومطابق
لنفس أسلوب TA-Lib المستخدم أصلاً بمكتبة freqtrade (ta.RSI/ta.ATR/ta.MFI).
"""

from __future__ import annotations

import pandas as pd


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def wilder_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = true_range(df)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    raw_flow = typical_price * df["Volume"]
    tp_diff = typical_price.diff()

    pos_flow = raw_flow.where(tp_diff > 0, 0.0)
    neg_flow = raw_flow.where(tp_diff < 0, 0.0)

    pos_sum = pos_flow.rolling(period).sum()
    neg_sum = neg_flow.rolling(period).sum()

    money_flow_ratio = pos_sum / neg_sum
    return 100 - 100 / (1 + money_flow_ratio)


def bollinger_bands(price: pd.Series, window: int = 20, stds: float = 2.0) -> dict[str, pd.Series]:
    mid = price.rolling(window).mean()
    std = price.rolling(window).std()
    return {"lower": mid - stds * std, "mid": mid, "upper": mid + stds * std}


def typical_price(df: pd.DataFrame) -> pd.Series:
    return (df["High"] + df["Low"] + df["Close"]) / 3