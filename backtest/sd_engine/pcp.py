"""
PCP - دخول ارتداد مع تأكيد (Retest + Confirmation) - نسخة شراء فقط

نسخة مُوَرَّدة (vendored) من D:\\Dev\\sd-bot-tasi\\sd_engine\\pcp.py، مع تعديل
إضافي واحد فقط: detect_pcp() صارت تمرر معاملات lookback الجديدة
(recent_high_lookback, micro_downtrend_lookback, volume_spike_lookback) لـ
check_confirmation() بدل الاعتماد على قيمها الافتراضية المُبَرمَجة. القيم
الافتراضية هنا مطابقة تماماً للأصل، فالسلوك بلا تمرير معاملات = مطابق حرفياً.

من الملف:
  "لا يمكن الدخول عكس الترند مطلقًا، لا بد تكون مع الاتجاه"
  "أن يكون الربح الضعفين كأقل حد أو ثلاثة أضعاف المنطقة"
ملاحظة تصميم مهمة: "لا تدخل بمجرد لمس المنطقة" - لازم تأكيد واحد على الأقل
(انظر confirmation.py) قبل ما تصير الإشارة صالحة.
"""

import pandas as pd

from sd_engine.zones import count_zone_touches, compute_zone_setup
from sd_engine.confirmation import check_confirmation


def detect_pcp(df: pd.DataFrame, zone: dict, trend: str, all_zones: list,
               atr_value: float | None = None, min_rr: float = 2.0,
               retest_window_days: int = 20,
               recent_high_lookback: int = 10,
               micro_downtrend_lookback: int = 10,
               volume_spike_lookback: int = 20) -> dict | None:
    """
    zone: منطقة طلب (من find_zones).
    trend: الترند وقت تكوّن المنطقة (لازم يكون 'up' - مع الترند فقط).
    all_zones: كل المناطق (تُستخدم لإيجاد أقرب منطقة عرض كهدف حقيقي).
    atr_value: ATR وقت تكوّن المنطقة (لحساب الوقف) - fallback تلقائي لو غير متوفر.
    min_rr: أقل نسبة عائد/مخاطرة مقبولة (الملف يقول ضعفين كحد أدنى).
    retest_window_days: أقصى مدة انتظار لأول ارتداد قبل ما تُعتبر المنطقة فاتها القطار.
    """
    if zone["type"] != "demand" or trend != "up":
        return None

    today_pos = len(df) - 1
    if today_pos <= zone["end_pos"]:
        return None
    if today_pos - zone["end_pos"] > retest_window_days:
        return None  # فاتها القطار - ما رجعت السعر تزورها بالوقت المعقول

    prior = df.iloc[: today_pos]
    if count_zone_touches(zone, prior) > 0:
        return None  # لمسة سابقة موجودة - فرصة الارتداد الأولى استُهلكت

    today = df.iloc[-1]
    if today["Low"] > zone["top"]:
        return None  # ما لمس المنطقة اليوم
    if today["Close"] < zone["bottom"]:
        return None  # كسر تحت المنطقة - فشل الارتداد
    if today["Open"] > zone["top"] * 1.01:
        return None  # فجوة افتتاح كبيرة - ارتداد غير نظيف (دخول متأخر)

    confirmation = check_confirmation(
        df, today_pos,
        recent_high_lookback=recent_high_lookback,
        micro_downtrend_lookback=micro_downtrend_lookback,
        volume_spike_lookback=volume_spike_lookback,
    )
    if not confirmation["confirmed"]:
        return None  # "لا تدخل بمجرد لمس المنطقة" - محتاج تأكيد واحد على الأقل

    setup = compute_zone_setup(zone, atr_value, all_zones)
    if setup is None or setup["rr"] < min_rr:
        return None

    return {
        "signal_type": "PCP",
        "zone": zone,
        "confirmation_types": confirmation["types"],
        "entry": round(float(setup["entry"]), 8),
        "stop": round(float(setup["stop"]), 8),
        "target": round(float(setup["target"]), 8),
        "risk": round(float(setup["risk"]), 8),
        "rr": round(float(setup["rr"]), 2),
    }