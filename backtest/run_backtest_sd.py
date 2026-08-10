"""
نقطة تشغيل نسخة ج (Supply & Demand، شموع الساعة) + مقارنة ثلاثية مع
نسختي أ وب المحفوظتين مسبقاً بـ results/trades_version_{a,b}.csv.

تشغيل:
    cd backtest
    ../venv/Scripts/python.exe run_backtest_sd.py
"""

from __future__ import annotations

import csv
import os
import sys
import time
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from engine_sd import MAX_HOLD_HOURS, run_backtest_sd
from fetch_data import get_candles
from resample import resample_to_1h

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "LINKUSDT", "LTCUSDT",
]

BACKTEST_DAYS = 90
EXTRA_ZONE_WARMUP_DAYS = 40  # نضج مناطق العرض/الطلب قبل بداية فترة التقييم
FIVE_MIN_MS = 5 * 60 * 1000

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def _fetch_wide_range() -> tuple[dict[str, list[dict]], int]:
    now_ms = int(time.time() * 1000)
    backtest_start_ms = now_ms - BACKTEST_DAYS * 24 * 60 * 60 * 1000
    fetch_start_ms = backtest_start_ms - EXTRA_ZONE_WARMUP_DAYS * 24 * 60 * 60 * 1000

    print(f"نطاق سحب البيانات (5 دقائق): {datetime.fromtimestamp(fetch_start_ms/1000, tz=timezone.utc)} → "
          f"{datetime.fromtimestamp(now_ms/1000, tz=timezone.utc)}")
    print(f"(بداية تقييم الصفقات الفعلية: {datetime.fromtimestamp(backtest_start_ms/1000, tz=timezone.utc)})")
    print()

    candles_5m: dict[str, list[dict]] = {}
    for symbol in SYMBOLS:
        candles_5m[symbol] = get_candles(symbol, fetch_start_ms, now_ms)

    return candles_5m, backtest_start_ms


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


def _load_stats_from_csv(path: str) -> dict:
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
                          "exit_reason", "quality_score", "rr", "hold_minutes", "pnl_pct"])
        for t in sorted(trades, key=lambda x: x["entry_time"]):
            writer.writerow([
                t["symbol"],
                datetime.fromtimestamp(t["entry_time"] / 1000, tz=timezone.utc).isoformat(),
                round(t["entry_price"], 8),
                datetime.fromtimestamp(t["exit_time"] / 1000, tz=timezone.utc).isoformat(),
                round(t["exit_price"], 8),
                t["exit_reason"],
                t["score"],
                t["rr"],
                round(t["hold_minutes"], 1),
                round(t["pnl_pct"], 4),
            ])


def main() -> None:
    candles_5m, backtest_start_ms = _fetch_wide_range()

    print("\nتجميع شموع 5 دقائق لفريم ساعة واحدة...")
    candles_1h = {s: resample_to_1h(candles_5m[s]) for s in SYMBOLS}
    for s in SYMBOLS:
        print(f"  {s}: {len(candles_1h[s])} شمعة ساعة")

    print(f"\nتشغيل نسخة ج (S&D — max_hold={MAX_HOLD_HOURS} ساعة، بلا حد أقصى للصفقات المتزامنة)...")
    trades_c = run_backtest_sd(SYMBOLS, candles_1h, backtest_start_ms)
    print(f"  ✓ {len(trades_c)} صفقة")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    _save_trades_csv(trades_c, os.path.join(RESULTS_DIR, "trades_version_c.csv"))

    stats_c = _stats_from_trades(trades_c)

    path_a = os.path.join(RESULTS_DIR, "trades_version_a.csv")
    path_b = os.path.join(RESULTS_DIR, "trades_version_b.csv")
    stats_a = _load_stats_from_csv(path_a) if os.path.exists(path_a) else None
    stats_b = _load_stats_from_csv(path_b) if os.path.exists(path_b) else None

    lines = []
    lines.append("# تقرير مقارنة ثلاثي — نسخة أ / نسخة ب / نسخة ج (S&D)\n")
    lines.append(f"نسخة ج: فريم ساعة، {BACKTEST_DAYS} يوم تقييم + {EXTRA_ZONE_WARMUP_DAYS} يوم إحماء لنضج المناطق, "
                  f"max_hold={MAX_HOLD_HOURS} ساعة (معايَر من 90 دقيقة على شموع 5 دقائق).\n")
    lines.append("\n⚠️ **قيود منهجية نسخة ج:** نفس قيد Order Book/Spread (غير متوفر تاريخياً، لذا Fee=0.2% فقط "
                  "يمثل التكلفة). النوافذ الزمنية الأصلية بـsd_engine (بوحدة 'يوم تداول TASI') أُعيدت معايرتها "
                  "×24 لتطابق فريم الساعة زمنياً؛ الثوابت الهيكلية (نافذة تأكيد القمة/القاع، عدد شموع القاعدة "
                  "1-6، عتبة الجودة 5/6، النسب المئوية) أُبقيت بلا تغيير. الدخول بسعر `zone top` (وليس سعر "
                  "إغلاق الشمعة كما بنسختي أ/ب) لأن هذا جزء أصيل من منطق sd_engine نفسه.\n")

    header = "| المقياس | نسخة أ | نسخة ب | نسخة ج (S&D) |"
    sep = "|---|---|---|---|"
    lines.append("\n" + header)
    lines.append(sep)

    def fmt(stats, key, kind):
        if stats is None:
            return "—"
        v = stats[key]
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
        a = fmt(stats_a, key, kind) if stats_a else "—"
        b = fmt(stats_b, key, kind) if stats_b else "—"
        c = fmt(stats_c, key, kind) if kind else str(stats_c[key])
        lines.append(f"| {label} | {a} | {b} | {c} |")

    report = "\n".join(lines) + "\n"
    print("\n" + report)

    with open(os.path.join(RESULTS_DIR, "comparison_report_3way.md"), "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\nالتقرير محفوظ بـ: {RESULTS_DIR}\\comparison_report_3way.md")
    print(f"سجل صفقات نسخة ج: {RESULTS_DIR}\\trades_version_c.csv")


if __name__ == "__main__":
    main()