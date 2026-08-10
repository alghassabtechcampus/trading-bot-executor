"""
نقطة تشغيل استراتيجية Grid Trading (نسخة ز) + مقارنة مع النسخ السابقة.

تشغيل:
    cd backtest
    ../venv/Scripts/python.exe run_backtest_grid.py
"""

from __future__ import annotations

import csv
import os
import sys
import time
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from engine_grid import GRID_LEVELS, LOOKBACK_DAYS, STOP_MARGIN, simulate_grid
from fetch_data import get_candles

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "LINKUSDT", "LTCUSDT",
]
BACKTEST_DAYS = 90
FIVE_MIN_MS = 5 * 60 * 1000
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"count": 0, "win_rate": 0.0, "total_pnl_pct": 0.0,
                "avg_pnl_pct": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
                "max_consecutive_losses": 0}

    trades_sorted = sorted(trades, key=lambda t: t["entry_time"])
    wins = [t for t in trades_sorted if t["pnl_pct"] > 0]
    losses = [t for t in trades_sorted if t["pnl_pct"] <= 0]
    total = sum(t["pnl_pct"] for t in trades_sorted)

    max_streak = 0
    current_streak = 0
    for t in trades_sorted:
        if t["pnl_pct"] <= 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    return {
        "count": len(trades_sorted),
        "win_rate": len(wins) / len(trades_sorted) * 100,
        "total_pnl_pct": total,
        "avg_pnl_pct": total / len(trades_sorted),
        "avg_win_pct": sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0.0,
        "avg_loss_pct": sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0.0,
        "max_consecutive_losses": max_streak,
    }


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


def _load_stats_from_csv(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    trades = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append({"entry_time": row["entry_time_utc"], "pnl_pct": float(row["pnl_pct"])})
    return _stats(trades)


def main() -> None:
    now_ms = int(time.time() * 1000)
    backtest_start_ms = now_ms - BACKTEST_DAYS * 24 * 60 * 60 * 1000
    fetch_start_ms = backtest_start_ms - (LOOKBACK_DAYS + 1) * 24 * 60 * 60 * 1000  # +1 يوم هامش أمان

    print(f"إعدادات الشبكة: {GRID_LEVELS} مستويات | نطاق {LOOKBACK_DAYS} أيام | وقف {STOP_MARGIN*100:.0f}% تحت الحد الأدنى\n")
    print("تحميل بيانات 5 دقائق...")

    all_trades = []
    per_symbol_meta = {}
    for symbol in SYMBOLS:
        candles = get_candles(symbol, fetch_start_ms, now_ms)
        result = simulate_grid(symbol, candles, backtest_start_ms)
        all_trades.extend(result["trades"])
        per_symbol_meta[symbol] = {"max_concurrent": result["max_concurrent"], "reset_events": result["reset_events"]}
        print(f"  {symbol}: {len(result['trades'])} صفقة | أقصى تكدّس={result['max_concurrent']} | "
              f"عدد مرات وقف النطاق={result['reset_events']}")

    stats_grid = _stats(all_trades)
    _save_trades_csv(all_trades, os.path.join(RESULTS_DIR, "trades_version_ز.csv"))

    total_resets = sum(m["reset_events"] for m in per_symbol_meta.values())
    max_concurrent_overall = max(m["max_concurrent"] for m in per_symbol_meta.values())

    print(f"\n✓ إجمالي: {len(all_trades)} صفقة | أقصى تكدّس عبر كل العملات={max_concurrent_overall} | "
          f"إجمالي مرات وقف النطاق={total_resets}")

    stats_a = _load_stats_from_csv(os.path.join(RESULTS_DIR, "trades_version_a.csv"))
    stats_b = _load_stats_from_csv(os.path.join(RESULTS_DIR, "trades_version_b.csv"))
    stats_d = _load_stats_from_csv(os.path.join(RESULTS_DIR, "trades_version_د.csv"))
    stats_e = _load_stats_from_csv(os.path.join(RESULTS_DIR, "trades_version_هـ.csv"))
    stats_f = _load_stats_from_csv(os.path.join(RESULTS_DIR, "trades_version_و.csv"))

    all_stats = {"أ": stats_a, "ب": stats_b, "د": stats_d, "هـ": stats_e, "و": stats_f, "ز (Grid)": stats_grid}

    lines = []
    lines.append("# تقرير المقارنة الكامل مع نسخة ز (Grid Trading)\n")
    lines.append(f"Grid: {GRID_LEVELS} مستويات، نطاق {LOOKBACK_DAYS} أيام (ثابت، بلا تجديد دوري)، "
                  f"وقف {STOP_MARGIN*100:.0f}% تحت الحد الأدنى، بلا max_hold_time، رسوم 0.2%.\n")
    lines.append(f"\n**إحصاءات خاصة بالشبكة:** أقصى عدد مستويات مفتوحة بنفس الوقت (عبر كل العملات) = "
                 f"{max_concurrent_overall} | إجمالي مرات تفعيل وقف النطاق الكامل = {total_resets} صفقة تحفّظية عبر 90 يوم/9 عملات\n")

    header = "| المقياس | أ | ب | د | هـ | و | ز (Grid) |"
    sep = "|---|---|---|---|---|---|---|"
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
        ("متوسط PnL/صفقة", "avg_pnl_pct", "pct3"),
        ("متوسط الربح/صفقة رابحة", "avg_win_pct", "pct3"),
        ("متوسط الخسارة/صفقة خاسرة", "avg_loss_pct", "pct3"),
        ("أكبر سلسلة خسائر متتالية", "max_consecutive_losses", None),
    ]
    for label, key, kind in rows:
        vals = [fmt(all_stats[col], key, kind) for col in ["أ", "ب", "د", "هـ", "و", "ز (Grid)"]]
        lines.append(f"| {label} | " + " | ".join(vals) + " |")

    report = "\n".join(lines) + "\n"
    print("\n" + report)

    with open(os.path.join(RESULTS_DIR, "comparison_report_grid.md"), "w", encoding="utf-8") as f:
        f.write(report)

    print(f"محفوظ بـ: {RESULTS_DIR}\\comparison_report_grid.md")


if __name__ == "__main__":
    main()