"""
نسخة Python طبق الأصل من منطق القرار بعقدة "Code in JavaScript1"
(workflow "بوت العملات تجريبي") بعد كل التعديلات الأخيرة:
معادلة Score المتدرجة، فلتر ATR%>=0.3، بوابة Volume>=2.0، عتبة 35،
سقف الوقف 0.7% (والهدف تلقائياً حتى 1.4%).

قيد معروف (مذكور بالتقرير النهائي): Order Book وSpread غير متوفرين
تاريخياً، فهما دائماً True هنا في كلا النسختين.

انحراف واحد متعمد عن الكود الحي: الكود الحي يحذف آخر شمعة من نافذة
الـ200 لأنها قد تكون لا تزال مفتوحة (بيانات لحظية حية). هنا الشموع
التاريخية كلها مغلقة فعلياً، فلا داعي لهذا الحذف الدفاعي — نستخدم
آخر شمعة بالنافذة مباشرة كسعر التحليل.
"""

from __future__ import annotations

from indicators import calc_atr, calc_ema, calc_rsi

ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 1.5
FEE_MARGIN_PERCENT = 0.2
MAX_STOP_PERCENT = 0.7
MIN_ATR_PERCENT = 0.3

SCORE_BUY_THRESHOLD = 35
SCORE_WATCH_THRESHOLD = 30
VOLUME_RATIO_GATE = 2.0

BTC_EMA200_WINDOW = 500
BTC_RSI_MIN = 45


def _volume_score(volume_ratio: float) -> float:
    if volume_ratio < 1.0:
        return 5.0
    if volume_ratio < 2.0:
        return 5.0 + (volume_ratio - 1.0) * 10.0
    if volume_ratio < 4.0:
        return 15.0 + (volume_ratio - 2.0) * 17.5
    return 50.0


def evaluate_signal(window: list[dict]) -> dict | None:
    """
    window: قائمة شموع مرتّبة زمنياً تصاعدياً، تنتهي بالشمعة الحالية
    (حتى 200 شمعة). يرجّع None لو البيانات غير كافية للتحليل.
    """
    if len(window) < 60:
        return None

    opens = [c["open"] for c in window]
    highs = [c["high"] for c in window]
    lows = [c["low"] for c in window]
    closes = [c["close"] for c in window]
    volumes = [c["volume"] for c in window]

    if len(volumes) < 21:
        return None

    current_price = closes[-1]
    current_open = opens[-1]
    previous_close = closes[-2]

    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    rsi = calc_rsi(closes, 14)
    previous_rsi = calc_rsi(closes[:-1], 14)
    atr14 = calc_atr(highs, lows, closes, ATR_PERIOD)

    if None in (ema20, ema50, rsi, previous_rsi, atr14) or current_price <= 0:
        return None

    up_trend = ema20 > ema50

    distance_from_ema20 = abs(current_price - ema20) / current_price * 100
    near_ema20 = distance_from_ema20 <= 0.35

    rsi_stable_or_rising = rsi >= previous_rsi - 1
    rsi_good = 42 <= rsi <= 62

    bullish_candle = current_price > current_open
    close_above_previous = current_price > previous_close

    previous_volumes = volumes[-21:-1]
    average_volume = sum(previous_volumes) / len(previous_volumes)
    current_volume = volumes[-1]
    volume_ratio = current_volume / average_volume if average_volume > 0 else 0.0

    # Order Book / Spread: بيانات تاريخية غير متوفرة — نفترضها محقّقة دائماً
    order_book_good = True
    spread_good = True

    atr_percent = (atr14 / current_price) * 100

    score = _volume_score(volume_ratio)
    if near_ema20:
        score += 10
    if rsi_good:
        score += 10
    if rsi_stable_or_rising:
        score += 5
    if bullish_candle:
        score += 10
    if close_above_previous:
        score += 5

    action = "IGNORE"

    if (
        atr_percent >= MIN_ATR_PERCENT
        and score >= SCORE_BUY_THRESHOLD
        and up_trend
        and spread_good
        and order_book_good
        and volume_ratio >= VOLUME_RATIO_GATE
    ):
        action = "BUY_NOW"
    elif atr_percent >= MIN_ATR_PERCENT and score >= SCORE_WATCH_THRESHOLD:
        action = "WATCH"

    stop_loss = None
    take_profit = None

    if action == "BUY_NOW":
        atr_stop_distance = atr14 * ATR_STOP_MULTIPLIER
        min_stop_distance = current_price * (FEE_MARGIN_PERCENT / 100)
        max_stop_distance = current_price * (MAX_STOP_PERCENT / 100)
        stop_distance = min(max(atr_stop_distance, min_stop_distance), max_stop_distance)
        take_profit_distance = stop_distance * 2

        stop_loss = current_price - stop_distance
        take_profit = current_price + take_profit_distance

    return {
        "action": action,
        "score": score,
        "price": current_price,
        "up_trend": up_trend,
        "volume_ratio": volume_ratio,
        "atr_percent": atr_percent,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }


def evaluate_btc_filter(btc_window: list[dict], btc_window_long: list[dict]) -> bool:
    """
    فلتر السوق لنسخة ب: BTC فوق EMA200 و EMA20>EMA50 و RSI>45.
    EMA200 تُحسب على نافذة أكبر (btc_window_long، 500 شمعة) حتى تكون
    قيمة EMA حقيقية مُنعَّمة، لا مجرد متوسط بسيط بنافذة 200.
    """
    if len(btc_window) < 60 or len(btc_window_long) < BTC_EMA200_WINDOW:
        return False

    closes = [c["close"] for c in btc_window]
    closes_long = [c["close"] for c in btc_window_long]

    current_price = closes[-1]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes_long, 200)
    rsi = calc_rsi(closes, 14)

    if None in (ema20, ema50, ema200, rsi):
        return False

    return current_price > ema200 and ema20 > ema50 and rsi > BTC_RSI_MIN