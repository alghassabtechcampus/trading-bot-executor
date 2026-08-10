"""
اكتشاف مناطق الطلب (Demand Zones) وتقييم قوتها.

نسخة مُوَرَّدة (vendored) من D:\\Dev\\sd-bot-tasi\\sd_engine\\zones.py، مع تعديل
إضافي واحد فقط: find_zones() صارت تقبل lookahead_bars و min_opposite_gap
كمعاملين اختياريين (بدل ثابت 15 وثابت وحدة MIN_OPPOSITE_ZONE_GAP=8 المُبَرمَجين
بالأصل) — حتى نقدر نُعاير النوافذ الزمنية لفريم الساعة بالكريبتو (×24 عن
شموع TASI اليومية) من كود المستدعي مباشرة، بدون لمس منطق الدالة نفسه.
القيم الافتراضية هنا مطابقة تماماً للأصل، فالسلوك بلا تمرير معاملات = مطابق حرفياً.

من الملف - جدول "اثبات المناطق":
  - الكاسرة للترند فقط                    -> أضعف المناطق
  - الكاسرة لمنطقة مقابلة (دمياند تكسر سبلاي) -> متوسطة القوة
  - الكاسرة للترند + منطقة مقابلة معًا        -> أقوى المناطق
  - كل منطقة تعمل لو (Low) تاريخي جديد        -> طلب جديد بحد ذاته

من الملف - "تقييم المناطق":
  - قلة عدد الشموع (<=6) أفضل
  - عدم زيارة المنطقة من قبل
  - خروج بمقدار ضعف حجم المنطقة أو أكثر
  - شمعة دوجي وحيدة = تحتاج تأكيد
  - شكل الدرج/السلم = غير صالحة نهائيًا
  - شمعة وحيدة = تضعف المنطقة

ملاحظة: نركز على مناطق الطلب (Demand) فقط - بدون بيع على المكشوف.
"""

import numpy as np
import pandas as pd

from sd_engine.indicators import breakout_volume_ratio


# ---------------------------------------------------------------
# 1) اكتشاف المناطق (Rally-Base-Rally للطلب / Drop-Base-Drop للعرض)
# ---------------------------------------------------------------
MIN_OPPOSITE_ZONE_GAP = 8  # شموع - القيمة الافتراضية الأصلية (شموع TASI اليومية)
DEFAULT_LOOKAHEAD_BARS = 15  # القيمة الافتراضية الأصلية


def _too_close_to_opposite(new_zone: dict, opposite_zone: dict | None, min_gap: int) -> bool:
    """هل new_zone تكوّنت خلال min_gap شمعة من آخر منطقة معاكسة؟"""
    if opposite_zone is None:
        return False
    gap = new_zone["start_pos"] - opposite_zone["end_pos"]
    return gap < min_gap


def find_zones(
    df_swings: pd.DataFrame,
    min_move_pct: float = 3.5,
    lookahead_bars: int = DEFAULT_LOOKAHEAD_BARS,
    min_opposite_gap: int = MIN_OPPOSITE_ZONE_GAP,
) -> list:
    """
    يمشي على القيعان والقمم، ولما يلقى قاع تلاه تحرك قوي (>= min_move_pct%)
    خلال lookahead_bars شمعة، يعتبر شموع الأساس عنده "منطقة طلب". نفس الشي
    بالعكس للقمم (منطقة عرض). تُستبعد منطقة تتكوّن خلال min_opposite_gap شمعة
    من آخر منطقة معاكسة - غالبًا نفس نطاق التذبذب الضيق منعكس بحافتين، مو
    منطقتين مستقلتين فعلًا.

    ملاحظة أداء (غير موجودة بالأصل): الحلقة الداخلية تستخدم مصفوفات numpy
    مباشرة بدل df.iloc[] المتكرر (بطيء جداً على نوافذ كبيرة/عدد قمم-قيعان
    كثير) — نفس النتائج الرقمية بالضبط، فقط أسرع.
    """
    zones = []
    last_zone_by_type = {"demand": None, "supply": None}
    swing_col = df_swings["swing"].to_numpy()
    highs = df_swings["High"].to_numpy()
    lows = df_swings["Low"].to_numpy()
    opens = df_swings["Open"].to_numpy()
    closes = df_swings["Close"].to_numpy()
    index = df_swings.index
    n = len(df_swings)

    swing_positions = [i for i in range(n) if swing_col[i] is not None]

    for pos in swing_positions:
        kind = swing_col[pos]
        lookahead_len = min(lookahead_bars, n - pos)
        if lookahead_len < 3:
            continue

        if kind == "L":
            row_low = lows[pos]
            move_pct = (highs[pos: pos + lookahead_len].max() - row_low) / row_low * 100
            if move_pct >= min_move_pct:
                row_high = highs[pos]
                base_end = pos
                for j in range(pos, min(pos + 6, n)):
                    if closes[j] > row_high:
                        break
                    base_end = j
                zone_top = max(closes[pos: base_end + 1].max(), opens[pos: base_end + 1].max())
                zone_bottom = lows[pos: base_end + 1].min()
                new_zone = {
                    "type": "demand",
                    "top": float(zone_top),
                    "bottom": float(zone_bottom),
                    "start": index[pos],
                    "end": index[base_end],
                    "start_pos": pos,
                    "end_pos": base_end,
                    "n_candles": base_end - pos + 1,
                    "move_pct": round(move_pct, 2),
                }
                if not _too_close_to_opposite(new_zone, last_zone_by_type["supply"], min_opposite_gap):
                    zones.append(new_zone)
                    last_zone_by_type["demand"] = new_zone

        elif kind == "H":
            row_high = highs[pos]
            move_pct = (row_high - lows[pos: pos + lookahead_len].min()) / row_high * 100
            if move_pct >= min_move_pct:
                row_low = lows[pos]
                base_end = pos
                for j in range(pos, min(pos + 6, n)):
                    if closes[j] < row_low:
                        break
                    base_end = j
                zone_bottom = min(closes[pos: base_end + 1].min(), opens[pos: base_end + 1].min())
                zone_top = highs[pos: base_end + 1].max()
                new_zone = {
                    "type": "supply",
                    "top": float(zone_top),
                    "bottom": float(zone_bottom),
                    "start": index[pos],
                    "end": index[base_end],
                    "start_pos": pos,
                    "end_pos": base_end,
                    "n_candles": base_end - pos + 1,
                    "move_pct": round(move_pct, 2),
                }
                if not _too_close_to_opposite(new_zone, last_zone_by_type["demand"], min_opposite_gap):
                    zones.append(new_zone)
                    last_zone_by_type["supply"] = new_zone

    return sorted(zones, key=lambda z: z["start_pos"])


