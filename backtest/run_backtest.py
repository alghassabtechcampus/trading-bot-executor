"""
نقطة التشغيل الرئيسية: يجلب البيانات (أو يحمّلها من الكاش)، يشغّل
نسخة أ (الحالية) ونسخة ب (المطوّرة)، ويطبع تقرير مقارنة جنباً لجنب.

تشغيل:
    cd backtest
    pip install -r requirements.txt
    python run_backtest.py
"""

from __future__ import annotations

import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from engine import run_backtest
from fetch_data import get_candles

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "LTCUSDT",
]

BACKTEST_DAYS = 90
GENERAL_BUFFER_CANDLES = 300      # ~25 ساعة إحماء لكل الرموز (EMA50/RSI/ATR)
BTC_LONG_BUFFER_CANDLES = 500     # إحماء إضافي لـ BTC (EMA200 الحقيقي)
FIVE_MIN_MS = 5 * 60 * 1000

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def _fetch_all() -> tuple[dict[str, list[dict]], list[dict], int]:
    now_ms = int(time.time() * 1000)
    backtest_start_ms = now_ms - BACKTEST_DAYS * 24 * 60 * 60 * 1000

    symbol_fetch_start_ms = backtest_start_ms - GENERAL_BUFFER_CANDLES * FIVE_MIN_MS
    btc_fetch_start_ms = backtest_start_ms - BTC_LONG_BUFFER_CANDLES * FIVE_MIN_MS

    print(f"نطاق الباكتست: {datetime.fromtimestamp(backtest_start_ms/1000, tz=timezone.utc)} → "
          f"{datetime.fromtimestamp(now_ms/1000, tz=timezone.utc)}")
    print()

    candles_by_symbol: dict[str, list[dict]] = {}
    for symbol in SYMBOLS:
        candles_by_symbol[symbol] = get_candles(symbol, symbol_fetch_start_ms, now_ms)

    print()
    btc_candles = get_candles("BTCUSDT", btc_fetch_start_ms, now_ms)

    return candles_by_symbol, btc_candles, backtest_start_ms


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {
            "count": 0, "win_rate": 0.0, "total_pnl_pct": 0.0,
            "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "max_consecutive_losses": 0,
        }

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
        "count": len(trades_sorted),
        "win_rate": len(wins) / len(trades_sorted) * 100,
        "total_pnl_pct": total_pnl_pct,
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "max_consecutive_losses": max_streak,
    }


def _save_trades_csv(trades: list[dict], path: str) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "entry_time_utc", "entry_price", "exit_time_utc", "exit_price",
                          "exit_reason", "score", "hold_minutes", "pnl_pct"])
        for t in sorted(trades, key=lambda x: x["entry_time"]):
            writer.writerow([
                t["symbol"],
                datetime.fromtimestamp(t["entry_time"] / 1000, tz=timezone.utc).isoformat(),
                round(t["entry_price"], 8),
                datetime.fromtimestamp(t["exit_time"] / 1000, tz=timezone.utc).isoformat(),
                round(t["exit_price"], 8),
                t["exit_reason"],
                round(t["score"], 2),
                round(t["hold_minutes"], 1),
                round(t["pnl_pct"], 4),
            ])


def _print_report(stats_a: dict, stats_b: dict) -> str:
    lines = []
    lines.append("# تقرير مقارنة الباكتست — نسخة أ (الحالية) مقابل نسخة ب (المطوّرة)\n")
    lines.append(f"فترة الباكتست: آخر {BACKTEST_DAYS} يوم | العملات: {', '.join(SYMBOLS)}\n")
    lines.append("\n⚠️ **قيد مهم:** بيانات Order Book وSpread التاريخية غير متوفرة من Bybit، "
                  "لذلك افتُرض تحقّقهما دائماً (`True`) في كلا النسختين. النتائج الفعلية الحية "
                  "قد تكون أضعف من هذا الباكتست بسبب هذا القيد (بعض الإشارات هنا كانت سترفض "
                  "فعلياً بسبب سبريد أو order book ضعيف لو توفرت البيانات).\n")
    lines.append("\nملاحظات منهجية إضافية: بدون انزلاق سعري (slippage)، تنفيذ فوري عند إغلاق "
                  "الشمعة، ورسوم تقريبية 0.2% ذهاب وإياب مطروحة من كل صفقة. "
                  "عند تلامس وقف الخسارة والهدف بنفس الشمعة (نادر)، يُفترض تنفيذ وقف الخسارة أولاً.\n")

    header = "| المقياس | نسخة أ (الحالية) | نسخة ب (فلتر BTC + ترتيب) |"
    sep = "|---|---|---|"
    rows = [
        ("عدد الصفقات", stats_a["count"], stats_b["count"]),
        ("نسبة النجاح", f"{stats_a['win_rate']:.1f}%", f"{stats_b['win_rate']:.1f}%"),
        ("إجمالي PnL%", f"{stats_a['total_pnl_pct']:+.2f}%", f"{stats_b['total_pnl_pct']:+.2f}%"),
        ("متوسط الربح/صفقة رابحة", f"{stats_a['avg_win_pct']:+.3f}%", f"{stats_b['avg_win_pct']:+.3f}%"),
        ("متوسط الخسارة/صفقة خاسرة", f"{stats_a['avg_loss_pct']:+.3f}%", f"{stats_b['avg_loss_pct']:+.3f}%"),
        ("أكبر سلسلة خسائر متتالية", stats_a["max_consecutive_losses"], stats_b["max_consecutive_losses"]),
    ]

    lines.append("\n" + header)
    lines.append(sep)
    for label, a, b in rows:
        lines.append(f"| {label} | {a} | {b} |")

    report = "\n".join(lines) + "\n"
    print(report)
    return report


def main() -> None:
    candles_by_symbol, btc_candles, backtest_start_ms = _fetch_all()

    print("\nتشغيل نسخة أ (بلا حد أقصى للصفقات المتزامنة)...")
    trades_a = run_backtest(
        SYMBOLS, candles_by_symbol, btc_candles,
        version="A", max_concurrent=None, backtest_start_ms=backtest_start_ms,
    )
    print(f"  ✓ {len(trades_a)} صفقة")

    print("\nتشغيل نسخة ب (فلتر BTC + ترتيب Score + حد صفقتين متزامنتين)...")
    trades_b = run_backtest(
        SYMBOLS, candles_by_symbol, btc_candles,
        version="B", max_concurrent=2, backtest_start_ms=backtest_start_ms,
    )
    print(f"  ✓ {len(trades_b)} صفقة")

    stats_a = _stats(trades_a)
    stats_b = _stats(trades_b)

    print()
    report = _print_report(stats_a, stats_b)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "comparison_report.md"), "w", encoding="utf-8") as f:
        f.write(report)

    _save_trades_csv(trades_a, os.path.join(RESULTS_DIR, "trades_version_a.csv"))
    _save_trades_csv(trades_b, os.path.join(RESULTS_DIR, "trades_version_b.csv"))

    print(f"\nالتقرير محفوظ بـ: {RESULTS_DIR}")


if __name__ == "__main__":
    main()