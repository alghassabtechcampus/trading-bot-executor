"""breakout.py — الجانب الحي لاستراتيجية "الاختراق + السوق صاعد" (breakout_btc).

الرياضيات كلها في bo_signal.py. هذا الملف ينفّذ فقط، وبنفس نمط liq_flush.py:

  الحَكَم : كل نبضة تحسب حالة البيتكوين اليومية وتحفظها في bo:regime، ولا
            يُفتح أي دخول إلا والبيتكوين صاعد. الخروج لا يتأثّر بالحَكَم.
  دخول    : SPOT market BUY بـ BO_USDT_PER_TRADE على إشارة شمعة 4 ساعات مغلقة
            عمرها ≤ ساعتين من إغلاقها (النبضة الساعية تلحق نبضة فائتة واحدة).
  خروج    : إغلاق شمعة 4 ساعات تحت الوقف المتحرك (آخر قاع مؤكد).
  حدود    : صفقة واحدة لكل عملة، أقصى MAX_OPEN، ونفس القمة لا تُدخَل مرتين.

الحالة كلها تحت bo: في Redis، والأوامر تحمل orderLinkId يبدأ بـ bo_، فلا تتقاطع
مع liq:* ولا trade:*/pending:*. ويُمرَّر `ex` (وحدة app) بالحقن مثل liq_flush
حتى تُختبر دورة كاملة على كائن مزيّف بلا شبكة.

تقاطع العملات: الدخول يُرفض على عملة فيها مركز liq_flush أو استراتيجية أخرى.
الاتجاه المعاكس (liq يدخل عملة يحملها breakout) غير ممنوع، مثل ما هو موثّق مع
الاستراتيجية D في handoff/LIQ_FLUSH_AR.md — البيع هنا مسقوف بالكمية المسجّلة
وبالرصيد معاً، فلا يبيع عملات غيره.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import bo_signal

logger = logging.getLogger("breakout")

STRATEGY = "breakout_btc"

# ============================================================
# أرقام مثبّتة — لا تُقرأ من البيئة (نفس منطق liq_flush)
# ============================================================
MAX_OPEN = len(bo_signal.SYMBOLS)        # صفقة واحدة لكل عملة كحد أقصى
MAX_SIGNAL_AGE_SECONDS = 2 * 60 * 60     # من إغلاق شمعة الـ 4 ساعات
BAR_SECONDS = 4 * 60 * 60
KLINE_LIMIT = 300                        # ~50 يوم من شموع 4 ساعات
DAILY_LIMIT = 200                        # ~200 يوم للحَكَم
TICK_LOCK_SECONDS = 15 * 60
ENTRY_RESERVE_SECONDS = 10 * 60
USED_HIGH_TTL_SECONDS = 120 * 24 * 60 * 60
CLOSED_TTL_SECONDS = 90 * 24 * 60 * 60
MISMATCH_FRACTION = Decimal("0.90")
MAX_EXIT_ATTEMPTS = 3
TICK_INTERVAL_MINUTES = 60
STALE_AFTER_MINUTES = 90


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """المفتاح القاتل. الافتراضي false."""
    return _flag("BO_ENABLED")


def is_dry_run() -> bool:
    return _flag("BO_DRY_RUN")


def usdt_per_trade() -> Decimal:
    return Decimal(os.getenv("BO_USDT_PER_TRADE", "10"))


# ============================================================
# مفاتيح Redis
# ============================================================

def _prefix(dry: bool) -> str:
    return "bo:dry" if dry else "bo"


def open_key(symbol: str, dry: bool) -> str:
    return f"{_prefix(dry)}:open:{symbol}"


def open_pattern(dry: bool) -> str:
    return f"{_prefix(dry)}:open:*"


def closed_key(symbol: str, stamp: int, dry: bool) -> str:
    return f"{_prefix(dry)}:closed:{symbol}:{stamp}"


def used_key(symbol: str, dry: bool) -> str:
    return f"{_prefix(dry)}:used:{symbol}"


def reserve_key(symbol: str, dry: bool) -> str:
    return f"{_prefix(dry)}:reserve:{symbol}"


def lock_key(dry: bool) -> str:
    return f"{_prefix(dry)}:tick:lock"


def last_tick_key(dry: bool) -> str:
    return f"{_prefix(dry)}:last_tick"


def regime_key(dry: bool) -> str:
    return f"{_prefix(dry)}:regime"


def order_link_id(side: str, symbol: str, stamp: int) -> str:
    return f"bo_{side}_{symbol}_{stamp}"[:36]


# ============================================================
# أدوات صغيرة
# ============================================================

def _iso(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_ms(ms: int) -> str:
    return _iso(int(ms) // 1000)


def _num(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _round(value: Decimal, places: str) -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _fee_of(order: dict[str, Any]) -> Decimal:
    try:
        return Decimal(str(order.get("cumExecFee") or "0"))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def warn_message(text: str) -> str:
    return f"⚠️ {text}"


def fetch_bars(ex: Any, symbol: str, interval: str, limit: int, now_ms: int) -> list[dict]:
    """شموع spot مغلقة فقط، الأقدم أولاً."""
    payload = ex.bybit_get("/v5/market/kline", {
        "category": "spot", "symbol": symbol, "interval": interval, "limit": str(limit),
    })
    rows = payload.get("result", {}).get("list", []) or []
    interval_ms = bo_signal.DAY_MS if interval == "D" else bo_signal.BAR_MS
    return bo_signal.parse_klines(rows, interval_ms, now_ms)


# ============================================================
# الحَكَم
# ============================================================

def compute_regime(ex: Any, now_ms: int) -> tuple[list[dict], dict[str, Any] | None]:
    daily = fetch_bars(ex, bo_signal.REGIME_SYMBOL, "D", DAILY_LIMIT, now_ms)
    flags = bo_signal.regime_flags(daily)
    return flags, bo_signal.regime_at(flags, now_ms)


def _regime_view(state: dict[str, Any] | None) -> dict[str, Any]:
    if not state:
        return {"up": False, "day": None, "reason": "no_data"}
    return {
        "up": bool(state["up"]),
        "day": _iso_ms(state["day"])[:10],
        "btc_close": state["close"],
        "last_low": state["last_low"],
        "prev_low": state["prev_low"],
    }


def regime_message(view: dict[str, Any]) -> str:
    if view["up"]:
        return (f"🟢 الحَكَم: البيتكوين صار صاعد (يومي {view['day']}) — "
                f"استراتيجية الاختراق مسموح لها تدخل")
    return (f"⏸️ الحَكَم: البيتكوين مو صاعد (يومي {view['day']}) — "
            f"لا دخول جديد للاختراق، والمراكز المفتوحة تكمل بوقفها")


def read_regime(ex: Any) -> dict[str, Any]:
    """قراءة فقط لآخر حالة محفوظة — للمسار /regime ولاستراتيجيات أخرى لاحقاً."""
    dry = is_dry_run()
    try:
        rec = ex.redis_get_json(regime_key(dry))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return rec or {"up": None, "reason": "no_tick_yet"}


# ============================================================
# الرسائل
# ============================================================

def buy_message(symbol: str, price: Decimal, stop: float, vol_ratio: float) -> str:
    risk = (float(price) - stop) / float(price) * 100 if price else 0
    return (f"🟢 شراء (اختراق): {symbol} بسعر {_num(price)} — وقف {stop:g} "
            f"({risk:.1f}%) — فوليوم {vol_ratio:.1f}× المتوسط")


def sell_message(symbol: str, pnl_pct: Decimal, pnl_usd: Decimal, *, dry: bool) -> str:
    mark = "🟢" if pnl_usd >= 0 else "🔴"
    tag = "🧪 تجريبي — " if dry else ""
    return (f"{tag}{mark} بيع (اختراق): {symbol} — إغلاق 4 ساعات تحت الوقف — "
            f"ربح/خسارة {pnl_pct:+.2f}% و {pnl_usd:+.2f}$")


# ============================================================
# الخروج
# ============================================================

def _journal_row(rec: dict[str, Any], event: str) -> dict[str, Any]:
    fees = Decimal(str(rec.get("entry_fee") or "0")) + Decimal(str(rec.get("exit_fee") or "0"))
    return {
        "strategy": STRATEGY,
        "event": event,
        "symbol": rec["symbol"],
        "entry_time": rec.get("entry_time_iso"),
        "entry_price": rec.get("entry_price"),
        "qty": rec.get("qty"),
        "exit_time": rec.get("exit_time_iso"),
        "exit_price": rec.get("exit_price"),
        "exit_reason": rec.get("exit_reason"),
        "stop_level": rec.get("stop"),
        "initial_stop": rec.get("initial_stop"),
        "fees": _num(fees),
        "pnl": rec.get("pnl"),
        "pnl_pct": rec.get("pnl_pct"),
        "hold_hours": rec.get("hold_hours"),
        "signal_bar": rec.get("signal_bar_iso"),
        "pivot_high": rec.get("pivot_high"),
        "vol_ratio": rec.get("vol_ratio"),
        "dry_run": rec.get("dry_run", False),
    }


def _finish(ex: Any, rec: dict[str, Any], *, now: int, dry: bool, exit_price: Decimal,
            exit_qty: Decimal, exit_value: Decimal, exit_fee: Decimal, order_id: str,
            reason: str, held_qty: Decimal) -> dict[str, Any]:
    entry_value = ex.as_decimal(rec["entry_value"], "entry_value")
    realised_entry = entry_value * (exit_qty / held_qty) if held_qty > 0 else entry_value
    pnl = _round(exit_value - realised_entry, "0.00000001")
    pnl_pct = _round(pnl / realised_entry * Decimal("100") if realised_entry > 0 else Decimal("0"),
                     "0.0001")
    closed = {
        **rec,
        "exit_time": now, "exit_time_iso": _iso(now),
        "exit_price": _num(exit_price), "exit_qty": _num(exit_qty),
        "exit_value": _num(exit_value), "exit_fee": _num(exit_fee),
        "exit_order_id": order_id, "exit_reason": reason,
        "pnl": _num(pnl), "pnl_pct": _num(pnl_pct),
        "hold_hours": round((now - int(rec["entry_time"])) / 3600, 2),
    }
    ex.redis_set_json(closed_key(rec["symbol"], now, dry), closed, ex_seconds=CLOSED_TTL_SECONDS)
    ex.redis_delete(open_key(rec["symbol"], dry))
    return closed


def _sell(ex: Any, rec: dict[str, Any], *, now: int, dry: bool, reason: str) -> dict[str, Any]:
    """بيع كامل الكمية المسجّلة. نفس احتياطات liq_flush._close_position."""
    symbol = rec["symbol"]
    held_qty = ex.as_decimal(rec["qty"], "qty")
    filters = ex.get_instrument_filters(symbol)
    step = filters["qty_step"]
    price_now = ex.get_ticker_price(symbol)

    if dry:
        qty = ex.floor_to_step(held_qty, step)
        closed = _finish(ex, rec, now=now, dry=dry, exit_price=price_now, exit_qty=qty,
                         exit_value=price_now * qty, exit_fee=Decimal("0"),
                         order_id=f"DRY-SELL-{symbol}-{now}", reason=reason, held_qty=held_qty)
        return {"symbol": symbol, "status": "sold", "closed": closed}

    target = ex.floor_to_step(held_qty * Decimal("0.999"), step)
    wallet = ex.get_coin_balance(ex.coin_from_symbol(symbol))["wallet_balance"]
    sell_qty = min(target, ex.floor_to_step(wallet, step))

    expected_sellable = target >= filters["min_qty"] and target * price_now >= filters["min_amt"]
    if expected_sellable and wallet < held_qty * MISMATCH_FRACTION:
        attempts = int(rec.get("exit_attempts") or 0) + 1
        if attempts < MAX_EXIT_ATTEMPTS:
            rec["exit_attempts"] = attempts
            ex.redis_set_json(open_key(symbol, dry), rec)
            return {"symbol": symbol, "status": "balance_mismatch", "attempts": attempts,
                    "remaining_qty": _num(wallet), "expected_qty": _num(held_qty)}
        closed = _finish(ex, rec, now=now, dry=dry, exit_price=price_now, exit_qty=wallet,
                         exit_value=wallet * price_now, exit_fee=Decimal("0"), order_id="",
                         reason="needs_manual_check", held_qty=held_qty)
        return {"symbol": symbol, "status": "needs_manual_check", "closed": closed,
                "remaining_qty": _num(wallet), "expected_qty": _num(held_qty)}

    if not (sell_qty > 0 and sell_qty >= filters["min_qty"]
            and sell_qty * price_now >= filters["min_amt"]):
        closed = _finish(ex, rec, now=now, dry=dry, exit_price=price_now, exit_qty=wallet,
                         exit_value=wallet * price_now, exit_fee=Decimal("0"), order_id="",
                         reason="dust_unsellable", held_qty=held_qty)
        return {"symbol": symbol, "status": "dust_unsellable", "closed": closed,
                "remaining_qty": _num(wallet)}

    resp = ex.create_market_sell(symbol, sell_qty, order_link_id("s", symbol, now))
    order_id = resp.get("result", {}).get("orderId")
    if not order_id:
        raise RuntimeError("Sell response does not contain orderId")
    order = ex.wait_for_fill(symbol, order_id)
    d = ex.execution_details(order)
    closed = _finish(ex, rec, now=now, dry=dry, exit_price=d["average_price"],
                     exit_qty=d["executed_qty"], exit_value=d["executed_value"],
                     exit_fee=_fee_of(order), order_id=str(d["order_id"]),
                     reason=reason, held_qty=held_qty)
    return {"symbol": symbol, "status": "sold", "closed": closed}


def process_exits(ex: Any, result: dict[str, Any], *, now: int, dry: bool) -> None:
    now_ms = now * 1000
    for key in ex.redis_keys(open_pattern(dry)):
        symbol = key.rsplit(":", 1)[-1]
        try:
            rec = ex.redis_get_json(key)
            if not rec:
                continue
            bars = fetch_bars(ex, symbol, "240", KLINE_LIMIT, now_ms)
            old_stop = float(rec["stop"])
            r = bo_signal.trail_and_check(
                bars, entry_bar_t=int(rec["signal_bar_t"]), stop=old_stop,
                checked_through=int(rec.get("checked_through") or rec["signal_bar_t"]),
            )
            rec["stop"] = r["stop"]
            rec["checked_through"] = r["checked_through"]

            if r["exit_bar"] is None:
                ex.redis_set_json(key, rec)
                item = {"symbol": symbol, "status": "holding", "stop": r["stop"],
                        "checked_through": _iso_ms(r["checked_through"])}
                if r["stop"] > old_stop:
                    item["stop_raised_from"] = old_stop
                result["exits"].append(item)
                continue

            rec["exit_bar_iso"] = _iso_ms(r["exit_bar"]["t"])
            rec["exit_bar_close"] = r["exit_bar"]["c"]
            sale = _sell(ex, rec, now=now, dry=dry, reason="trail_stop")
            result["exits"].append({k: v for k, v in sale.items() if k != "closed"})
            st = sale["status"]
            if st == "sold":
                c = sale["closed"]
                result["journal"].append(_journal_row(c, "exit"))
                result["messages"].append(sell_message(
                    symbol, Decimal(c["pnl_pct"]), Decimal(c["pnl"]), dry=dry))
            elif st in ("dust_unsellable", "needs_manual_check"):
                result["journal"].append(_journal_row(sale["closed"], "exit"))
                result["messages"].append(warn_message(
                    f"{symbol} (اختراق): ما انباع — {st}، المتبقي {sale['remaining_qty']} — "
                    f"راجع المنصة يدوياً"))
            elif st == "balance_mismatch":
                result["messages"].append(warn_message(
                    f"{symbol} (اختراق): الرصيد {sale['remaining_qty']} أقل بكثير من المسجّل "
                    f"{sale['expected_qty']} — لم يُبَع، محاولة {sale['attempts']}/{MAX_EXIT_ATTEMPTS}"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("breakout exit failed for %s", symbol)
            result["errors"].append({"stage": "exit", "symbol": symbol, "error": str(exc)})
            result["messages"].append(warn_message(f"خطأ في خروج {symbol} (اختراق): {exc}"))


# ============================================================
# الدخول
# ============================================================

def _foreign_position(ex: Any, symbol: str) -> str | None:
    try:
        if ex.tracked_trade(symbol):
            return "trade"
        if ex.pending_entry(symbol):
            return "pending"
        if ex.redis_get_json(f"liq:open:{symbol}"):
            return "liq_flush"
    except Exception:  # noqa: BLE001
        logger.exception("foreign position check failed for %s", symbol)
    return None


def _open_position(ex: Any, symbol: str, sig: dict[str, Any], *, now: int, dry: bool,
                   amount: Decimal) -> dict[str, Any]:
    if dry:
        price = ex.get_ticker_price(symbol)
        filters = ex.get_instrument_filters(symbol)
        qty = ex.floor_to_step(amount / price, filters["qty_step"])
        if qty <= 0 or qty < filters["min_qty"]:
            raise RuntimeError(f"dry-run qty {qty} below minimum for {symbol}")
        value, fee, order_id, status = price * qty, Decimal("0"), f"DRY-BUY-{symbol}-{now}", "DryFilled"
    else:
        resp = ex.create_market_buy(symbol, amount, order_link_id("b", symbol, now))
        order_id = resp.get("result", {}).get("orderId")
        if not order_id:
            raise RuntimeError("Buy response does not contain orderId")
        order = ex.wait_for_fill(symbol, order_id)
        d = ex.execution_details(order)
        price, qty, value = d["average_price"], d["executed_qty"], d["executed_value"]
        fee, order_id, status = _fee_of(order), str(d["order_id"]), str(d["status"])

    rec = {
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
        "stop": float(sig["stop"]),
        "initial_stop": float(sig["stop"]),
        "signal_bar_t": int(sig["bar_t"]),
        "signal_bar_iso": _iso_ms(sig["bar_t"]),
        "signal_close": sig["close"],
        "checked_through": int(sig["bar_t"]),
        "pivot_high": sig["pivot_high"],
        "pivot_high_t": int(sig["pivot_high_t"]),
        "vol_ratio": round(float(sig["vol_ratio"]), 2),
        "dry_run": dry,
    }
    ex.redis_set_json(open_key(symbol, dry), rec)
    ex.redis_set_json(used_key(symbol, dry),
                      {"pivot_high_t": rec["pivot_high_t"], "entered": now},
                      ex_seconds=USED_HIGH_TTL_SECONDS)
    return rec


def process_entries(ex: Any, result: dict[str, Any], *, now: int, dry: bool,
                    flags: list[dict], symbols: list[str]) -> None:
    now_ms = now * 1000
    amount = usdt_per_trade()
    open_count = len(ex.redis_keys(open_pattern(dry)))
    checked = errors = 0

    for symbol in symbols:
        try:
            if ex.redis_get_json(open_key(symbol, dry)):
                continue
            bars = fetch_bars(ex, symbol, "240", KLINE_LIMIT, now_ms)
            checked += 1
            if len(bars) < bo_signal.VOL_WINDOW + 2 * bo_signal.K + 2:
                continue
            sig = bo_signal.evaluate(bars)
            if not sig["signal"]:
                continue

            age = now - (sig["bar_t"] // 1000 + BAR_SECONDS)
            if age > MAX_SIGNAL_AGE_SECONDS:
                result["skipped"].append({"symbol": symbol, "reason": "signal_too_old",
                                          "age_minutes": round(age / 60, 1)})
                continue

            reg = bo_signal.regime_at(flags, sig["bar_t"])
            if not (reg and reg["up"]):
                result["skipped"].append({"symbol": symbol, "reason": "regime_not_up",
                                          "signal_bar": _iso_ms(sig["bar_t"])})
                continue

            used = ex.redis_get_json(used_key(symbol, dry))
            if used and int(used.get("pivot_high_t", -1)) == int(sig["pivot_high_t"]):
                result["skipped"].append({"symbol": symbol, "reason": "pivot_already_used"})
                continue

            if open_count >= MAX_OPEN:
                result["skipped"].append({"symbol": symbol, "reason": "max_open"})
                continue

            foreign = _foreign_position(ex, symbol)
            if foreign:
                result["skipped"].append({"symbol": symbol, "reason": f"foreign_{foreign}"})
                result["messages"].append(warn_message(
                    f"تخطّي اختراق {symbol}: فيه مركز مفتوح من {foreign} على نفس العملة"))
                continue

            usdt = ex.get_usdt_balance()
            if usdt < amount:
                result["skipped"].append({"symbol": symbol, "reason": "insufficient_balance",
                                          "available": _num(usdt)})
                result["messages"].append(warn_message(
                    f"رصيد غير كافٍ لاختراق {symbol}: {_num(usdt)} USDT < {_num(amount)}"))
                continue

            if not ex.redis_set_json(reserve_key(symbol, dry), {"since": now},
                                     ex_seconds=ENTRY_RESERVE_SECONDS, nx=True):
                result["skipped"].append({"symbol": symbol, "reason": "entry_reserved"})
                continue
            try:
                rec = _open_position(ex, symbol, sig, now=now, dry=dry, amount=amount)
            except Exception:
                ex.redis_delete(reserve_key(symbol, dry))
                raise

            open_count += 1
            result["entries"].append({"symbol": symbol, "status": "bought",
                                      "entry_price": rec["entry_price"], "qty": rec["qty"],
                                      "stop": rec["stop"], "vol_ratio": rec["vol_ratio"]})
            result["journal"].append(_journal_row(rec, "entry"))
            result["messages"].append(buy_message(symbol, Decimal(rec["entry_price"]),
                                                  rec["stop"], rec["vol_ratio"]))
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.exception("breakout entry failed for %s", symbol)
            result["errors"].append({"stage": "entry", "symbol": symbol, "error": str(exc)})
            result["messages"].append(warn_message(f"خطأ في فحص اختراق {symbol}: {exc}"))

    result["scan"] = {"checked": checked, "errors": errors}


# ============================================================
# النبضة الساعية
# ============================================================

def run_tick(ex: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """خروج أولاً، ثم الحَكَم، ثم الدخول. لا يرمي استثناءً أبداً."""
    params = params or {}
    dry = is_dry_run()
    enabled = is_enabled()
    now = int(time.time())

    result: dict[str, Any] = {
        "strategy": STRATEGY, "status": "ok", "enabled": enabled, "dry_run": dry,
        "time": now, "time_iso": _iso(now), "regime": None,
        "exits": [], "entries": [], "skipped": [], "journal": [], "messages": [], "errors": [],
    }

    override_now = params.get("now")
    if override_now and not dry:
        result["status"] = "overrides_rejected"
        result["errors"].append({"stage": "params", "error": "now is accepted only when BO_DRY_RUN=true"})
        return result
    if override_now:
        now = int(override_now)
        result["time"], result["time_iso"] = now, _iso(now)

    try:
        ex.redis_set_json(last_tick_key(dry), {"at": now, "at_iso": _iso(now)})
    except Exception:  # noqa: BLE001
        logger.exception("breakout last_tick stamp failed")

    if not enabled:
        if not dry and not ex.redis_keys(open_pattern(dry)):
            result["status"] = "disabled"
            return result
        result["status"] = "disabled_exits_only"

    try:
        acquired = ex.redis_set_json(lock_key(dry), {"since": now},
                                     ex_seconds=TICK_LOCK_SECONDS, nx=True)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "redis_error"
        result["errors"].append({"stage": "lock", "error": str(exc)})
        result["messages"].append(warn_message(f"خطأ breakout (Redis): {exc}"))
        return result
    if not acquired:
        result["status"] = "locked"
        return result

    try:
        process_exits(ex, result, now=now, dry=dry)

        # الحَكَم يُحسب ويُحفظ حتى لو الدخول معطّل: يفيد المراقبة ويُستخدم لاحقاً
        # لاستراتيجيات أخرى (مفتاح bo:regime).
        try:
            flags, state = compute_regime(ex, now * 1000)
        except Exception as exc:  # noqa: BLE001
            logger.exception("regime failed")
            result["errors"].append({"stage": "regime", "error": str(exc)})
            result["messages"].append(warn_message(f"فشل حساب الحَكَم (BTC يومي): {exc} — لا دخول"))
            return result
        view = _regime_view(state)
        result["regime"] = view
        prev = ex.redis_get_json(regime_key(dry)) or {}
        if prev.get("up") is not None and bool(prev.get("up")) != view["up"]:
            result["messages"].append(regime_message(view))
        ex.redis_set_json(regime_key(dry), {**view, "updated": now, "updated_iso": _iso(now)})

        if result["status"] == "disabled_exits_only":
            return result

        if not view["up"]:
            result["skipped"].append({"symbol": None, "reason": "regime_not_up_scan_skipped"})
            return result

        symbols = params.get("symbols") if dry and params.get("symbols") else bo_signal.SYMBOLS
        process_entries(ex, result, now=now, dry=dry, flags=flags, symbols=list(symbols))
    except Exception as exc:  # noqa: BLE001
        logger.exception("breakout tick failed")
        result["status"] = "error"
        result["errors"].append({"stage": "tick", "error": str(exc)})
        result["messages"].append(warn_message(f"خطأ breakout: {exc}"))
    finally:
        try:
            ex.redis_delete(lock_key(dry))
        except Exception:  # noqa: BLE001
            logger.exception("breakout lock release failed")
    return result


def health(ex: Any) -> dict[str, Any]:
    dry = is_dry_run()
    now = int(time.time())
    out = {"strategy": STRATEGY, "dry_run": dry, "enabled": is_enabled(), "now_iso": _iso(now),
           "last_tick_at_iso": None, "last_tick_age_minutes": None, "stale": True}
    try:
        rec = ex.redis_get_json(last_tick_key(dry))
        if rec:
            age = round((now - int(rec["at"])) / 60, 2)
            out.update(last_tick_at_iso=_iso(int(rec["at"])), last_tick_age_minutes=age,
                       stale=age > STALE_AFTER_MINUTES)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def list_positions(ex: Any) -> dict[str, Any]:
    dry = is_dry_run()
    out = []
    for key in ex.redis_keys(open_pattern(dry)):
        rec = ex.redis_get_json(key)
        if not rec:
            continue
        try:
            price = ex.get_ticker_price(rec["symbol"])
            qty = ex.as_decimal(rec["qty"], "qty")
            ev = ex.as_decimal(rec["entry_value"], "entry_value")
            pnl = price * qty - ev
            out.append({**rec, "current_price": _num(price), "pnl": _num(_round(pnl, "0.00000001")),
                        "pnl_pct": _num(_round(pnl / ev * Decimal("100"), "0.0001")) if ev > 0 else "0",
                        "hours_held": round((time.time() - int(rec["entry_time"])) / 3600, 2)})
        except Exception as exc:  # noqa: BLE001
            out.append({"symbol": rec.get("symbol"), "error": str(exc)})
    return {"strategy": STRATEGY, "dry_run": dry, "enabled": is_enabled(),
            "open": len(out), "max_open": MAX_OPEN, "regime": read_regime(ex), "result": out}
