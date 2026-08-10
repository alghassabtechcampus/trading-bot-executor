"""
توليد إشارات الدخول/الخروج للاستراتيجيات الثلاث الكلاسيكية، حسب القرارات
التصميمية المتفق عليها (انظر ملخص المحادثة):

د) Donchian Channel Breakout — دخول: إغلاق يكسر أعلى High لآخر 20 شمعة
   سابقة. خروج طبيعي: إغلاق يكسر أدنى Low لآخر 10 شموع سابقة. وقف: 2×ATR(20).
هـ) Bollinger + RSI Mean Reversion — إشارة: Close<=lower_band و RSI<30.
   تأكيد: أول إغلاق يرجع فوق lower_band خلال 12 شمعة (وإلا تُهمَل الإشارة).
   وقف: 1.5×ATR(14). هدف: SMA20 وقت الدخول (ثابت).
و) Bandtastic (قيم افتراضية حرفية من الكود الرسمي) — دخول: Close<bb_lower1
   (باند 1 انحراف، على typical price). خروج طبيعي: MFI(14)>46 و
   Close>bb_upper2 (باند 2 انحراف). وقف: -34.5% (القيمة الأصلية بالكود).
"""

from __future__ import annotations

import pandas as pd

from indicators_classic import bollinger_bands, mfi, typical_price, wilder_atr, wilder_rsi


def donchian_signals(df: pd.DataFrame) -> dict:
    donchian_high_20 = df["High"].rolling(20).max().shift(1)
    donchian_low_10 = df["Low"].rolling(10).min().shift(1)
    atr20 = wilder_atr(df, 20)

    entry = (df["Close"] > donchian_high_20).fillna(False)
    exit_signal = (df["Close"] < donchian_low_10).fillna(False)
    stop = df["Close"] - 2 * atr20  # يُقيَّم فقط وقت الدخول الفعلي بمحرك المحاكاة

    return {"entry": entry, "exit_signal": exit_signal, "stop": stop, "target": None}


def bollinger_rsi_signals(df: pd.DataFrame, confirm_window: int = 12) -> dict:
    bands = bollinger_bands(df["Close"], window=20, stds=2.0)
    rsi14 = wilder_rsi(df["Close"], 14)
    atr14 = wilder_atr(df, 14)

    signal_candle = (df["Close"] <= bands["lower"]) & (rsi14 < 30)
    signal_candle = signal_candle.fillna(False).to_numpy()
    close = df["Close"].to_numpy()
    lower = bands["lower"].to_numpy()
    mid = bands["mid"].to_numpy()
    atr_arr = atr14.to_numpy()

    n = len(df)
    entry = [False] * n
    stop = [None] * n
    target = [None] * n

    pending_since = None  # index لشمعة الإشارة، بانتظار تأكيد

    for i in range(n):
        if pending_since is not None:
            waited = i - pending_since
            if waited > confirm_window:
                pending_since = None  # انتهت مهلة الانتظار - أُهملت الإشارة
            elif close[i] > lower[i]:
                entry[i] = True
                stop[i] = close[i] - 1.5 * atr_arr[i]
                target[i] = mid[i]
                pending_since = None
                continue  # لا تبدأ إشارة جديدة بنفس الشمعة اللي أكّدت السابقة

        if signal_candle[i]:
            pending_since = i

    return {
        "entry": pd.Series(entry, index=df.index),
        "exit_signal": None,  # الخروج فقط بالوقف أو الهدف أو max_hold
        "stop": pd.Series(stop, index=df.index, dtype="float64"),
        "target": pd.Series(target, index=df.index, dtype="float64"),
    }


def bandtastic_signals(df: pd.DataFrame) -> dict:
    tp = typical_price(df)
    bands1 = bollinger_bands(tp, window=20, stds=1.0)
    bands2 = bollinger_bands(tp, window=20, stds=2.0)
    mfi14 = mfi(df, 14)

    entry = (df["Close"] < bands1["lower"]).fillna(False)
    exit_signal = ((mfi14 > 46) & (df["Close"] > bands2["upper"])).fillna(False)
    stop = df["Close"] * (1 - 0.345)  # الوقف الأصلي -34.5% بالكود

    return {"entry": entry, "exit_signal": exit_signal, "stop": stop, "target": None}