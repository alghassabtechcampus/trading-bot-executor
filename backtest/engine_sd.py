"""
محرك محاكاة نسخة ج — استراتيجية العرض والطلب (Supply & Demand) من sd_engine
مطبّقة على شموع الساعة للكريبتو، بنفس أسلوب scan_stock الأصلي (إعادة حساب
المناطق من الصفر عند كل نقطة زمنية، مشي للأمام Walk-Forward).

معايرة النوافذ الزمنية: كل الثوابت اللي كانت "بعدد أيام تداول TASI" بالأصل
(شمعة = يوم) أُعيد ضربها ×RESCALE_FACTOR (=24) لتحافظ على نفس المعنى
الزمني الحقيقي على شموع الساعة. الثوابت الهيكلية/النسبية (نافذة تأكيد
القمة/القاع left=5,right=5، عدد شموع القاعدة 1-6، النسب المئوية،
عتبة الجودة 5/6) بقيت بلا تغيير — انظر التقرير النهائي للتفصيل الكامل.
"""

from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0])  # يضمن قابلية "import sd_engine" بغض النظر عن CWD

import pandas as pd

from sd_engine.trend import find_swings, detect_trend_at
from sd_engine.zones import (
    find_zones, evaluate_zone_quality, compute_zone_setup,
    is_staircase_shape, is_historical_low,
)
from sd_engine.indicators import compute_atr
from sd_engine.pcp import detect_pcp

RESCALE_FACTOR = 24  # شمعة يوم TASI -> 24 شمعة ساعة كريبتو

# نوافذ زمنية مُعاد معايرتها (×24) — القيم الأصلية بالتعليق.
# هذي نوافذ "مدة زمنية حقيقية" فعلاً (كم يوم انتظار، مقارنة بحجم تداول حديث):
LOOKAHEAD_BARS = 15 * RESCALE_FACTOR                  # 360  (كان 15 يوم)
RETEST_WINDOW_BARS = 20 * RESCALE_FACTOR               # 480  (كان 20 يوم)
VOLUME_LOOKBACK_BARS = 20 * RESCALE_FACTOR             # 480  (كان 20 يوم)
RECENT_HIGH_LOOKBACK = 10 * RESCALE_FACTOR             # 240  (كان 10 أيام)
MICRO_DOWNTREND_LOOKBACK = 10 * RESCALE_FACTOR         # 240  (كان 10 أيام)
VOLUME_SPIKE_LOOKBACK = 20 * RESCALE_FACTOR            # 480  (كان 20 يوم)
MAX_HOLD_HOURS = 18                                     # معايَر من 90 دقيقة/شموع 5د = 18 شمعة

# ثوابت هيكلية/نسبية — بلا تغيير عن sd_engine الأصلية (بما فيها MIN_OPPOSITE_GAP،
# رغم إنه بالأصل "8 أيام": تحقّقنا تجريبياً إنه فعلياً مقياس تباعد نسبي بين
# نقاط سوينق متتالية (مبني على نافذة left/right الهيكلية غير المُعايَرة)، مو
# مدة زمنية حقيقية. تجربة أولى برقم مُعايَر (192) أنتجت صفر مناطق طلب بالكامل
# مقابل عشرات مناطق عرض (69 عرض / 0 طلب على BTC) - رصدنا التحيّز، رجعنا
# للرقم الأصلي (8) وتحقق التوازن الطبيعي (~46 طلب / ~53 عرض). انظر التقرير
# النهائي لتفاصيل هذا التصحيح.
SWING_LEFT = 5
SWING_RIGHT = 5
MIN_OPPOSITE_GAP = 8
MIN_MOVE_PCT = 3.5
MIN_QUALITY_SCORE = 5
MIN_RR = 2.0
ATR_PERIOD = 14
FEE_ROUNDTRIP_PCT = 0.2  # نفس افتراض نسختي أ/ب للمقارنة العادلة

WINDOW_BARS = 1500  # نافذة متحركة تكفي بسهولة أكبر نافذة داخلية (480) + هامش


def _candles_to_df(candles_1h: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles_1h)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("dt").sort_index()
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume",
    })
    return df[["Open", "High", "Low", "Close", "Volume"]]


