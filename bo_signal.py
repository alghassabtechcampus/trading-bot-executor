"""
bo_signal.py — رياضيات استراتيجية "الاختراق + السوق صاعد" (breakout_btc) والحَكَم
==================================================================================
مستخرجة من دورة أسهم بدر، ومختبرة في المحادثة على 10 عملات Bybit spot، 4 ساعات،
2023-01 → 2026-10 (رسوم 0.15% لكل جهة). القواعد هنا هي نفسها حرفياً:

  القمة/القاع  : شمعة أعلى (أدنى) من شمعتين يمين وشمعتين يسار (K=2). تُعرف فقط
                 بعد إغلاق الشمعتين اللتين بعدها — لا نظر للمستقبل.
  الدخول       : إغلاق شمعة 4 ساعات فوق آخر قمة مؤكدة، والشمعة السابقة أغلقت
                 تحتها أو عندها (اختراق طازج)
                 + فوليوم الشمعة > 1.5 × متوسط آخر 20 شمعة (يشمل الشمعة نفسها)
                 + شمعة صاعدة يغلق نصفها العلوي: close > open و
                   (close − low) ≥ 0.5 × (high − low)
                 + الإغلاق فوق آخر قاع مؤكد (وإلا لا يوجد وقف منطقي)
                 + الحَكَم: البيتكوين صاعد على اليومي
                 + نفس القمة لا تُستخدم لدخولين
  الوقف        : آخر قاع مؤكد وقت الدخول. يُرفع إلى كل قاع مؤكد أعلى منه يتكوّن
                 بعد الدخول (pivot.ts > entry_bar_ts − K شموع).
  الخروج       : إغلاق شمعة 4 ساعات تحت الوقف. لا هدف ولا خروج زمني.

الحَكَم (regime): على شموع BTCUSDT اليومية — آخر قاعين مؤكدين صاعدين (الأحدث
أعلى) وإغلاق اليوم فوق آخر قاع. شمعة 4 ساعات تفتح في t ترى فقط الأيام التي
أُغلقت عند t أو قبلها (نفس الباك-تيست، وهو اختيار محافظ).

نتيجة الباك-تيست (للمقارنة مع الحي): ~529 صفقة على 10 عملات في 46 شهراً،
39% رابحة، متوسط +1.56% للصفقة بعد الرسوم، عامل ربح 1.68. 2025–2026: +7%.

لا pandas هنا عمداً: قوائم بسيطة تكفي، والدوال نقية فتُختبر بشموع مصطنعة.
"""

from __future__ import annotations

from typing import Any

K = 2                      # شموع على كل جانب للقمة/القاع
VOL_WINDOW = 20            # متوسط الفوليوم
VOL_MULT = 1.5             # الفوليوم المطلوب نسبةً إلى المتوسط
BODY_FRACTION = 0.5        # الإغلاق في النصف العلوي من مدى الشمعة
BAR_MS = 4 * 3600 * 1000   # فريم الدخول
DAY_MS = 24 * 3600 * 1000  # فريم الحَكَم

# عملات الباك-تيست ناقص LINK (ما ينباع عبر الـ API على الحساب الحقيقي).
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
           "LTCUSDT", "SOLUSDT", "DOGEUSDT", "AVAXUSDT"]
REGIME_SYMBOL = "BTCUSDT"


# ============================================================
# الشموع
# ============================================================

def parse_klines(rows: list[list[Any]], interval_ms: int, now_ms: int) -> list[dict[str, float]]:
    """صفوف Bybit (الأحدث أولاً) → شموع مغلقة فقط، الأقدم أولاً، بلا تكرار."""
    seen: dict[int, dict[str, float]] = {}
    for row in rows or []:
        try:
            t = int(row[0])
            bar = {"t": t, "o": float(row[1]), "h": float(row[2]), "l": float(row[3]),
                   "c": float(row[4]), "v": float(row[5])}
        except (IndexError, TypeError, ValueError):
            continue
        if t + interval_ms <= now_ms:          # الشمعة المفتوحة تُرمى
            seen[t] = bar
    return [seen[t] for t in sorted(seen)]


def pivots(bars: list[dict[str, float]], k: int = K) -> tuple[list[dict], list[dict]]:
    """القمم والقيعان. conf = فهرس الشمعة التي تُعرف عندها (i + k)."""
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        h, l = bars[i]["h"], bars[i]["l"]
        if all(h > bars[i - j]["h"] and h > bars[i + j]["h"] for j in range(1, k + 1)):
            highs.append({"i": i, "t": bars[i]["t"], "p": h, "conf": i + k})
        if all(l < bars[i - j]["l"] and l < bars[i + j]["l"] for j in range(1, k + 1)):
            lows.append({"i": i, "t": bars[i]["t"], "p": l, "conf": i + k})
    return highs, lows


def _confirmed(points: list[dict], at: int) -> list[dict]:
    return [p for p in points if p["conf"] <= at]


