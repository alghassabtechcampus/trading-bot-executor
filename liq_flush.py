"""liq_flush.py — الجانب الحي لاستراتيجية "التصفيات الإجبارية" (liq_flush).

الرياضيات كلها في liq_signal.py ولا تُلمس من هنا. هذا الملف ينفّذ القواعد
المثبّتة فقط:

  دخول : SPOT market BUY بـ LIQ_USDT_PER_TRADE لكل رمز في sigs، أقصى 3 لكل فحص
  خروج : بعد 24 ساعة من الدخول، أو وقف عند entry × 0.92 — أيهما أولاً
  حدود : أقصى 6 صفقات مفتوحة، وتهدئة 24 ساعة لكل رمز بعد دخوله
  لحاق : liq_signal يفحص آخر شمعتين، فإشارة ضيّعتها نبضة فاشلة تُدخَل في
         النبضة التالية بشرط أن يكون عمر شمعتها ≤ MAX_SIGNAL_AGE_SECONDS،
         وتُسجَّل بـ late_bars=1 في الشيت

الحالة كلها تحت بادئة liq: في Redis (liq:open:*, liq:cooldown:*, liq:closed:*)
ولا تتقاطع مع trade:* / pending:* اللي تستخدمها الاستراتيجيات الأخرى، وكل أمر
يحمل orderLinkId يبدأ بـ liq_ حتى يُعرف في أرشيف الصفقات (trade_logger.py).

--------------------------------------------------------------------------
لماذا يُمرَّر `ex` بدل الاستيراد من app؟
--------------------------------------------------------------------------
`ex` هو وحدة app نفسها (app.py يمرّر sys.modules[__name__]). الاستيراد المباشر
`from app import ...` يصنع استيرادًا دائريًا لأن app يستورد هذا الملف، والأهم أنه
يربط الاختبار بوجود BYBIT_API_KEY وRedis حقيقي: app.py يقرأ os.environ[...] عند
الاستيراد. بتمرير الوحدة كوسيط نقدر نشغّل دورة كاملة (دخول → خروج) على كائن
مزيّف بلا شبكة إطلاقاً — وهذا بالضبط ما يفعله tests/test_liq_flush.py.

الدوال المطلوبة في `ex` موجودة كلها في app.py: as_decimal, decimal_str,
floor_to_step, coin_from_symbol, redis_set_json, redis_get_json, redis_delete,
redis_keys, bybit_get, get_usdt_balance, get_coin_balance, get_ticker_price,
get_instrument_filters, create_market_buy, create_market_sell, wait_for_fill,
execution_details, tracked_trade, pending_entry.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

logger = logging.getLogger("liq-flush")

STRATEGY = "liq_flush"

# ============================================================
# أرقام الاستراتيجية — مثبّتة، لا تُقرأ من البيئة
# ============================================================
# هذه خرجت من liquidation_stress_test.py ولا يجوز تعديلها من متغيّرات البيئة:
# تغييرها بالخطأ في Dokploy يعني أن الحي ما يقارن بالباك-تيست أبداً.
MAX_PER_SCAN = 3                  # أقصى عدد عملات في فحص الساعة الواحدة
MAX_OPEN = 6                      # أقصى عدد صفقات liq_flush مفتوحة في نفس الوقت
COOLDOWN_SECONDS = 24 * 60 * 60   # لا نعيد دخول نفس الرمز قبل 24 ساعة
HOLD_SECONDS = 24 * 60 * 60       # الخروج الزمني: 24 ساعة من الدخول
STOP_FRACTION = Decimal("0.92")   # وقف الخسارة: −8% من سعر التنفيذ

# تسامح الخروج الزمني. entry_time هو لحظة وصول الطلب إلى المنفّذ، وفيها تأخير
# شبكة يزيد ثوانيَ عن بداية تشغيل n8n، بينما نبضة اليوم التالي تصل على نفس
# الثانية من الساعة. فبلا تسامح يفوت شرط >= بثوانٍ قليلة ويبقى المركز مفتوحاً
# نبضة كاملة إضافية: ETC في 2026-10-08 احتفظ 25.0 ساعة بدل 24 لتأخير 4 ثوان
# (handoff/LIQ_STATUS_CHECK_AR.md). خمس دقائق أصغر بكثير من المسافة بين نبضتين،
# فهي لا تقدر تُقدّم الخروج نبضة كاملة — تمنع تأخيره نبضة كاملة فقط.
HOLD_TOLERANCE_SECONDS = 5 * 60

# أقصى عمر مقبول لشمعة الإشارة وقت التنفيذ، يُقاس من **إغلاق** الشمعة.
# liq_signal يفحص آخر شمعتين (EVAL_BARS) فنبضة فاشلة تلحقها النبضة التالية،
# لكن إشارة قديمة لا تُدخَل: حركة التصفية تُستهلك بسرعة والباك-تيست يدخل عند
# افتتاح الشمعة التالية مباشرة. ساعتان تكفيان لنبضة واحدة فائتة (اللحاق يحصل
# بعد ~ساعة من الإغلاق) وترفض ما هو أقدم من ذلك.
MAX_SIGNAL_AGE_SECONDS = 2 * 60 * 60
BAR_SECONDS = 60 * 60             # شموع الإشارة ساعية

# قفل التنفيذ المتزامن: n8n يعيد المحاولة عند أي timeout، والفحص يستغرق ~60 ثانية،
# فبدون القفل تقدر محاولتان تدخلان نفس الإشارة مرتين.
TICK_LOCK_SECONDS = 15 * 60

# كم شمعة ساعة نسحب للتحقّق من الوقف. 30 > 24 ساعة الاحتفاظ، فتغطّي كل عمر
# الصفقة حتى لو فاتت عدة نبضات (n8n توقف سابقاً — راجع handoff/README_AR.md).
STOP_KLINE_LIMIT = 30

# مهلة بقاء سجل الصفقة المغلقة في Redis (للمراجعة فقط؛ الأرشيف الدائم هو
# Google Sheet عبر n8n + executions_real.jsonl من trade_logger.py).
CLOSED_TTL_SECONDS = 90 * 24 * 60 * 60

# حدّ "عدم تطابق الرصيد": لو الموجود في المحفظة أقل من 90% من الكمية المسجّلة،
# فهذه ليست رسوماً ولا تقريب خطوة — هذه قراءة رصيد خاطئة أو بيع من مكان آخر.
# الفرق بينها وبين الغبار: الغبار = الكمية المسجّلة نفسها أصغر من حد البيع،
# وعدم التطابق = كنّا نتوقّع كمية قابلة للبيع فوجدنا أقل منها بكثير.
MISMATCH_FRACTION = Decimal("0.90")

# لا نعيد المحاولة إلى ما لا نهاية: بعد هذا العدد من الفحوص المتتالية بنفس
# الحالة، يُغلق السجل بحالة needs_manual_check حتى لا يبقى يحذّر كل ساعة للأبد.
MAX_EXIT_ATTEMPTS = 3

# مهلة حجز الدخول. تغطّي أمر الشراء وانتظار الفيل كاملاً وتزيد، فلو مات العامل
# في منتصف الشراء رجع الرمز قابلاً للدخول بعدها — ومع ذلك تبقى التهدئة وسجل
# الدخول يمنعانه 24 ساعة، فالفشل يميل إلى عدم الدخول لا إلى الدخول مرتين.
ENTRY_RESERVE_SECONDS = 10 * 60

# عمر سجل الدخول. أطول من التهدئة (24 ساعة) لأن وظيفته أن يبقى حين تختفي
# التهدئة: لو حُذف مفتاح التهدئة أو انتهى قبل وقته، هذا السجل وحده يمنع
# الدخول المكرّر. الفرق بينه وبين liq:closed: أن هذا يُكتب عند الدخول لا الخروج،
# فيغطّي الصفقة المفتوحة أيضاً.
ENTRY_JOURNAL_SECONDS = 48 * 60 * 60

# النبضة كل ساعة (جدول n8n: الدقيقة 2 من كل ساعة). "متقادمة" = فاتت نبضة
# كاملة ونصف — هامش يمنع إنذاراً كاذباً من تأخّر دقائق في الجدول.
TICK_INTERVAL_MINUTES = 60
STALE_AFTER_MINUTES = 90


# ============================================================
# الإعدادات من البيئة — تُقرأ عند كل نداء
# ============================================================
# القراءة عند النداء (لا عند الاستيراد) حتى يقدر الاختبار يبدّلها بـ monkeypatch،
# وحتى لا يحتاج أي تغيير في المفتاح القاتل إعادة بناء الصورة.

def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """المفتاح القاتل. الافتراضي false: صورة جديدة لا تتداول حتى تُشغّل صريحاً."""
    return _flag("LIQ_ENABLED")


def is_dry_run() -> bool:
    return _flag("LIQ_DRY_RUN")


def usdt_per_trade() -> Decimal:
    return Decimal(os.getenv("LIQ_USDT_PER_TRADE", "10"))


# ============================================================
# مفاتيح Redis
# ============================================================
# التشغيل التجريبي يكتب تحت بادئة منفصلة (liq:dry:*) فيمرّ بنفس آلة الحالة
# بالكامل — تهدئة، حدود، خروج — بدون أن يخلط سجلاته مع الصفقات الحقيقية.

def _prefix(dry: bool) -> str:
    return "liq:dry" if dry else "liq"


def open_key(symbol: str, dry: bool) -> str:
    return f"{_prefix(dry)}:open:{symbol}"


def open_pattern(dry: bool) -> str:
    return f"{_prefix(dry)}:open:*"


def cooldown_key(symbol: str, dry: bool) -> str:
    return f"{_prefix(dry)}:cooldown:{symbol}"


def closed_key(symbol: str, stamp: int, dry: bool) -> str:
    return f"{_prefix(dry)}:closed:{symbol}:{stamp}"


def lock_key(dry: bool) -> str:
    return f"{_prefix(dry)}:tick:lock"


def reserve_key(symbol: str, dry: bool) -> str:
    """حجز الدخول لرمز واحد. يُؤخذ بـ SET NX قبل أمر الشراء — راجع
    process_entries: كل الفحوص قبله "اقرأ ثم اكتب"، وهذا وحده ذرّي."""
    return f"{_prefix(dry)}:reserve:{symbol}"


def journal_key(symbol: str, stamp: int, dry: bool) -> str:
    """سجل الدخول: مفتاح واحد لكل دخول، والوقت في اسم المفتاح نفسه حتى يُقرأ
    بـ KEYS بدون GET لكل رمز."""
    return f"{_prefix(dry)}:journal:{symbol}:{stamp}"


def journal_pattern(symbol: str, dry: bool) -> str:
    return f"{_prefix(dry)}:journal:{symbol}:*"


def last_tick_key(dry: bool) -> str:
    return f"{_prefix(dry)}:last_tick"


def order_link_id(side: str, symbol: str, stamp: int) -> str:
    """بادئة liq_ تميّز أوامر هذه الاستراتيجية في /v5/execution/list.

    trade_logger.py يحفظ orderLinkId كما هو، فهذه هي الوصلة الوحيدة بين الفيل
    الحقيقي والاستراتيجية التي سببته. حد Bybit 36 حرفاً وهذا التنسيق ~28.
    """
    return f"liq_{side}_{symbol}_{stamp}"[:36]


# ============================================================
# أدوات صغيرة
# ============================================================

def _iso(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value: Any) -> str:
    """نص رقمي بدون صيغة علمية ولا أصفار زائدة — للعرض في تلغرام والسجل."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _bar_end_ts(bar: Any) -> int | None:
    """لحظة إغلاق شمعة الإشارة بالثواني، أو None لو تعذّر تحليل النص.

    bar من liq_signal هو وقت **افتتاح** الشمعة (هكذا ترجّعها Bybit)، وشمعة
    17:00 تُغلق 18:00. فعمر الإشارة يُقاس من الإغلاق لأنه اللحظة التي صارت
    فيها الإشارة معروفة أصلاً — القياس من الافتتاح يطرح ساعة كاملة بلا سبب
    ويرفض لحاقاً صحيحاً.
    """
    if not isinstance(bar, str):
        return None
    try:
        opened = datetime.strptime(bar, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(opened.timestamp()) + BAR_SECONDS


def _round(value: Decimal, places: str) -> Decimal:
    """تقريب للعرض والتسجيل. القسمة في Decimal ترجّع 28 رقماً عشرياً، وصفٌّ مثل
    pnl_pct = 0.01655355073663300778016884622 في Google Sheet غير قابل للقراءة
    ولا للمقارنة بـ +1.65% المتوقّعة من الباك-تيست."""
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _fee_of(order: dict[str, Any]) -> Decimal:
    """رسوم الأمر المنفّذ. execution_details في app.py لا ترجّعها، وهي مطلوبة
    للمقارنة مع الباك-تيست (الذي يفترض 0.2% ذهاباً وإياباً)."""
    try:
        return Decimal(str(order.get("cumExecFee") or "0"))
    except Exception:  # noqa: BLE001 — رقم مشوّه من المنصة لا يُسقط الصفقة
        return Decimal("0")


# ============================================================
# رسائل تلغرام (عربي، على الأحداث الحقيقية فقط)
# ============================================================
# لا يُرسل شيء من هنا: الرسائل تُرجَّع في messages[] وn8n هو من يبعثها بعقدة
# Telegram الموجودة أصلاً — نفس نمط indicators/alert_watcher.py. فائدته أن
# بيانات بوت تلغرام لا تدخل حاوية التنفيذ التي تحمل مفتاح Bybit.

def buy_message(symbol: str, price: Decimal, z_r4: float, z_oi4: float,
                late_bars: int = 0) -> str:
    # الدخول المتأخر يُعلن صريحاً: إشارة لُحقت بعد نبضة فاشلة ليست نفس الدخول
    # الفوري، ولازم تعرف ذلك وقت وصول الرسالة لا وقت مراجعة الشيت.
    late = f" — ⏱ لحاق متأخر {late_bars} شمعة" if late_bars else ""
    return (f"🟢 شراء: {symbol} بسعر {_num(price)} — "
            f"z سعر {z_r4:.1f} / z OI {z_oi4:.1f} — خروج بعد 24 ساعة أو وقف 8%{late}")


def sell_message(symbol: str, reason: str, pnl_pct: Decimal, pnl_usd: Decimal,
                 *, dry: bool = False) -> str:
    reason_ar = {"time": "24 ساعة", "stop": "وقف"}.get(reason, reason)
    mark = "🟢" if pnl_usd >= 0 else "🔴"
    tag = "🧪 تجريبي — " if dry else ""
    return (f"{tag}{mark} بيع: {symbol} — سبب: {reason_ar} — "
            f"ربح/خسارة {pnl_pct:+.2f}% و {pnl_usd:+.2f}$")


def warn_message(text: str) -> str:
    return f"⚠️ {text}"


# ============================================================
# بيانات السوق المطلوبة للخروج
# ============================================================

def lowest_low_since(ex: Any, symbol: str, since_ms: int, now_ms: int) -> Decimal | None:
    """أدنى low بين شموع الساعة المغلقة التي بدأت بعد التنفيذ.

    الباك-تيست يضرب الوقف على low الشمعة، لا على إغلاقها
    (liquidation_stress_test.py:51). فحص آخر سعر فقط كان يفوّت أي فتيل ينزل
    تحت الوقف ويرجع — وهو ما يجعل الحي يبدو أفضل من الاختبار بلا سبب حقيقي.

    نسحب 30 شمعة ونفحصها كلها لا الأخيرة وحدها: نبضة واحدة فائتة كانت ستُسقط
    ساعة من المسار نهائياً.

    شموع spot لا linear: التنفيذ على spot، فالوقف يُقاس على السوق الذي نتداوله.
    يرجّع None إذا تعذّر السحب — القرار يرجع لآخر سعر وحده، ولا يُلغى الخروج.
    """
    try:
        payload = ex.bybit_get("/v5/market/kline", {
            "category": "spot",
            "symbol": symbol,
            "interval": "60",
            "limit": str(STOP_KLINE_LIMIT),
        })
        rows = payload.get("result", {}).get("list", []) or []
    except Exception:  # noqa: BLE001
        logger.exception("stop kline fetch failed for %s", symbol)
        return None

    hour_floor_ms = (now_ms // 3_600_000) * 3_600_000
    lows: list[Decimal] = []
    for row in rows:
        try:
            start_ms = int(row[0])
            # start >= since_ms: نستثني شمعة الدخول نفسها، لأن أدنى سعر فيها قد
            # يكون قبل تنفيذنا فينتج وقفاً لم يحدث لنا.
            # start < hour_floor: الشمعة الحالية لم تُغلق بعد، ويغطيها آخر سعر.
            if since_ms <= start_ms < hour_floor_ms:
                lows.append(Decimal(str(row[3])))
        except (IndexError, TypeError, ValueError):
            continue
    return min(lows) if lows else None


# ============================================================
# الخروج
# ============================================================

def _reason_with_quantities(reason: str, remaining: Decimal, expected: Decimal) -> str:
    """سبب الخروج ومعه الكميتان في خانة واحدة.

    تبويب liq_flush في الشيت له 18 عموداً ثابتة (A1:R1)، فإضافة عمودين جديدين
    تكسر ربط عقدة Google Sheets في n8n. الكميتان تُحقنان هنا داخل نص السبب —
    خانة نصية أصلاً — فيصل الرقمان إلى الشيت بدون أي تغيير في المخطّط.
    """
    return f"{reason} remaining={_num(remaining)} expected={_num(expected)}"


def _close_unsold(
    ex: Any,
    record: dict[str, Any],
    reason: str,
    *,
    now: int,
    dry: bool,
    remaining_qty: Decimal,
    expected_qty: Decimal,
    price_now: Decimal,
) -> dict[str, Any]:
    """يغلق صفقة لم تُبَع فعلاً، مع صف خروج كامل بدل الحذف الصامت.

    الربح هنا تقييم سوقي لما تبقّى فعلاً مقابل ما دُفع كاملاً: دُفع entry_value
    وبقي remaining_qty قيمتها كذا. لو كان المتبقي غباراً فالنتيجة خسارة شبه
    كاملة — وهي النتيجة الحقيقية طالما أن ما تبقّى غير قابل للبيع. إخفاؤها هو
    ما كان العيب.
    """
    symbol = str(record["symbol"])
    entry_value = ex.as_decimal(record["entry_value"], "entry_value")
    remaining_value = remaining_qty * price_now
    pnl_usd = _round(remaining_value - entry_value, "0.00000001")
    pnl_pct = _round(
        (pnl_usd / entry_value * Decimal("100")) if entry_value > 0 else Decimal("0"),
        "0.0001",
    )

    closed = {
        **record,
        "exit_time": now,
        "exit_time_iso": _iso(now),
        "exit_price": _num(price_now),
        "exit_qty": _num(remaining_qty),
        "exit_value": _num(remaining_value),
        "exit_fee": "0",
        "exit_order_id": "",
        "exit_reason": _reason_with_quantities(reason, remaining_qty, expected_qty),
        "exit_reason_code": reason,
        "unsold": True,
        "remaining_qty": _num(remaining_qty),
        "expected_qty": _num(expected_qty),
        "pnl": _num(pnl_usd),
        "pnl_pct": _num(pnl_pct),
        "hold_hours": round((now - int(record["entry_time"])) / 3600, 2),
    }
    ex.redis_set_json(closed_key(symbol, now, dry), closed, ex_seconds=CLOSED_TTL_SECONDS)
    ex.redis_delete(open_key(symbol, dry))
    return closed


def _retry_or_escalate(
    ex: Any,
    record: dict[str, Any],
    reason: str,
    *,
    now: int,
    dry: bool,
    remaining_qty: Decimal,
    expected_qty: Decimal,
    price_now: Decimal,
) -> dict[str, Any]:
    """رصيد أقل بكثير من المتوقّع: لا نبيع ولا نحذف — نعيد المحاولة ثم نصعّد.

    قراءة رصيد خاطئة أو خطأ حساب قد تُصلح نفسها في الفحص التالي، فالحذف الفوري
    يفقد مركزاً حقيقياً. وفي المقابل التكرار بلا سقف يعني تحذيراً كل ساعة إلى
    الأبد، فبعد MAX_EXIT_ATTEMPTS فحصاً متتالياً بنفس الحالة يُغلق السجل
    بـ needs_manual_check ليتدخّل إنسان.
    """
    symbol = str(record["symbol"])
    previous = str(record.get("exit_block_code") or "")
    attempts = int(record.get("exit_attempts") or 0)
    attempts = attempts + 1 if previous == "balance_mismatch" else 1

    if attempts >= MAX_EXIT_ATTEMPTS:
        closed = _close_unsold(
            ex, record, "needs_manual_check", now=now, dry=dry,
            remaining_qty=remaining_qty, expected_qty=expected_qty, price_now=price_now,
        )
        logger.error("liq balance mismatch on %s unresolved after %d checks — "
                     "closed as needs_manual_check (remaining=%s expected=%s)",
                     symbol, attempts, _num(remaining_qty), _num(expected_qty))
        return {
            "symbol": symbol,
            "status": "needs_manual_check",
            "reason": reason,
            "attempts": attempts,
            "remaining_qty": _num(remaining_qty),
            "expected_qty": _num(expected_qty),
            "closed": closed,
        }

    # السجل يبقى مفتوحاً: الفحص التالي يعيد المحاولة من البداية.
    record["exit_block_code"] = "balance_mismatch"
    record["exit_attempts"] = attempts
    record["exit_last_check"] = now
    record["exit_last_check_iso"] = _iso(now)
    record["remaining_qty"] = _num(remaining_qty)
    record["expected_qty"] = _num(expected_qty)
    ex.redis_set_json(open_key(symbol, dry), record)

    logger.error("liq balance mismatch on %s: remaining=%s expected=%s (check %d/%d)",
                 symbol, _num(remaining_qty), _num(expected_qty),
                 attempts, MAX_EXIT_ATTEMPTS)
    return {
        "symbol": symbol,
        "status": "balance_mismatch",
        "reason": reason,
        "attempts": attempts,
        "remaining_qty": _num(remaining_qty),
        "expected_qty": _num(expected_qty),
    }


def _close_position(
    ex: Any,
    record: dict[str, Any],
    reason: str,
    *,
    now: int,
    dry: bool,
) -> dict[str, Any]:
    """بيع السوق لكامل الكمية المنفّذة، ثم نقل السجل من open إلى closed."""
    symbol = str(record["symbol"])
    entry_price = ex.as_decimal(record["entry_price"], "entry_price")
    entry_value = ex.as_decimal(record["entry_value"], "entry_value")
    held_qty = ex.as_decimal(record["qty"], "qty")

    filters = ex.get_instrument_filters(symbol)
    step = filters["qty_step"]

    price_now = ex.get_ticker_price(symbol)

    if dry:
        # لا شراء فعلي حصل، فلا يوجد رصيد حقيقي يُفحص: قراءة المحفظة هنا كانت
        # تقيس عملات استراتيجية أخرى (أو صفراً) فتحوّل كل خروج تجريبي إلى
        # "غبار" يُحذف بصمت — وهذا بالضبط ما كان يخفي نتيجة الصفقة.
        # الصفقة وهمية بالكمية المسجّلة بالضبط: بلا حسم 0.999 لأن رسوم الشراء
        # التي يعوّضها ذلك الحسم لم تُدفع أصلاً، فيأتي الربح مطابقاً للباك-تيست.
        exit_qty = ex.floor_to_step(held_qty, step)
        exit_price = price_now
        exit_value = exit_price * exit_qty
        exit_fee = Decimal("0")
        exit_order_id = f"DRY-SELL-{symbol}-{now}"
    else:
        # ×0.999 مثل close_tracked_trade في app.py: رسوم الشراء تُخصم من العملة
        # الأساسية، فبيع الكمية المسجّلة حرفياً يُرفض بـ "insufficient balance".
        target_qty = ex.floor_to_step(held_qty * Decimal("0.999"), step)

        # سقف احترازي بالرصيد الفعلي. الحد الأدنى يحمي من بيع عملات استراتيجية
        # أخرى لو كانت الكمية المسجّلة أكبر من الموجود (بيع يدوي بينهما مثلاً).
        coin = ex.coin_from_symbol(symbol)
        wallet_qty = ex.get_coin_balance(coin)["wallet_balance"]
        available = ex.floor_to_step(wallet_qty, step)
        sell_qty = min(target_qty, available)

        # المقارنة والتبليغ بالكميات الخام: المسجّل مقابل الموجود في المحفظة.
        # التقريب لخطوة الأمر تفصيلة تنفيذية تُظهر صفراً حين يكون المتبقي أصغر
        # من الخطوة، وهو أسوأ رقم يمكن وضعه في تحذير.
        #
        # فحص عدم التطابق يسبق فحص "قابل للبيع" عمداً: نقص 50% قد يكون قابلاً
        # للبيع تماماً، وبيعه بصمت بنصف الحجم يخفي الخطأ بدل أن يكشفه. الشرط
        # الثاني (أن المركز المتوقّع كان قابلاً للبيع) هو ما يفصل هذه الحالة عن
        # "مجرد غبار": مركز صغير أصلاً لا يوجد فيه ما يُستغرب.
        expected_sellable = (target_qty >= filters["min_qty"]
                             and target_qty * price_now >= filters["min_amt"])
        if expected_sellable and wallet_qty < held_qty * MISMATCH_FRACTION:
            return _retry_or_escalate(
                ex, record, reason, now=now, dry=dry,
                remaining_qty=wallet_qty, expected_qty=held_qty,
                price_now=price_now,
            )

        sellable = (sell_qty > 0
                    and sell_qty >= filters["min_qty"]
                    and sell_qty * price_now >= filters["min_amt"])

        if not sellable:
            closed = _close_unsold(
                ex, record, "dust_unsellable", now=now, dry=dry,
                remaining_qty=wallet_qty, expected_qty=held_qty, price_now=price_now,
            )
            logger.warning("liq dust on %s: remaining=%s expected=%s value=%s",
                           symbol, _num(wallet_qty), _num(held_qty),
                           _num(wallet_qty * price_now))
            return {
                "symbol": symbol,
                "status": "dust_unsellable",
                "reason": reason,
                "remaining_qty": _num(wallet_qty),
                "expected_qty": _num(held_qty),
                "value": _num(wallet_qty * price_now),
                "closed": closed,
            }

        response = ex.create_market_sell(symbol, sell_qty, order_link_id("s", symbol, now))
        order_id = response.get("result", {}).get("orderId")
        if not order_id:
            raise RuntimeError("Sell response does not contain orderId")
        order = ex.wait_for_fill(symbol, order_id)
        details = ex.execution_details(order)
        exit_price = details["average_price"]
        exit_qty = details["executed_qty"]
        exit_value = details["executed_value"]
        exit_fee = _fee_of(order)
        exit_order_id = str(details["order_id"])

    # الربح على القيمة المنفّذة فعلاً، لا على سعر × كمية: البيع الجزئي يجعل
    # الاثنين مختلفين، والقيمة هي ما وصل المحفظة بالفعل.
    realised_entry = entry_value * (exit_qty / held_qty) if held_qty > 0 else entry_value
    pnl_usd = _round(exit_value - realised_entry, "0.00000001")
    pnl_pct = _round(
        (pnl_usd / realised_entry * Decimal("100")) if realised_entry > 0 else Decimal("0"),
        "0.0001",
    )

    closed = {
        **record,
        "exit_time": now,
        "exit_time_iso": _iso(now),
        "exit_price": _num(exit_price),
        "exit_qty": _num(exit_qty),
        "exit_value": _num(exit_value),
        "exit_fee": _num(exit_fee),
        "exit_order_id": exit_order_id,
        "exit_reason": reason,
        "pnl": _num(pnl_usd),
        "pnl_pct": _num(pnl_pct),
        "hold_hours": round((now - int(record["entry_time"])) / 3600, 2),
    }

    ex.redis_set_json(closed_key(symbol, now, dry), closed, ex_seconds=CLOSED_TTL_SECONDS)
    ex.redis_delete(open_key(symbol, dry))

    return {
        "symbol": symbol,
        "status": "sold",
        "reason": reason,
        "exit_price": _num(exit_price),
        "pnl": _num(pnl_usd),
        "pnl_pct": _num(pnl_pct),
        "closed": closed,
    }


def _journal_exit(closed: dict[str, Any]) -> dict[str, Any]:
    """صف واحد مكتمل للرحلة كاملة — هذا ما يُضاف إلى Google Sheet."""
    return {
        "strategy": STRATEGY,
        "event": "exit",
        "symbol": closed["symbol"],
        "entry_time": closed.get("entry_time_iso"),
        "entry_price": closed.get("entry_price"),
        "qty": closed.get("qty"),
        "exit_time": closed.get("exit_time_iso"),
        "exit_price": closed.get("exit_price"),
        "exit_reason": closed.get("exit_reason"),
        "stop_level": closed.get("stop_level"),
        "fees": _num(
            Decimal(str(closed.get("entry_fee") or "0"))
            + Decimal(str(closed.get("exit_fee") or "0"))
        ),
        "pnl": closed.get("pnl"),
        "pnl_pct": closed.get("pnl_pct"),
        "hold_hours": closed.get("hold_hours"),
        "z_r4": closed.get("z_r4"),
        "z_oi4": closed.get("z_oi4"),
        "signal_bar": closed.get("signal_bar"),
        "late_bars": closed.get("late_bars", 0),
        "dry_run": closed.get("dry_run", False),
    }


def process_exits(ex: Any, result: dict[str, Any], *, now: int, dry: bool) -> None:
    """يفحص كل صفقة liq مفتوحة: وقف أولاً، ثم الخروج الزمني.

    الوقف قبل الزمن مقصود: لو فاتت نبضات وصار الشرطان صحيحين معاً، فالوقف كان
    سيضرب أولاً زمنياً — وهذا ما يحسبه الباك-تيست.
    """
    for key in ex.redis_keys(open_pattern(dry)):
        symbol = key.rsplit(":", 1)[-1]
        try:
            record = ex.redis_get_json(key)
            if not record:
                continue

            entry_time = int(record["entry_time"])
            entry_price = ex.as_decimal(record["entry_price"], "entry_price")
            stop_level = entry_price * STOP_FRACTION
            age_seconds = now - entry_time

            price_now = ex.get_ticker_price(symbol)
            path_low = lowest_low_since(ex, symbol, entry_time * 1000, now * 1000)
            trigger_price = min(price_now, path_low) if path_low is not None else price_now

            if trigger_price <= stop_level:
                reason = "stop"
            elif age_seconds >= HOLD_SECONDS - HOLD_TOLERANCE_SECONDS:
                reason = "time"
            else:
                result["exits"].append({
                    "symbol": symbol,
                    "status": "holding",
                    "price": _num(price_now),
                    "path_low": _num(path_low) if path_low is not None else None,
                    "stop_level": _num(stop_level),
                    "hours_held": round(age_seconds / 3600, 2),
                })
                continue

            record["stop_level"] = _num(stop_level)
            if reason == "stop":
                record["stop_trigger_price"] = _num(trigger_price)

            sale = _close_position(ex, record, reason, now=now, dry=dry)
            result["exits"].append(sale)

            status = sale["status"]
            if status == "sold":
                closed = sale["closed"]
                result["journal"].append(_journal_exit(closed))
                result["messages"].append(sell_message(
                    symbol, reason,
                    Decimal(closed["pnl_pct"]),
                    Decimal(closed["pnl"]),
                    dry=dry,
                ))

            elif status == "dust_unsellable":
                # صف خروج كامل أولاً: نتيجة الصفقة تُسجَّل حتى حين يتعذّر البيع.
                closed = sale["closed"]
                result["journal"].append(_journal_exit(closed))
                result["messages"].append(warn_message(
                    f"{symbol}: الصفقة ما انباعت — المتبقي "
                    f"{sale['remaining_qty']} أقل من الحد الأدنى للبيع "
                    f"(المتوقّع {sale['expected_qty']}، قيمة المتبقي "
                    f"{sale['value']}$). سُجّل صف خروج بسبب dust_unsellable "
                    f"(سبب الخروج: {reason})"
                ))

            elif status == "balance_mismatch":
                # لا صف خروج: الصفقة ما زالت مفتوحة وستُعاد محاولتها.
                result["messages"].append(warn_message(
                    f"عدم تطابق الرصيد في {symbol}: الموجود "
                    f"{sale['remaining_qty']} والمتوقّع {sale['expected_qty']} "
                    f"(أقل من {MISMATCH_FRACTION * 100:.0f}% من المسجّل). "
                    f"لم يُبَع ولم يُحذف — محاولة {sale['attempts']}/"
                    f"{MAX_EXIT_ATTEMPTS}، يُعاد الفحص في النبضة التالية"
                ))

            elif status == "needs_manual_check":
                closed = sale["closed"]
                result["journal"].append(_journal_exit(closed))
                result["messages"].append(warn_message(
                    f"🛑 {symbol}: عدم تطابق الرصيد استمر {sale['attempts']} "
                    f"فحوص متتالية (الموجود {sale['remaining_qty']} والمتوقّع "
                    f"{sale['expected_qty']}). أُغلق السجل بحالة "
                    f"needs_manual_check — يحتاج مراجعة يدوية على المنصة"
                ))

        except Exception as exc:  # noqa: BLE001 — رمز واحد فاشل لا يوقف الباقي
            logger.exception("liq exit failed for %s", symbol)
            result["errors"].append({"stage": "exit", "symbol": symbol, "error": str(exc)})
            result["messages"].append(warn_message(f"خطأ في خروج {symbol}: {exc}"))


# ============================================================
# الدخول
# ============================================================

def _open_count(ex: Any, dry: bool) -> int:
    return len(ex.redis_keys(open_pattern(dry)))


def _foreign_position(ex: Any, symbol: str) -> str | None:
    """هل لاستراتيجية أخرى مركز أو أمر معلّق على نفس الرمز؟

    السبب ليس المنع النظري بل خطر حقيقي: close_tracked_trade في app.py يقرأ
    usd_value الإجمالي للعملة في فحص الغبار، فلو صار مركز الاستراتيجية الأخرى
    غباراً بينما نحن نحمل نفس العملة، شافت قيمتنا وحسبتها لها وباعت من عملاتنا.
    """
    try:
        if ex.tracked_trade(symbol):
            return "trade"
        if ex.pending_entry(symbol):
            return "pending"
    except Exception:  # noqa: BLE001 — فشل القراءة لا يمنع الدخول
        logger.exception("foreign position check failed for %s", symbol)
    return None


def _open_position(
    ex: Any,
    signal: dict[str, Any],
    *,
    now: int,
    dry: bool,
    amount: Decimal,
) -> dict[str, Any]:
    symbol = str(signal["symbol"])

    if dry:
        price = ex.get_ticker_price(symbol)
        filters = ex.get_instrument_filters(symbol)
        qty = ex.floor_to_step(amount / price, filters["qty_step"])
        if qty <= 0 or qty < filters["min_qty"]:
            raise RuntimeError(
                f"dry-run qty {qty} below minimum {filters['min_qty']} for {symbol}"
            )
        value = price * qty
        fee = Decimal("0")
        order_id = f"DRY-BUY-{symbol}-{now}"
        status = "DryFilled"
    else:
        response = ex.create_market_buy(symbol, amount, order_link_id("b", symbol, now))
        order_id = response.get("result", {}).get("orderId")
        if not order_id:
            raise RuntimeError("Buy response does not contain orderId")
        order = ex.wait_for_fill(symbol, order_id)
        details = ex.execution_details(order)
        # الكمية والسعر من المنصة لا من الإشارة: الخروج لاحقاً يبيع هذه بالضبط.
        price = details["average_price"]
        qty = details["executed_qty"]
        value = details["executed_value"]
        fee = _fee_of(order)
        order_id = str(details["order_id"])
        status = str(details["status"])

    signal_bar = signal.get("bar")
    record = {
        "strategy": STRATEGY,
        "symbol": symbol,
        "coin": ex.coin_from_symbol(symbol),
        "entry_order_id": order_id,
        "entry_status": status,
        "entry_time": now,
        "entry_time_iso": _iso(now),
        "entry_price": _num(price),
        "entry_value": _num(value),
        "entry_fee": _num(fee),
        "qty": _num(qty),
        "stop_level": _num(price * STOP_FRACTION),
        "exit_due": now + HOLD_SECONDS,
        "exit_due_iso": _iso(now + HOLD_SECONDS),
        "signal_bar": signal_bar if isinstance(signal_bar, str) else str(signal_bar),
        "signal_close": _num(signal.get("close")),
        "z_r4": float(signal["z_r4"]),
        "z_oi4": float(signal["z_oi4"]),
        # كم شمعة تأخّر الدخول عن شمعة الإشارة. 0 = النبضة التي تلي الإغلاق
        # مباشرة (الحالة الطبيعية)، 1 = لحاق بعد نبضة فاشلة. يُحفظ في السجل لا
        # يُحسب لاحقاً، لأن صف الخروج يحتاجه بعد 24 ساعة من اختفاء الإشارة.
        "late_bars": int(signal.get("late_bars") or 0),
        "dry_run": dry,
    }

    # الحالة قبل التهدئة: لو مات الطلب بينهما، تهدئة بلا مركز تفقد الصفقة تماماً،
    # أما مركز بلا تهدئة فأسوأ ما فيه إشارة مكرّرة يمنعها وجود المركز نفسه.
    ex.redis_set_json(open_key(symbol, dry), record)
    ex.redis_set_json(
        cooldown_key(symbol, dry),
        {"entered": now, "entry_price": record["entry_price"]},
        ex_seconds=COOLDOWN_SECONDS,
    )
    # سجل الدخول: عائلة مفاتيح ثالثة مستقلة عن open: وcooldown:. الحارسان
    # الأولان مفتاح واحد لكل رمز، فحذفه — يدوياً أو بانتهاء مهلة قبل وقتها —
    # يفتح الرمز للدخول فوراً. هذا السجل يُقرأ أيضاً في process_entries، فمسح
    # الحارسين وحدهما لم يعد كافياً لإعادة الدخول قبل 24 ساعة.
    # ملاحظة صريحة: مسح البادئة كلها يمسح هذا أيضاً — الحماية طبقة إضافية
    # لا ضمان مطلق.
    ex.redis_set_json(
        journal_key(symbol, now, dry),
        {"entered": now, "entered_iso": _iso(now),
         "entry_price": record["entry_price"], "qty": record["qty"]},
        ex_seconds=ENTRY_JOURNAL_SECONDS,
    )
    return record


def _last_journal_entry(ex: Any, symbol: str, *, dry: bool) -> int:
    """وقت أحدث دخول مسجّل لهذا الرمز، أو 0 إذا لا سجل.

    الوقت يُقرأ من اسم المفتاح لا من محتواه: KEYS واحد لكل رمز بدل GET لكل
    مفتاح. مفتاح بلاحقة غير رقمية يُتجاهل بدل أن يُسقط الدخول كله.
    """
    newest = 0
    for key in ex.redis_keys(journal_pattern(symbol, dry)):
        try:
            stamp = int(key.rsplit(":", 1)[-1])
        except (TypeError, ValueError):
            logger.warning("liq journal key with no timestamp: %s", key)
            continue
        newest = max(newest, stamp)
    return newest


def _journal_entry(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "strategy": STRATEGY,
        "event": "entry",
        "symbol": record["symbol"],
        "entry_time": record["entry_time_iso"],
        "entry_price": record["entry_price"],
        "qty": record["qty"],
        "exit_time": None,
        "exit_price": None,
        "exit_reason": None,
        "stop_level": record["stop_level"],
        "fees": record["entry_fee"],
        "pnl": None,
        "pnl_pct": None,
        "hold_hours": None,
        "z_r4": record["z_r4"],
        "z_oi4": record["z_oi4"],
        "signal_bar": record["signal_bar"],
        "late_bars": record.get("late_bars", 0),
        "dry_run": record["dry_run"],
    }


def _normalise_signal(raw: dict[str, Any]) -> dict[str, Any]:
    """نحوّل مخرجات liq_signal.scan إلى حقول قابلة للتخزين (bar هو Timestamp)."""
    bar = raw.get("bar")
    return {
        "symbol": str(raw["symbol"]),
        "close": raw.get("close"),
        "z_r4": float(raw["z_r4"]),
        "z_oi4": float(raw["z_oi4"]),
        "bar": bar.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(bar, "strftime") else str(bar),
        # غيابها = 0: force_signals اليدوي وأي نداء قديم يبقى يعمل كما هو.
        "late_bars": int(raw.get("late_bars") or 0),
    }


def process_entries(
    ex: Any,
    result: dict[str, Any],
    signals: list[dict[str, Any]],
    *,
    now: int,
    dry: bool,
) -> None:
    amount = usdt_per_trade()
    open_count = _open_count(ex, dry)

    # sigs مرتّبة من الأكثر تطرفاً في liq_signal.scan — نأخذ الأول 3 ونتجاهل الباقي.
    for raw in signals[:MAX_PER_SCAN]:
        symbol = str(raw.get("symbol", "?"))
        try:
            signal = _normalise_signal(raw)

            # حدّ عمر الإشارة المتأخّرة — أول فحص لأنه الأرخص (بلا Redis ولا
            # شبكة) ولأن إشارة قديمة مرفوضة أصلاً لا تستحق بقية الفحوص.
            # العمر يُقاس وقت التنفيذ (now) لا وقت الفحص: الفحص يستغرق ~43
            # ثانية ولا يُغيّر العمر عملياً، لكن الاتفاق المكتوب هو لحظة
            # الدخول. وشمعة غير قابلة للتحليل تُرفض عند التأخّر: إشارة لا
            # نعرف عمرها أخطر من إشارة فائتة.
            if signal["late_bars"]:
                bar_end = _bar_end_ts(signal["bar"])
                age = None if bar_end is None else now - bar_end
                if age is None or age > MAX_SIGNAL_AGE_SECONDS:
                    result["skipped"].append({
                        "symbol": symbol,
                        "reason": "signal_too_old",
                        "late_bars": signal["late_bars"],
                        "signal_bar": signal["bar"],
                        "age_minutes": None if age is None else round(age / 60, 1),
                    })
                    result["messages"].append(warn_message(
                        f"تخطّي {symbol}: إشارة شمعة {signal['bar']} عمرها "
                        f"{'غير معروف' if age is None else f'{age / 3600:.1f} ساعة'} "
                        f"وهو فوق حد {MAX_SIGNAL_AGE_SECONDS // 3600} ساعة — لا لحاق"
                    ))
                    continue

            if open_count >= MAX_OPEN:
                result["skipped"].append({"symbol": symbol, "reason": "max_open"})
                continue

            if ex.redis_get_json(open_key(symbol, dry)):
                result["skipped"].append({"symbol": symbol, "reason": "already_open"})
                continue

            if ex.redis_get_json(cooldown_key(symbol, dry)):
                result["skipped"].append({"symbol": symbol, "reason": "cooldown"})
                continue

            # الحارس الثالث: التهدئة مفتاح واحد، وغيابه لأي سبب يفتح الرمز
            # فوراً. سجل الدخول عائلة مفاتيح منفصلة، فهو يمنع الدخول المكرّر
            # حتى لو اختفت التهدئة. لا يستبدلها: التهدئة أرخص (GET واحد) وهي
            # من تمنع في الحالة الطبيعية.
            entered_at = _last_journal_entry(ex, symbol, dry=dry)
            if entered_at and now - entered_at < COOLDOWN_SECONDS:
                result["skipped"].append({
                    "symbol": symbol,
                    "reason": "recent_entry_in_journal",
                    "entered_at": entered_at,
                    "entered_at_iso": _iso(entered_at),
                })
                result["messages"].append(warn_message(
                    f"تخطّي {symbol}: سجل الدخول يحمل دخولاً في "
                    f"{_iso(entered_at)} (أقل من 24 ساعة) بينما مفتاح التهدئة "
                    f"غير موجود — تحقّق من حالة Redis"
                ))
                continue

            foreign = _foreign_position(ex, symbol)
            if foreign:
                result["skipped"].append({"symbol": symbol, "reason": f"foreign_{foreign}"})
                result["messages"].append(warn_message(
                    f"تخطّي {symbol}: فيه صفقة مفتوحة من استراتيجية أخرى على نفس "
                    f"العملة ({foreign}) — ادخل يدوياً إذا أردت الإشارة"
                ))
                continue

            usdt = ex.get_usdt_balance()
            if usdt < amount:
                result["skipped"].append({
                    "symbol": symbol, "reason": "insufficient_balance",
                    "available": _num(usdt), "required": _num(amount),
                })
                result["messages"].append(warn_message(
                    f"رصيد غير كافٍ: {_num(usdt)} USDT < {_num(amount)} — "
                    f"تخطّي {symbol}"
                ))
                continue

            # الحجز الذرّي، آخر خطوة قبل الشراء. كل الفحوص فوقه "اقرأ ثم
            # اكتب": بين قراءة open_key وكتابته في _open_position توجد نافذة
            # تمرّ منها نبضتان متوازيتان معاً فتشتريان نفس الرمز مرتين — وهذا
            # مثبّت في tests/test_liq_flush.py::test_two_concurrent_entries...
            # SET NX أمر واحد ذرّي في Redis، فواحدة فقط تكسب الحجز.
            # يُؤخذ بعد فحص الرصيد لا قبله حتى لا يُحجز رمز سيُتخطّى أصلاً.
            if not ex.redis_set_json(
                reserve_key(symbol, dry),
                {"since": now},
                ex_seconds=ENTRY_RESERVE_SECONDS,
                nx=True,
            ):
                result["skipped"].append({"symbol": symbol, "reason": "entry_reserved"})
                continue

            try:
                record = _open_position(ex, signal, now=now, dry=dry, amount=amount)
            except Exception:
                # تحرير الحجز عند فشل الشراء: وإلا بقي الرمز محجوزاً بلا مركز
                # حتى تنتهي المهلة، فتضيع إشارة صالحة في النبضة القادمة.
                # النجاح لا يحرّره: open_key والتهدئة وسجل الدخول تتولّى المنع
                # بعدها، والحجز ينتهي وحده بمهلته.
                ex.redis_delete(reserve_key(symbol, dry))
                raise

            open_count += 1

            result["entries"].append({
                "symbol": symbol,
                "status": "bought",
                "entry_price": record["entry_price"],
                "qty": record["qty"],
                "z_r4": record["z_r4"],
                "z_oi4": record["z_oi4"],
                "late_bars": record["late_bars"],
            })
            result["journal"].append(_journal_entry(record))
            result["messages"].append(buy_message(
                symbol,
                Decimal(record["entry_price"]),
                record["z_r4"],
                record["z_oi4"],
                record["late_bars"],
            ))

        except Exception as exc:  # noqa: BLE001
            logger.exception("liq entry failed for %s", symbol)
            result["errors"].append({"stage": "entry", "symbol": symbol, "error": str(exc)})
            result["messages"].append(warn_message(f"خطأ في دخول {symbol}: {exc}"))


# ============================================================
# النبضة الساعية
# ============================================================

def run_tick(ex: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """نبضة واحدة: خروج أولاً ثم فحص الإشارات ثم دخول.

    الخروج قبل الفحص وليس بعده: الفحص يسحب ~85 طلباً من Bybit ويستغرق ~60 ثانية،
    فلو قُتل العامل في منتصفه (gunicorn --timeout) تكون عمليات الخروج قد نُفّذت
    وثُبّتت في Redis أصلاً. العكس يترك مركزاً تجاوز الوقف معلّقاً ساعة كاملة.

    لا يرفع استثناءً أبداً: أي خطأ يعود في errors[] وmessages[] حتى لا تتحول
    مشكلة في Bybit إلى 500 يُسقط بقية مسارات التنفيذ.
    """
    params = params or {}
    dry = is_dry_run()
    enabled = is_enabled()
    now = int(time.time())

    result: dict[str, Any] = {
        "strategy": STRATEGY,
        "status": "ok",
        "enabled": enabled,
        "dry_run": dry,
        "time": now,
        "time_iso": _iso(now),
        "exits": [],
        "entries": [],
        "skipped": [],
        "journal": [],
        "messages": [],
        "errors": [],
    }

    # أدوات الاختبار: مقبولة في التشغيل التجريبي فقط. وجودها في التشغيل الحقيقي
    # يعني أن أحداً يقدر يفرض شراء رمز عبر الـ webhook، فنرفضها صريحاً.
    forced = params.get("force_signals")
    skip_scan = bool(params.get("skip_scan"))
    override_now = params.get("now")
    if (forced or skip_scan or override_now) and not dry:
        result["status"] = "overrides_rejected"
        result["errors"].append({
            "stage": "params",
            "error": "force_signals/skip_scan/now are accepted only when LIQ_DRY_RUN=true",
        })
        return result
    if override_now:
        now = int(override_now)
        result["time"] = now
        result["time_iso"] = _iso(now)

    # بصمة "النبضة وصلت المنفّذ". أسوأ أنواع الفشل هو الذي لا يظهر في أي سجل:
    # ثلاث نبضات في 2026-10-08 ما وصلت الحاوية أصلاً (خطأ DNS في عقدة n8n)،
    # فلا رسالة ولا صف ولا خطأ في errors[] — صمت تام. /liq_health يقرأ هذه
    # البصمة ويقيس عمرها، فيصير الصمت قابلاً للكشف من الخارج.
    # قبل المفتاح القاتل وقبل القفل: نبضة معطّلة أو مكرّرة وصلت فعلاً، وهذا
    # بالضبط ما نقيسه. وفشل الكتابة لا يُسقط النبضة — البصمة ليست الوظيفة.
    try:
        ex.redis_set_json(last_tick_key(dry), {"at": now, "at_iso": _iso(now)})
    except Exception:  # noqa: BLE001
        logger.exception("liq last_tick stamp failed")

    if not enabled:
        # المفتاح القاتل لا يهجر المراكز المفتوحة: مركز بلا خروج يبقى مفتوحاً
        # للأبد ويتجاوز الوقف. نمنع الدخول فقط، ونكمل الخروج.
        if not dry and _open_count(ex, dry) == 0:
            result["status"] = "disabled"
            return result
        result["status"] = "disabled_exits_only"

    lock = lock_key(dry)
    try:
        acquired = ex.redis_set_json(
            lock, {"since": now}, ex_seconds=TICK_LOCK_SECONDS, nx=True
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("liq tick lock failed")
        result["status"] = "redis_error"
        result["errors"].append({"stage": "lock", "error": str(exc)})
        result["messages"].append(warn_message(f"خطأ liq_flush (Redis): {exc}"))
        return result

    if not acquired:
        # ليس خطأ: n8n يعيد المحاولة بعد timeout والنبضة السابقة ما زالت تعمل.
        result["status"] = "locked"
        return result

    try:
        process_exits(ex, result, now=now, dry=dry)

        if result["status"] == "disabled_exits_only":
            return result

        if _open_count(ex, dry) >= MAX_OPEN:
            # لا داعي لإنفاق 60 ثانية على فحص لا يمكن أن يُنتج دخولاً.
            result["skipped"].append({"symbol": None, "reason": "max_open_scan_skipped"})
            return result

        if forced:
            signals = list(forced)
            result["scan"] = {"source": "forced", "signals": len(signals)}
        elif skip_scan:
            signals = []
            result["scan"] = {"source": "skipped", "signals": 0}
        else:
            import liq_signal  # استيراد متأخّر: pandas/numpy ثقيلة ولا تُحمَّل عند الإقلاع
            rows, sigs = liq_signal.scan()
            signals = sigs
            result["scan"] = {
                "source": "liq_signal",
                "checked": len(rows),
                "errors": sum(1 for r in rows if "error" in r),
                "signals": len(sigs),
            }
            if result["scan"]["errors"]:
                failed = [r["symbol"] for r in rows if "error" in r]
                result["messages"].append(warn_message(
                    f"فشل حساب الإشارة لـ {len(failed)} رمز: {', '.join(failed)}"
                ))

        if len(signals) > MAX_PER_SCAN:
            result["skipped"].extend(
                {"symbol": str(s.get("symbol")), "reason": "over_max_per_scan"}
                for s in signals[MAX_PER_SCAN:]
            )

        process_entries(ex, result, signals, now=now, dry=dry)

    except Exception as exc:  # noqa: BLE001 — المسار لا يرمي أبداً
        logger.exception("liq tick failed")
        result["status"] = "error"
        result["errors"].append({"stage": "tick", "error": str(exc)})
        result["messages"].append(warn_message(f"خطأ liq_flush: {exc}"))
    finally:
        # الإفراج دائماً: قفل باقٍ 15 دقيقة يعني نبضة ضائعة لا سبب لها.
        try:
            ex.redis_delete(lock)
        except Exception:  # noqa: BLE001
            logger.exception("liq tick lock release failed")

    return result


def health(ex: Any) -> dict[str, Any]:
    """قراءة فقط: هل النبضة الساعية تصل المنفّذ فعلاً؟

    لا تنادي Bybit ولا تلمس أي مركز — مفتاح Redis واحد فقط، فتصلح للنداء
    المتكرّر من مراقب خارجي بدون أن تنفق من حدود Bybit.

    stale = فات وقت نبضة كاملة ونصف بلا أن تصل أي نبضة. last_tick_at = null
    يعني أنه لم تصل نبضة واحدة منذ آخر إقلاع لـ Redis، وهو أيضاً stale.
    """
    dry = is_dry_run()
    now = int(time.time())
    out: dict[str, Any] = {
        "strategy": STRATEGY,
        "dry_run": dry,
        "enabled": is_enabled(),
        "now": now,
        "now_iso": _iso(now),
        "expected_interval_minutes": TICK_INTERVAL_MINUTES,
        "stale_after_minutes": STALE_AFTER_MINUTES,
        "last_tick_at": None,
        "last_tick_at_iso": None,
        "last_tick_age_minutes": None,
        "stale": True,
    }

    try:
        record = ex.redis_get_json(last_tick_key(dry))
    except Exception as exc:  # noqa: BLE001 — المسار لا يرمي، يُبلّغ
        out["error"] = str(exc)
        return out

    if not record:
        return out

    try:
        at = int(record["at"])
    except (KeyError, TypeError, ValueError) as exc:
        out["error"] = f"malformed last_tick record: {exc}"
        return out

    age = round((now - at) / 60, 2)
    out["last_tick_at"] = at
    out["last_tick_at_iso"] = _iso(at)
    out["last_tick_age_minutes"] = age
    out["stale"] = age > STALE_AFTER_MINUTES
    return out


def list_positions(ex: Any) -> dict[str, Any]:
    """قراءة فقط: مراكز liq_flush المفتوحة مع ربح/خسارة اللحظة."""
    dry = is_dry_run()
    out: list[dict[str, Any]] = []

    for key in ex.redis_keys(open_pattern(dry)):
        symbol = key.rsplit(":", 1)[-1]
        record = ex.redis_get_json(key)
        if not record:
            continue
        try:
            price = ex.get_ticker_price(symbol)
            qty = ex.as_decimal(record["qty"], "qty")
            entry_value = ex.as_decimal(record["entry_value"], "entry_value")
            value = price * qty
            pnl = value - entry_value
            out.append({
                **record,
                "current_price": _num(price),
                "current_value": _num(value),
                "pnl": _num(_round(pnl, "0.00000001")),
                "pnl_pct": _num(
                    _round(pnl / entry_value * Decimal("100"), "0.0001")
                ) if entry_value > 0 else "0",
                "hours_held": round((time.time() - int(record["entry_time"])) / 3600, 2),
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"symbol": symbol, "error": str(exc)})

    return {
        "strategy": STRATEGY,
        "dry_run": dry,
        "enabled": is_enabled(),
        "open": len(out),
        "max_open": MAX_OPEN,
        "result": out,
    }