# ---------------------------------------------------------------
# 2) صيغة الصفقة (entry/stop/target/rr) - مصدر وحيد يُستخدم بالتقييم والتنفيذ
# ---------------------------------------------------------------
def nearest_supply_target(entry: float, all_zones: list) -> float | None:
    """أقرب منطقة عرض فوق الدخول بـ 1% على الأقل (نستخدم قاع المنطقة كهدف)."""
    min_target = entry * 1.01
    candidates = [z["bottom"] for z in all_zones if z["type"] == "supply" and z["bottom"] >= min_target]
    if not candidates:
        return None
    return min(candidates)


def compute_zone_setup(zone: dict, atr_value: float | None, all_zones: list,
                        atr_mult: float = 0.5, fallback_mult: float = 2.5) -> dict | None:
    """
    entry = top، stop = bottom - (ATR*0.5) - أو 10% من ارتفاع المنطقة لو ATR
    غير متوفر (بداية البيانات قبل اكتمال فترة التهيئة). target/rr كالمعتاد.
    """
    entry = zone["top"]
    zone_height = zone["top"] - zone["bottom"]
    margin = atr_value * atr_mult if atr_value and not pd.isna(atr_value) else zone_height * 0.1
    stop = zone["bottom"] - margin
    risk = entry - stop
    if risk <= 0:
        return None

    target = nearest_supply_target(entry, all_zones)
    if target is None:
        target = entry + risk * fallback_mult
    rr = (target - entry) / risk

    return {"entry": entry, "stop": stop, "target": target, "risk": risk, "rr": rr}


# ---------------------------------------------------------------
# 3) شروط منطقة الطلب - نظام تسجيل 6 نقاط (نحتفظ فقط بمناطق 5-6)
# ---------------------------------------------------------------
def evaluate_zone_quality(zone: dict, df: pd.DataFrame, all_zones: list,
                           atr_value: float | None = None,
                           volume_lookback: int = 20, min_volume_ratio: float = 1.3,
                           resistance_height_mult: float = 1.0) -> dict:
    score = 0
    reasons = []

    # أ) انطلاق قوي: جسم شمعة الخروج كبير نسبيًا + move_pct قوي
    breakout_pos = zone["end_pos"] + 1
    strong_launch = False
    if breakout_pos < len(df):
        candle = df.iloc[breakout_pos]
        candle_range = candle["High"] - candle["Low"]
        body_ratio = abs(candle["Close"] - candle["Open"]) / candle_range if candle_range > 0 else 0
        strong_launch = body_ratio >= 0.6 and zone["move_pct"] >= 3.0
    if strong_launch:
        score += 1
        reasons.append("انطلاق قوي (جسم شمعة كبير + move_pct>=3%)")
    else:
        reasons.append("انطلاق ضعيف")

    # ب) Fresh Zone - نستبعد اليوم الحالي (آخر صف بـdf) من فحص اللمسات
    prior = df.iloc[:-1]
    if count_zone_touches(zone, prior) == 0:
        score += 1
        reasons.append("Fresh Zone - صفر لمسات")
    else:
        reasons.append("سبق أن زارها السعر")

    # ج) 1-6 شموع
    if 1 <= zone["n_candles"] <= 6:
        score += 1
        reasons.append("عدد شموع مناسب (1-6)")
    else:
        reasons.append("عدد شموع أكثر من 6")

    # د) حجم انطلاق مرتفع
    vol_ratio = breakout_volume_ratio(zone=zone, df=df, lookback=volume_lookback)
    if vol_ratio is not None and vol_ratio >= min_volume_ratio:
        score += 1
        reasons.append(f"حجم انطلاق مرتفع (x{vol_ratio:.2f} المتوسط)")
    else:
        reasons.append("حجم انطلاق غير كافٍ" if vol_ratio is not None else "بيانات حجم غير كافية")

    # هـ) لا توجد مقاومة قريبة تخنق الحركة
    zone_height = zone["top"] - zone["bottom"]
    near_resistance = any(
        other["type"] == "supply" and zone["top"] < other["bottom"] < zone["top"] + zone_height * resistance_height_mult
        for other in all_zones
    )
    if not near_resistance:
        score += 1
        reasons.append("لا توجد مقاومة قريبة")
    else:
        reasons.append("مقاومة قريبة تخنق الحركة")

    # و) R:R >= 2 (بنفس صيغة compute_zone_setup الفعلية)
    setup = compute_zone_setup(zone, atr_value, all_zones)
    if setup and setup["rr"] >= 2.0:
        score += 1
        reasons.append(f"R:R >= 2 (فعلي {setup['rr']:.2f})")
    else:
        reasons.append("R:R أقل من 2")

    return {"score": score, "max_score": 6, "reasons": reasons}


