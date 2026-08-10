"""
التشخيص الأخير: هل المشكلة رسوم التنفيذ، أو غياب ميزة حقيقية بالإشارة
نفسها؟ + هل max_hold_time=90 دقيقة يظلم Donchian وBandtastic (استراتيجيتا
اتجاه/إشارة طبيعية، لم تُصمَّما لأفق قصير)؟

منهجية "بدون رسوم": الرسوم كانت تُطرح كقيمة ثابتة (0.2) من كل صفقة بلا
استثناء (pnl_pct_net = pnl_pct_gross - 0.2) — نعيد بناء pnl الإجمالي
(gross) رياضياً من سجلات الصفقات المحفوظة أصلاً (gross = net + 0.2)،
بدون أي إعادة محاكاة (نفس الصفقات بالضبط، فقط طرحنا الرسوم أو لا).

منهجية "بدون قيد وقت": إعادة محاكاة فعلية لـ Donchian وBandtastic فقط
(الوحيدتين المتأثرتين، عندهما إشارة خروج طبيعية مستقلة عن الوقت)،
بإزالة شرط max_hold_time بالكامل — الخروج فقط بالوقف أو الإشارة
الطبيعية. الرسوم تبقى 0.2% بهذا المتغيّر (نعزل متغيّر واحد بكل اختبار).
"""

from __future__ import annotations

import csv
import os
import sys
import time
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from engine_classic import _candles_to_df, simulate_strategy
from fetch_data import get_candles
from strategies_classic import bandtastic_signals, donchian_signals

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "LINKUSDT", "LTCUSDT",
]
BACKTEST_DAYS = 90
GENERAL_BUFFER_CANDLES = 300
FIVE_MIN_MS = 5 * 60 * 1000
FEE_PCT = 0.2

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")

VERSION_FILES = {
    "أ": "trades_version_a.csv",
    "ب": "trades_version_b.csv",
    "د": "trades_version_د.csv",
    "هـ": "trades_version_هـ.csv",
    "و": "trades_version_و.csv",
}


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"count": 0, "win_rate": 0.0, "total_pnl_pct": 0.0, "avg_pnl_pct": 0.0}
    wins = [t for t in trades if t["pnl_pct"] > 0]
    total = sum(t["pnl_pct"] for t in trades)
    return {
        "count": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "total_pnl_pct": total,
        "avg_pnl_pct": total / len(trades),
    }


def _load_trades_with_fee(path: str) -> list[dict]:
    trades = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append({"pnl_pct": float(row["pnl_pct"])})
    return trades


def _gross_trades(trades_with_fee: list[dict]) -> list[dict]:
    return [{"pnl_pct": t["pnl_pct"] + FEE_PCT} for t in trades_with_fee]


def main() -> None:
    print("=== 1) إعادة بناء نتائج 'بدون رسوم' من سجلات الصفقات المحفوظة (بلا إعادة محاكاة) ===\n")

    results = {}
    for label, filename in VERSION_FILES.items():
        path = os.path.join(RESULTS_DIR, filename)
        if not os.path.exists(path):
            print(f"  تحذير: {filename} غير موجود، تخطّي {label}")
            continue
        with_fee = _load_trades_with_fee(path)
        without_fee = _gross_trades(with_fee)
        results[label] = {
            "with_fee": _stats(with_fee),
            "no_fee": _stats(without_fee),
        }
        print(f"  {label}: {len(with_fee)} صفقة | مع رسوم avg={results[label]['with_fee']['avg_pnl_pct']:+.4f}% "
              f"| بدون رسوم avg={results[label]['no_fee']['avg_pnl_pct']:+.4f}%")

    print("\n=== 2) إعادة محاكاة د وو بدون قيد max_hold_time (أفقها الزمني الطبيعي) ===\n")

    now_ms = int(time.time() * 1000)
    backtest_start_ms = now_ms - BACKTEST_DAYS * 24 * 60 * 60 * 1000
    fetch_start_ms = backtest_start_ms - GENERAL_BUFFER_CANDLES * FIVE_MIN_MS

    candles_by_symbol = {s: get_candles(s, fetch_start_ms, now_ms) for s in SYMBOLS}

    for label, signal_fn in [("د", donchian_signals), ("و", bandtastic_signals)]:
        all_trades = []
        still_open_count = 0
        for symbol in SYMBOLS:
            candles = candles_by_symbol[symbol]
            df = _candles_to_df(candles)
            signals = signal_fn(df)
            trades = simulate_strategy(symbol, candles, signals, fee_pct=FEE_PCT, max_hold_minutes=None)
            trades = [t for t in trades if t["entry_time"] >= backtest_start_ms]
            all_trades.extend(trades)

        stats_unlimited = _stats(all_trades)
        results[label]["no_time_limit"] = stats_unlimited
        avg_hold_h = sum(t["hold_minutes"] for t in all_trades) / len(all_trades) / 60 if all_trades else 0
        print(f"  {label}: {len(all_trades)} صفقة مكتملة (بلا قيد وقت) | avg={stats_unlimited['avg_pnl_pct']:+.4f}% "
              f"| متوسط مدة الاحتفاظ={avg_hold_h:.1f} ساعة")

    # ------------------------------------------------------------
    # التقرير النهائي
    # ------------------------------------------------------------
    lines = []
    lines.append("# التشخيص الأخير — مع رسوم / بدون رسوم / بدون قيد وقت\n")
    lines.append("منهجية 'بدون رسوم': إعادة بناء رياضي من نفس الصفقات المحفوظة (gross = net + 0.2%)، "
                  "بلا إعادة محاكاة. منهجية 'بدون قيد وقت': إعادة محاكاة فعلية لـ Donchian وBandtastic "
                  "فقط (رسوم 0.2% ثابتة، فقط أزلنا سقف 90 دقيقة) — متغيّر واحد بكل اختبار.\n")

    header = "| الاستراتيجية | # صفقات (أساس) | متوسط PnL/صفقة (مع رسوم) | متوسط PnL/صفقة (بدون رسوم) | متوسط PnL/صفقة (بدون قيد وقت) |"
    sep = "|---|---|---|---|---|"
    lines.append("\n" + header)
    lines.append(sep)

    for label in ["أ", "ب", "د", "هـ", "و"]:
        if label not in results:
            continue
        r = results[label]
        with_fee = f"{r['with_fee']['avg_pnl_pct']:+.4f}%"
        no_fee = f"{r['no_fee']['avg_pnl_pct']:+.4f}%"
        no_time = f"{r['no_time_limit']['avg_pnl_pct']:+.4f}% ({r['no_time_limit']['count']} صفقة)" if "no_time_limit" in r else "—"
        lines.append(f"| {label} | {r['with_fee']['count']} | {with_fee} | {no_fee} | {no_time} |")

    report = "\n".join(lines) + "\n"
    print("\n" + report)

    with open(os.path.join(RESULTS_DIR, "final_diagnostic.md"), "w", encoding="utf-8") as f:
        f.write(report)

    print(f"محفوظ بـ: {RESULTS_DIR}\\final_diagnostic.md")


if __name__ == "__main__":
    main()