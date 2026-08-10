"""
محرك محاكاة Grid Trading — سبوت فقط (بلا Short، بلا رافعة).

التصميم المتفق عليه:
- نطاق ثابت من أعلى/أدنى سعر لآخر 7 أيام، 10 مستويات (9 "درجات" شراء).
- كل مستوى فارغ يُملأ فور لمس السعر له (Low <= level)، ويُباع عند وصول
  السعر للمستوى الذي فوقه مباشرة (High >= level_above).
- وقف نطاق موحّد = lower_bound × (1 - STOP_MARGIN): عند كسره تُغلق كل
  المستويات المفتوحة لنفس السعر فوراً، ويُعاد تعريف النطاق من جديد
  (نطاق ثابت بعدها بلا تجديد دوري، حسب الاتفاق).
- بلا max_hold_time (الحماية الوحيدة هي وقف النطاق نفسه).
- كسر الحد الأعلى يُتجاهَل عمداً (لا خطر بحساب سبوت بلا Short).
"""

from __future__ import annotations

import pandas as pd

FEE_ROUNDTRIP_PCT = 0.2
GRID_LEVELS = 10
LOOKBACK_DAYS = 7
STOP_MARGIN = 0.05
FIVE_MIN_MS = 5 * 60 * 1000
LOOKBACK_CANDLES = LOOKBACK_DAYS * 24 * 60 // 5  # 2016


def _make_grid(window_lows, window_highs):
    lower = min(window_lows)
    upper = max(window_highs)
    step = (upper - lower) / (GRID_LEVELS - 1)
    levels = [lower + i * step for i in range(GRID_LEVELS)]
    stop_price = lower * (1 - STOP_MARGIN)
    return {"lower": lower, "upper": upper, "levels": levels, "stop_price": stop_price}


def simulate_grid(symbol: str, candles: list[dict], backtest_start_ms: int) -> dict:
    n = len(candles)
    timestamps = [c["timestamp"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    if n <= LOOKBACK_CANDLES:
        return {"trades": [], "max_concurrent": 0, "reset_events": 0}

    trades = []
    open_lots: dict[int, dict] = {}  # level_index -> {"entry_time","entry_price"}
    max_concurrent = 0
    reset_events = 0

    grid = _make_grid(lows[0:LOOKBACK_CANDLES], highs[0:LOOKBACK_CANDLES])

    for idx in range(LOOKBACK_CANDLES, n):
        t = timestamps[idx]
        low = lows[idx]
        high = highs[idx]
        in_eval_window = t >= backtest_start_ms

        # ------------------------------------------------------------
        # 1) وقف النطاق الكامل: كسر الحد الأدنى بهامش الأمان
        # ------------------------------------------------------------
        if low <= grid["stop_price"]:
            for lvl_idx, lot in open_lots.items():
                pnl_pct = (grid["stop_price"] - lot["entry_price"]) / lot["entry_price"] * 100 - FEE_ROUNDTRIP_PCT
                trade = {
                    "symbol": symbol,
                    "entry_time": lot["entry_time"],
                    "entry_price": lot["entry_price"],
                    "exit_time": t,
                    "exit_price": grid["stop_price"],
                    "exit_reason": "range_stop",
                    "hold_minutes": (t - lot["entry_time"]) / 60000,
                    "pnl_pct": pnl_pct,
                }
                if lot["entry_time"] >= backtest_start_ms:
                    trades.append(trade)
            open_lots = {}

            window_start = max(0, idx - LOOKBACK_CANDLES + 1)
            grid = _make_grid(lows[window_start:idx + 1], highs[window_start:idx + 1])
            if in_eval_window:
                reset_events += 1
            continue  # نفس المنطق الموحّد أعلاه يكفي لهذي الشمعة

        # ------------------------------------------------------------
        # 2) بيع المستويات اللي وصل سعر هدفها
        # ------------------------------------------------------------
        for lvl_idx in list(open_lots.keys()):
            target_price = grid["levels"][lvl_idx + 1]
            if high >= target_price:
                lot = open_lots.pop(lvl_idx)
                pnl_pct = (target_price - lot["entry_price"]) / lot["entry_price"] * 100 - FEE_ROUNDTRIP_PCT
                trade = {
                    "symbol": symbol,
                    "entry_time": lot["entry_time"],
                    "entry_price": lot["entry_price"],
                    "exit_time": t,
                    "exit_price": target_price,
                    "exit_reason": "grid_sell",
                    "hold_minutes": (t - lot["entry_time"]) / 60000,
                    "pnl_pct": pnl_pct,
                }
                if lot["entry_time"] >= backtest_start_ms:
                    trades.append(trade)

        # ------------------------------------------------------------
        # 3) شراء أي مستوى فارغ لمسه السعر (كل المستويات المكسورة بنفس الشمعة)
        # ------------------------------------------------------------
        for lvl_idx in range(GRID_LEVELS - 1):  # المستويات 0..8 قابلة للشراء
            if lvl_idx in open_lots:
                continue
            level_price = grid["levels"][lvl_idx]
            if low <= level_price:
                open_lots[lvl_idx] = {"entry_time": t, "entry_price": level_price}

        if in_eval_window:
            max_concurrent = max(max_concurrent, len(open_lots))

    return {"trades": trades, "max_concurrent": max_concurrent, "reset_events": reset_events}