# ============================================================
# الحَكَم: هل البيتكوين صاعد على اليومي؟
# ============================================================

def regime_flags(daily: list[dict[str, float]], k: int = K) -> list[dict[str, Any]]:
    """لكل يوم مغلق: هل الاتجاه صاعد بنهايته؟ (tc = لحظة إغلاق اليوم)."""
    _, lows = pivots(daily, k)
    out = []
    for i, bar in enumerate(daily):
        conf = _confirmed(lows, i)
        up = (len(conf) >= 2 and conf[-1]["p"] > conf[-2]["p"]
              and bar["c"] > conf[-1]["p"])
        out.append({"tc": bar["t"] + DAY_MS, "up": up,
                    "last_low": conf[-1]["p"] if conf else None,
                    "prev_low": conf[-2]["p"] if len(conf) >= 2 else None,
                    "close": bar["c"], "day": bar["t"]})
    return out


def regime_at(flags: list[dict[str, Any]], t_ms: int) -> dict[str, Any] | None:
    """حالة آخر يوم أُغلق عند t_ms أو قبله. None = لا بيانات كافية."""
    best = None
    for f in flags:
        if f["tc"] <= t_ms:
            best = f
        else:
            break
    return best


# ============================================================
# إشارة الدخول
# ============================================================

def _vol_avg(bars: list[dict[str, float]], i: int) -> float | None:
    if i < VOL_WINDOW - 1:
        return None
    return sum(b["v"] for b in bars[i - VOL_WINDOW + 1:i + 1]) / VOL_WINDOW


def evaluate(bars: list[dict[str, float]], i: int | None = None) -> dict[str, Any]:
    """يقيّم شمعة مغلقة واحدة (افتراضياً الأخيرة) بدون الحَكَم.

    يرجّع signal=True فقط إذا تحققت كل شروط السعر. الحَكَم و"القمة المستخدمة"
    يفحصهما breakout.py لأنهما يحتاجان حالة خارج الشموع.
    """
    n = len(bars)
    i = n - 1 if i is None else i
    out: dict[str, Any] = {"signal": False, "reason": None}
    if i < 1 or i >= n:
        out["reason"] = "not_enough_bars"
        return out
    # _confirmed(…, i) يضمن أن كل قمة/قاع مستخدم عُرف عند الشمعة i أو قبلها.
    highs, lows = pivots(bars, K)
    ch, cl = _confirmed(highs, i), _confirmed(lows, i)
    bar, prev = bars[i], bars[i - 1]
    out.update({"bar_t": bar["t"], "close": bar["c"]})
    if len(ch) < 2 or not cl:
        out["reason"] = "no_structure"
        return out
    h1, pl = ch[-1], cl[-1]
    out.update({"pivot_high": h1["p"], "pivot_high_t": h1["t"],
                "stop": pl["p"], "stop_t": pl["t"]})
    va = _vol_avg(bars, i)
    if va is None:
        out["reason"] = "not_enough_bars"
        return out
    out["vol_ratio"] = bar["v"] / va if va > 0 else 0.0
    if not (bar["c"] > h1["p"] and prev["c"] <= h1["p"]):
        out["reason"] = "no_breakout"
        return out
    if not bar["v"] > VOL_MULT * va:
        out["reason"] = "low_volume"
        return out
    rng = bar["h"] - bar["l"]
    if not (bar["c"] > bar["o"] and (bar["c"] - bar["l"]) >= BODY_FRACTION * rng):
        out["reason"] = "weak_candle"
        return out
    if not bar["c"] > pl["p"]:
        out["reason"] = "below_stop"
        return out
    out["signal"] = True
    out["risk_pct"] = (bar["c"] - pl["p"]) / bar["c"] * 100
    return out


# ============================================================
# الوقف المتحرك والخروج
# ============================================================

def trail_and_check(bars: list[dict[str, float]], *, entry_bar_t: int, stop: float,
                    checked_through: int) -> dict[str, Any]:
    """يمر على الشموع المغلقة بعد checked_through بالترتيب، مثل الباك-تيست:
    أولاً يرفع الوقف بالقيعان المؤكدة حتى تلك الشمعة، ثم يفحص إغلاقها.

    يرجّع الوقف الجديد، وآخر شمعة فُحصت، و exit_bar إن أغلقت شمعة تحت الوقف.
    """
    _, lows = pivots(bars, K)
    min_pivot_t = entry_bar_t - K * BAR_MS
    last = checked_through
    for i, bar in enumerate(bars):
        if bar["t"] <= checked_through or bar["t"] <= entry_bar_t:
            continue
        conf = _confirmed(lows, i)
        if conf:
            pl = conf[-1]
            if pl["t"] > min_pivot_t and pl["p"] > stop:
                stop = pl["p"]
        last = bar["t"]
        if bar["c"] < stop:
            return {"stop": stop, "checked_through": last, "exit_bar": bar}
    return {"stop": stop, "checked_through": last, "exit_bar": None}
