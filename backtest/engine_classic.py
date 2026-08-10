"""
محرك محاكاة عام للاستراتيجيات الكلاسيكية الثلاث — يقبل إشارات دخول/خروج
جاهزة (من strategies_classic.py) ويطبّق نفس قواعد المحاكاة الموحّدة على
الثلاثة معاً: أولوية الخروج (وقف ← إشارة الاستراتيجية الطبيعية ← هدف إن
وُجد ← max_hold_time)، رسوم 0.2% ذهاب وإياب، بلا حد أقصى للصفقات المتزامنة،
منع دخول جديد لنفس الرمز وعنده صفقة مفتوحة أصلاً.
"""

from __future__ import annotations

import pandas as pd

FEE_ROUNDTRIP_PCT = 0.2
MAX_HOLD_MINUTES = 90


def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume",
    })
    return df


def simulate_strategy(
    symbol: str,
    candles: list[dict],
    signals: dict,
    fee_pct: float = FEE_ROUNDTRIP_PCT,
    max_hold_minutes: float | None = MAX_HOLD_MINUTES,
) -> list[dict]:
    """
    max_hold_minutes=None يعني بلا حد أقصى للمدة — الخروج فقط بالوقف/الهدف/
    إشارة الاستراتيجية الطبيعية. أي صفقة تبقى مفتوحة لنهاية البيانات تُستبعد
    من النتائج (مو صفقة مكتملة، ما نقدر نحسب لها PnL حقيقي).
    """
    df = _candles_to_df(candles)
    n = len(df)

    timestamps = df["timestamp"].to_numpy()
    highs = df["High"].to_numpy()
    lows = df["Low"].to_numpy()
    closes = df["Close"].to_numpy()

    entry_arr = signals["entry"].to_numpy()
    stop_arr = signals["stop"].to_numpy() if signals["stop"] is not None else None
    target_arr = signals["target"].to_numpy() if signals["target"] is not None else None
    exit_signal_arr = signals["exit_signal"].to_numpy() if signals["exit_signal"] is not None else None

    trades = []
    position = None  # dict أو None

    for i in range(n):
        if position is not None:
            exit_price = None
            exit_reason = None

            if lows[i] <= position["stop"]:
                exit_price = position["stop"]
                exit_reason = "stop_loss"
            elif position["target"] is not None and highs[i] >= position["target"]:
                exit_price = position["target"]
                exit_reason = "take_profit"
            elif exit_signal_arr is not None and exit_signal_arr[i]:
                exit_price = closes[i]
                exit_reason = "signal_exit"
            elif max_hold_minutes is not None:
                hold_minutes = (timestamps[i] - position["entry_time"]) / 60000
                if hold_minutes >= max_hold_minutes:
                    exit_price = closes[i]
                    exit_reason = "max_hold_time"

            if exit_reason is not None:
                pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100 - fee_pct
                trades.append({
                    "symbol": symbol,
                    "entry_time": position["entry_time"],
                    "entry_price": position["entry_price"],
                    "exit_time": int(timestamps[i]),
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "hold_minutes": (timestamps[i] - position["entry_time"]) / 60000,
                    "pnl_pct": pnl_pct,
                })
                position = None

        if position is None and entry_arr[i]:
            stop_val = stop_arr[i] if stop_arr is not None else None
            target_val = target_arr[i] if target_arr is not None and not pd.isna(target_arr[i]) else None
            if stop_val is None or pd.isna(stop_val):
                continue
            position = {
                "entry_time": int(timestamps[i]),
                "entry_price": closes[i],
                "stop": stop_val,
                "target": target_val,
            }

    return trades