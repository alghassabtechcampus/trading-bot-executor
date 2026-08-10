"""
نقطة تشغيل الاستراتيجيات الكلاسيكية الثلاث (د: Donchian، هـ: Bollinger+RSI،
و: Bandtastic) + مقارنة شاملة مع نسختي أ وب المحفوظتين مسبقاً.

تشغيل:
    cd backtest
    ../venv/Scripts/python.exe run_backtest_classic.py
"""

from __future__ import annotations

import csv
import os
import sys
import time
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from engine_classic import MAX_HOLD_MINUTES, simulate_strategy
from fetch_data import get_candles
from strategies_classic import bandtastic_signals, bollinger_rsi_signals, donchian_signals

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "LINKUSDT", "LTCUSDT",
]

BACKTEST_DAYS = 90
GENERAL_BUFFER_CANDLES = 300
FIVE_MIN_MS = 5 * 60 * 1000

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")

STRATEGIES = {
    "د (Donchian)": donchian_signals,
    "هـ (Bollinger+RSI)": bollinger_rsi_signals,
    "و (Bandtastic)": bandtastic_signals,
}


def _load_all_candles() -> tuple[dict[str, list[dict]], int]:
    now_ms = int(time.time() * 1000)
    backtest_start_ms = now_ms - BACKTEST_DAYS * 24 * 60 * 60 * 1000
    fetch_start_ms = backtest_start_ms - GENERAL_BUFFER_CANDLES * FIVE_MIN_MS

    candles: dict[str, list[dict]] = {}
    for symbol in SYMBOLS:
        candles[symbol] = get_candles(symbol, fetch_start_ms, now_ms)

    return candles, backtest_start_ms


def _stats_from_trades(trades: list[dict]) -> dict:
    if not trades:
        return {"count": 0, "win_rate": 0.0, "total_pnl_pct": 0.0,
                "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "max_consecutive_losses": 0}

    trades_sorted = sorted(trades, key=lambda t: t["entry_time"])
    wins = [t for t in trades_sorted if t["pnl_pct"] > 0]
    losses = [t for t in trades_sorted if t["pnl_pct"] <= 0]

    total_pnl_pct = sum(t["pnl_pct"] for t in trades_sorted)
    avg_win_pct = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0.0
    avg_loss_pct = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0.0

    max_streak = 0
    current_streak = 0
    for t in trades_sorted:
        if t["pnl_pct"] <= 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    return {
        "count": len(trades_sorted), "win_rate": len(wins) / len(trades_sorted) * 100,
        "total_pnl_pct": total_pnl_pct, "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct, "max_consecutive_losses": max_streak,
    }


def _load_stats_from_csv(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    trades = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append({"entry_time": row["entry_time_utc"], "pnl_pct": float(row["pnl_pct"])})
    return _stats_from_trades(trades)


def _save_trades_csv(trades: list[dict], path: str) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "entry_time_utc", "entry_price", "exit_time_utc", "exit_price",
                          "exit_reason", "hold_minutes", "pnl_pct"])
        for t in sorted(trades, key=lambda x: x["entry_time"]):
            writer.writerow([
                t["symbol"],
                datetime.fromtimestamp(t["entry_time"] / 1000, tz=timezone.utc).isoformat(),
                round(t["entry_price"], 8),
                datetime.fromtimestamp(t["exit_time"] / 1000, tz=timezone.utc).isoformat(),
                round(t["exit_price"], 8),
                t["exit_reason"],
                round(t["hold_minutes"], 1),
                round(t["pnl_pct"], 4),
            ])


def main() -> None:
    print("تحميل بيانات 5 دقائق (من الكاش، بدون سحب إضافي)...")
    candles_by_symbol, backtest_start_ms = _load_all_candles()
    for s in SYMBOLS:
        print(f"  {s}: {len(candles_by_symbol[s])} شمعة")

    results_by_strategy: dict[str, list[dict]] = {}

    for label, signal_fn in STRATEGIES.items():
        print(f"\nتشغيل {label}...")
        all_trades = []
        for symbol in SYMBOLS:
            candles = candles_by_symbol[symbol]
            from engine_classic import _candles_to_df
            df = _candles_to_df(candles)
            signals = signal_fn(df)
            trades = simulate_strategy(symbol, candles, signals)
            trades = [t for t in trades if t["entry_time"] >= backtest_start_ms]
            all_trades.extend(trades)
        print(f"  ✓ {len(all_trades)} صفقة")
        results_by_strategy[label] = all_trades

        safe_name = label.split(" ")[0]
        _save_trades_csv(all_trades, os.path.join(RESULTS_DIR, f"trades_version_{safe_name}.csv"))

    stats = {label: _stats_from_trades(trades) for label, trades in results_by_strategy.items()}

    path_a = os.path.join(RESULTS_DIR, "trades_version_a.csv")
    path_b = os.path.join(RESULTS_DIR, "trades_version_b.csv")
    stats_a = _load_stats_from_csv(path_a)
    stats_b = _load_stats_from_csv(path_b)

    all_stats = {"أ": stats_a, "ب": stats_b, **{k.split(" ")[0]: v for k, v in stats.items()}}

    lines = []
    lines.append("# تقرير مقارنة شامل — أ / ب / د (Donchian) / هـ (Bollinger+RSI) / و (Bandtastic)\n")
    lines.append(f"نفس بيانات 90 يوم / 9 عملات / فريم 5 دقائق / رسوم 0.2% ذهاب وإياب / "
                  f"max_hold={MAX_HOLD_MINUTES} دقيقة / بلا حد أقصى صفقات متزامنة.\n")

    header = "| المقياس | أ | ب | د (Donchian) | هـ (Bollinger+RSI) | و (Bandtastic) |"
    sep = "|---|---|---|---|---|---|"
    lines.append("\n" + header)
    lines.append(sep)

    def fmt(s, key, kind):
        if s is None:
            return "—"
        v = s[key]
        if kind == "pct1":
            return f"{v:.1f}%"
        if kind == "pct2":
            return f"{v:+.2f}%"
        if kind == "pct3":
            return f"{v:+.3f}%"
        return str(v)

    rows = [
        ("عدد الصفقات", "count", None),
        ("نسبة النجاح", "win_rate", "pct1"),
        ("إجمالي PnL%", "total_pnl_pct", "pct2"),
        ("متوسط الربح/صفقة رابحة", "avg_win_pct", "pct3"),
        ("متوسط الخسارة/صفقة خاسرة", "avg_loss_pct", "pct3"),
        ("أكبر سلسلة خسائر متتالية", "max_consecutive_losses", None),
    ]
    for label, key, kind in rows:
        vals = [fmt(all_stats[col], key, kind) for col in ["أ", "ب", "د", "هـ", "و"]]
        lines.append(f"| {label} | " + " | ".join(vals) + " |")

    report = "\n".join(lines) + "\n"
    print("\n" + report)

    with open(os.path.join(RESULTS_DIR, "comparison_report_5way.md"), "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\nالتقرير محفوظ بـ: {RESULTS_DIR}\\comparison_report_5way.md")


if __name__ == "__main__":
    main()