def _evaluate_symbol_at(window_df: pd.DataFrame) -> dict | None:
    """
    يعيد بناء المناطق من الصفر على window_df (نفس أسلوب scan_stock)، ويرجّع
    أفضل إشارة PCP صالحة لآخر شمعة بالنافذة (اليوم/الساعة الحالية)، أو None.
    """
    df_sw = find_swings(window_df, left=SWING_LEFT, right=SWING_RIGHT)
    all_zones = find_zones(
        df_sw, min_move_pct=MIN_MOVE_PCT,
        lookahead_bars=LOOKAHEAD_BARS, min_opposite_gap=MIN_OPPOSITE_GAP,
    )
    if not all_zones:
        return None

    atr_series = compute_atr(window_df, period=ATR_PERIOD)

    candidates = []
    for zone in all_zones:
        if zone["type"] != "demand":
            continue

        local_trend = detect_trend_at(df_sw, zone["end_pos"])
        if local_trend != "up":
            continue

        if is_staircase_shape(zone, window_df):
            continue

        atr_value = atr_series.iloc[zone["end_pos"]] if zone["end_pos"] < len(atr_series) else None

        quality = evaluate_zone_quality(
            zone, window_df, all_zones, atr_value=atr_value,
            volume_lookback=VOLUME_LOOKBACK_BARS,
        )
        if quality["score"] < MIN_QUALITY_SCORE:
            continue

        sig = detect_pcp(
            window_df, zone, local_trend, all_zones, atr_value=atr_value,
            min_rr=MIN_RR, retest_window_days=RETEST_WINDOW_BARS,
            recent_high_lookback=RECENT_HIGH_LOOKBACK,
            micro_downtrend_lookback=MICRO_DOWNTREND_LOOKBACK,
            volume_spike_lookback=VOLUME_SPIKE_LOOKBACK,
        )
        if not sig:
            continue

        sig["quality_score"] = quality["score"]
        sig["is_historical_low"] = is_historical_low(zone, window_df)
        sig["rank"] = quality["score"] * 10 + (5 if sig["is_historical_low"] else 0) + sig["rr"]
        candidates.append(sig)

    if not candidates:
        return None

    return max(candidates, key=lambda s: s["rank"])


def run_backtest_sd(
    symbols: list[str],
    candles_1h_by_symbol: dict[str, list[dict]],
    backtest_start_ms: int,
) -> list[dict]:
    dfs = {s: _candles_to_df(candles_1h_by_symbol[s]) for s in symbols}

    open_positions: dict[str, dict] = {}
    closed_trades: list[dict] = []

    for symbol in symbols:
        df = dfs[symbol]
        n = len(df)

        for idx in range(n):
            ts = df.index[idx]
            ts_ms = int(ts.value // 1_000_000)

            # ------------------------------------------------------------
            # 1) فحص الخروج للصفقة المفتوحة (لو وجدت): وقف، هدف، مدة قصوى معايَرة
            # ------------------------------------------------------------
            pos = open_positions.get(symbol)
            if pos is not None:
                candle = df.iloc[idx]
                exit_price = None
                exit_reason = None

                if candle["Low"] <= pos["stop"]:
                    exit_price = pos["stop"]
                    exit_reason = "stop_loss"
                elif candle["High"] >= pos["target"]:
                    exit_price = pos["target"]
                    exit_reason = "take_profit"
                else:
                    hold_hours = (ts_ms - pos["entry_time"]) / 3_600_000
                    if hold_hours >= MAX_HOLD_HOURS:
                        exit_price = candle["Close"]
                        exit_reason = "max_hold_time"

                if exit_reason is not None:
                    pnl_pct_gross = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
                    pnl_pct_net = pnl_pct_gross - FEE_ROUNDTRIP_PCT

                    closed_trades.append({
                        "symbol": symbol,
                        "entry_time": pos["entry_time"],
                        "entry_price": pos["entry_price"],
                        "exit_time": ts_ms,
                        "exit_price": exit_price,
                        "exit_reason": exit_reason,
                        "score": pos["quality_score"],
                        "rr": pos["rr"],
                        "hold_minutes": (ts_ms - pos["entry_time"]) / 60000,
                        "pnl_pct": pnl_pct_net,
                    })
                    del open_positions[symbol]

            if symbol in open_positions:
                continue  # لسه مفتوحة (ما انقفلت هالساعة) - لا داعي نبحث عن إشارة جديدة

            # ------------------------------------------------------------
            # 2) بحث عن إشارة جديدة (فقط ضمن فترة التقييم الفعلية، بعد فترة
            #    الإحماء اللازمة لنضج المناطق)
            # ------------------------------------------------------------
            if ts_ms < backtest_start_ms:
                continue
            if idx < 100:  # حد أدنى بسيط لضمان بيانات كافية لأي مؤشر
                continue

            start = max(0, idx - WINDOW_BARS + 1)
            window_df = df.iloc[start: idx + 1]

            sig = _evaluate_symbol_at(window_df)
            if sig is None:
                continue

            open_positions[symbol] = {
                "entry_time": ts_ms,
                "entry_price": sig["entry"],
                "stop": sig["stop"],
                "target": sig["target"],
                "quality_score": sig["quality_score"],
                "rr": sig["rr"],
            }

    return closed_trades