# ---------------------------------------------------------------
# 3) اثبات المناطق - تصنيف القوة (غير مستخدَمة بمسار scanner.py الحي، لكن
#    مُبقاة هنا طبق الأصل لاكتمال النسخة الموَرَّدة)
# ---------------------------------------------------------------
def classify_zone_strength(zone: dict, df_swings: pd.DataFrame, all_zones: list) -> dict:
    if zone["type"] != "demand":
        return {"strength": 0, "label": "not_applicable", "reasons": []}

    reasons = []

    trend_before = _trend_before_zone(df_swings, zone)
    trend_breaking = trend_before == "down"
    if trend_breaking:
        reasons.append("كسرت ترند هابط")

    opposite_breaking = _breaks_opposite_zone(zone, all_zones)
    if opposite_breaking:
        reasons.append("كسرت منطقة عرض سابقة بنفس النطاق")

    if trend_breaking and opposite_breaking:
        return {"strength": 3, "label": "قوية (كاسرة للترند ومنطقة مقابلة)", "reasons": reasons}
    if opposite_breaking:
        return {"strength": 2, "label": "متوسطة (كاسرة لمنطقة مقابلة)", "reasons": reasons}
    if trend_breaking:
        return {"strength": 1, "label": "ضعيفة (كاسرة للترند فقط)", "reasons": reasons}
    return {"strength": 0, "label": "غير مثبتة", "reasons": ["لم تكسر ترند ولا منطقة مقابلة"]}


def _trend_before_zone(df_swings: pd.DataFrame, zone: dict) -> str:
    from sd_engine.trend import detect_trend_at
    pos = max(0, zone["start_pos"] - 1)
    return detect_trend_at(df_swings, pos)


def _breaks_opposite_zone(zone: dict, all_zones: list) -> bool:
    for other in all_zones:
        if other["type"] != "supply":
            continue
        if other["start_pos"] >= zone["start_pos"]:
            continue
        overlap = not (zone["top"] < other["bottom"] or zone["bottom"] > other["top"])
        if overlap:
            return True
    return False


def is_historical_low(zone: dict, df: pd.DataFrame) -> bool:
    """كل منطقة تعمل لو (قاع) تاريخي جديد تعتبر طلب جديد بحد ذاته - من الملف."""
    if zone["type"] != "demand":
        return False
    data_before = df.loc[: zone["start"]]
    return zone["bottom"] <= data_before["Low"].min()


# ---------------------------------------------------------------
# 4) Fresh Zone - عدّاد اللمسات وشكل الدرج/السلم
# ---------------------------------------------------------------
def count_zone_touches(zone: dict, df: pd.DataFrame) -> int:
    after = df.iloc[zone["end_pos"] + 1:]
    if after.empty:
        return 0

    if zone["type"] == "demand":
        in_zone = (after["Low"].to_numpy() <= zone["top"])
    else:
        in_zone = (after["High"].to_numpy() >= zone["bottom"])

    prev_in_zone = np.concatenate(([False], in_zone[:-1]))
    return int(np.sum(in_zone & ~prev_in_zone))


def is_staircase_shape(zone: dict, df: pd.DataFrame) -> bool:
    base = df.loc[zone["start"]: zone["end"]]
    if len(base) < 2:
        return False

    rows = base[["High", "Low"]].to_dict("records")
    non_overlapping = 0
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        overlaps = not (cur["High"] < prev["Low"] or cur["Low"] > prev["High"])
        if not overlaps:
            non_overlapping += 1

    return non_overlapping >= (len(rows) - 1) * 0.5