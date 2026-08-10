"""
تأكيد الدخول عند لمس منطقة الطلب - "لا تدخل بمجرد لمس المنطقة".
لازم تأكيد واحد على الأقل من خمسة: شمعة ابتلاعية / Pin Bar / كسر قمة محلية /
كسر ترند صغير هابط / زيادة حجم واضحة.

نسخة مُوَرَّدة (vendored) من D:\\Dev\\sd-bot-tasi\\sd_engine\\confirmation.py، مع
تعديل إضافي واحد فقط: check_confirmation() صارت تقبل معاملات lookback
اختيارية (recent_high_lookback, micro_downtrend_lookback, volume_spike_lookback)
بدل القيم الافتراضية المُبَرمَجة بالأصل — حتى نقدر نُعاير النوافذ الزمنية
لفريم الساعة بالكريبتو من كود المستدعي مباشرة. القيم الافتراضية هنا مطابقة
تماماً للأصل (10, 10, 20)، فالسلوك بلا تمرير معاملات = مطابق حرفياً.
منطق الدوال الخمسة نفسها بلا أي تغيير.
"""

import pandas as pd


def is_bullish_engulfing(df: pd.DataFrame, pos: int) -> bool:
    if pos < 1:
        return False
    cur, prev = df.iloc[pos], df.iloc[pos - 1]
    cur_bullish = cur["Close"] > cur["Open"]
    prev_bearish = prev["Close"] < prev["Open"]
    engulfs = cur["Open"] <= prev["Close"] and cur["Close"] >= prev["Open"]
    return bool(cur_bullish and prev_bearish and engulfs)


def is_pin_bar(df: pd.DataFrame, pos: int, min_wick_to_body: float = 2.0) -> bool:
    candle = df.iloc[pos]
    body = abs(candle["Close"] - candle["Open"])
    full_range = candle["High"] - candle["Low"]
    if full_range <= 0:
        return False
    lower_wick = min(candle["Open"], candle["Close"]) - candle["Low"]
    close_near_high = (candle["Close"] - candle["Low"]) / full_range >= 0.66
    if body == 0:
        return bool(lower_wick > 0 and close_near_high)
    return bool(lower_wick >= body * min_wick_to_body and close_near_high)


def breaks_recent_high(df: pd.DataFrame, pos: int, lookback: int = 10) -> bool:
    if pos < 1:
        return False
    start = max(0, pos - lookback)
    prior_high = df["High"].iloc[start:pos].max()
    return bool(df["High"].iloc[pos] > prior_high)


def breaks_micro_downtrend(df: pd.DataFrame, pos: int, lookback: int = 10) -> bool:
    """
    يدوّر على قمتين محليتين متتاليتين هابطتين (كل وحدة أعلى نقطة بنافذة ±1 شمعة)
    بآخر lookback شمعة، ويتحقق هل شمعة اليوم كسرت أحدث وحدة منهم.
    """
    start = max(0, pos - lookback)
    window = df["High"].iloc[start:pos].values
    if len(window) < 5:
        return False

    local_highs = []
    for i in range(1, len(window) - 1):
        if window[i] > window[i - 1] and window[i] > window[i + 1]:
            local_highs.append(window[i])
    if len(local_highs) < 2:
        return False

    descending = local_highs[-1] < local_highs[-2]
    if not descending:
        return False

    return bool(df["High"].iloc[pos] > local_highs[-1])


def has_volume_spike(df: pd.DataFrame, pos: int, lookback: int = 20, multiplier: float = 1.3) -> bool:
    if "Volume" not in df.columns or pos < lookback:
        return False
    avg = df["Volume"].iloc[pos - lookback: pos].mean()
    if not avg or avg <= 0:
        return False
    return bool(df["Volume"].iloc[pos] >= avg * multiplier)


def check_confirmation(
    df: pd.DataFrame,
    pos: int,
    recent_high_lookback: int = 10,
    micro_downtrend_lookback: int = 10,
    volume_spike_lookback: int = 20,
) -> dict:
    """يفحص الأنواع الخمسة على شمعة الموضع pos، يرجع confirmed=True لو تحقق أي وحد."""
    checks = {
        "bullish_engulfing": is_bullish_engulfing(df, pos),
        "pin_bar": is_pin_bar(df, pos),
        "breaks_recent_high": breaks_recent_high(df, pos, lookback=recent_high_lookback),
        "breaks_micro_downtrend": breaks_micro_downtrend(df, pos, lookback=micro_downtrend_lookback),
        "volume_spike": has_volume_spike(df, pos, lookback=volume_spike_lookback),
    }
    confirmed_types = [k for k, v in checks.items() if v]
    return {"confirmed": len(confirmed_types) > 0, "types": confirmed_types, "checks": checks}