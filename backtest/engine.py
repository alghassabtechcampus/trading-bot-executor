"""
محرك المحاكاة: يمشي عبر الزمن (شمعة 5 دقائق كل خطوة) لكل الرموز معاً،
يفتح ويغلق صفقات حسب نسخة الاستراتيجية (أ أو ب)، ويرجّع سجل الصفقات
المُغلقة الكامل لحساب إحصائيات المقارنة.
"""

from __future__ import annotations

from strategy import evaluate_btc_filter, evaluate_signal, FEE_MARGIN_PERCENT

MAX_HOLD_MINUTES = 90
FIVE_MIN_MS = 5 * 60 * 1000

WINDOW_SIZE = 200
BTC_LONG_WINDOW_SIZE = 500


def _build_index(candles: list[dict]) -> dict[int, int]:
    return {c["timestamp"]: i for i, c in enumerate(candles)}


def _window_ending_at(candles: list[dict], idx: int, size: int) -> list[dict]:
    start = max(0, idx - size + 1)
    return candles[start:idx + 1]


def run_backtest(
    symbols: list[str],
    candles_by_symbol: dict[str, list[dict]],
    btc_candles: list[dict],
    version: str,
    max_concurrent: int | None = None,
    backtest_start_ms: int | None = None,
) -> list[dict]:
    """
    version: 'A' أو 'B'.
    max_concurrent: أقصى عدد صفقات مفتوحة متزامنة (None = بلا حد، نسخة أ).
    """
    assert version in ("A", "B")

    indices = {s: _build_index(candles_by_symbol[s]) for s in symbols}
    btc_index = _build_index(btc_candles)

    # الخط الزمني المرجعي: كل الطوابع الزمنية الموجودة بأي رمز، ضمن نطاق الباكتست
    all_timestamps: set[int] = set()
    for s in symbols:
        all_timestamps.update(indices[s].keys())
    timeline = sorted(t for t in all_timestamps if backtest_start_ms is None or t >= backtest_start_ms)

    open_positions: dict[str, dict] = {}
    closed_trades: list[dict] = []

    for t in timeline:
        # ------------------------------------------------------------
        # 1) فحص الخروج للصفقات المفتوحة (بنفس أولوية app.py: وقف، هدف، مدة قصوى)
        # ------------------------------------------------------------
        for symbol in list(open_positions.keys()):
            idx = indices[symbol].get(t)
            if idx is None:
                continue
            candle = candles_by_symbol[symbol][idx]
            pos = open_positions[symbol]

            exit_price = None
            exit_reason = None

            if candle["low"] <= pos["stop_loss"]:
                exit_price = pos["stop_loss"]
                exit_reason = "stop_loss"
            elif candle["high"] >= pos["take_profit"]:
                exit_price = pos["take_profit"]
                exit_reason = "take_profit"
            else:
                hold_minutes = (t - pos["entry_time"]) / 60000
                if hold_minutes >= MAX_HOLD_MINUTES:
                    exit_price = candle["close"]
                    exit_reason = "max_hold_time"

            if exit_reason is not None:
                pnl_pct_gross = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
                pnl_pct_net = pnl_pct_gross - FEE_MARGIN_PERCENT

                closed_trades.append({
                    "symbol": symbol,
                    "entry_time": pos["entry_time"],
                    "entry_price": pos["entry_price"],
                    "exit_time": t,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "score": pos["score"],
                    "hold_minutes": (t - pos["entry_time"]) / 60000,
                    "pnl_pct": pnl_pct_net,
                })
                del open_positions[symbol]

        # ------------------------------------------------------------
        # 2) تقييم إشارات جديدة للرموز بدون صفقة مفتوحة
        # ------------------------------------------------------------
        candidates = []
        for symbol in symbols:
            if symbol in open_positions:
                continue
            idx = indices[symbol].get(t)
            if idx is None or idx < 59:
                continue

            window = _window_ending_at(candles_by_symbol[symbol], idx, WINDOW_SIZE)
            signal = evaluate_signal(window)
            if signal is None or signal["action"] != "BUY_NOW":
                continue

            candidates.append((symbol, signal))

        if not candidates:
            continue

        # ------------------------------------------------------------
        # 3) فلترة/ترتيب حسب النسخة، ثم فتح الصفقات
        # ------------------------------------------------------------
        if version == "B":
            btc_idx = btc_index.get(t)
            if btc_idx is None or btc_idx < 59:
                continue

            btc_window = _window_ending_at(btc_candles, btc_idx, WINDOW_SIZE)
            btc_window_long = _window_ending_at(btc_candles, btc_idx, BTC_LONG_WINDOW_SIZE)

            if not evaluate_btc_filter(btc_window, btc_window_long):
                continue

            candidates.sort(key=lambda c: c[1]["score"], reverse=True)

            free_slots = max_concurrent - len(open_positions)
            candidates = candidates[:max(0, free_slots)]

        for symbol, signal in candidates:
            open_positions[symbol] = {
                "entry_time": t,
                "entry_price": signal["price"],
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "score": signal["score"],
            }

    return closed_trades