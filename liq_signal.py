"""
liq_signal.py — كاشف إشارة "التصفيات الإجبارية" (نسخة البث المباشر)
=====================================================================
نفس قواعد الاختبار بالضبط (لا تعدّل الأرقام):
  - شموع ساعة من Bybit (linear) + Open Interest ساعي
  - r4  = تغير السعر خلال 4 ساعات   | oi4 = تغير الـ OI خلال 4 ساعات
  - z محسوب على آخر 720 ساعة (30 يوم) — الماضي فقط
  - إشارة: z_r4 <= -3  و  z_oi4 <= -3

تُفحص آخر EVAL_BARS شمعة مغلقة لا آخر واحدة فقط. السبب: الإشارة تُقرأ في النبضة
التي تلي إغلاق الشمعة، فنبضة فاشلة واحدة كانت تعني إشارة ضائعة للأبد — وهذا حصل
فعلاً: إشارة AVAX على شمعة 2026-10-08 17:00 ضاعت لأن نبضة 18:02 فشلت بخطأ DNS
(handoff/LIQ_STATUS_CHECK_AR.md). الخروج يلحق نفسه لأنه يقارن طوابع زمنية مطلقة،
أما الدخول فلا. كل إشارة تحمل late_bars = كم شمعة تأخّرت (0 = آخر شمعة مغلقة)،
و liq_flush يرفض الدخول إذا تجاوز عمر شمعة الإشارة MAX_SIGNAL_AGE_SECONDS.

الاستخدام:
  python liq_signal.py          → يطبع حالة كل العملات الآن (فحص يدوي بدون تداول)
  from liq_signal import scan   → يرجع قائمة الإشارات (للبوت)
"""

import time

import numpy as np
import pandas as pd
import requests

BASE = "https://api.bybit.com"
# 18 عملة الاختبار، ناقص LINK (ما ينباع عبر الـ API على الحساب الحقيقي)
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "ADAUSDT", "DOGEUSDT",
           "AVAXUSDT", "LTCUSDT", "DOTUSDT", "TRXUSDT", "ATOMUSDT", "NEARUSDT",
           "BCHUSDT", "ETCUSDT", "FILUSDT", "UNIUSDT"]
Z_WIN = 720
LOOKBACK = 4
Z_THRESH = -3.0
BARS = Z_WIN + 60          # كم شمعة نسحب
# كم شمعة مغلقة نفحص في كل نبضة. 2 = آخر شمعة والتي قبلها، فنبضة فاشلة واحدة
# تلحقها النبضة التالية. لا تُزاد بلا سبب: كل شمعة إضافية تعني السماح بإشارة
# أقدم، وحد العمر في liq_flush يرفضها أصلاً.
EVAL_BARS = 2


def _get(path, params):
    for attempt in range(5):
        try:
            j = requests.get(BASE + path, params=params, timeout=15).json()
            if j.get("retCode") == 0:
                return j["result"]
        except Exception:  # noqa
            pass
        time.sleep(1 + attempt)
    raise RuntimeError(f"Bybit failed: {path} {params.get('symbol')}")


def fetch(sym):
    """آخر BARS شمعة ساعة + OI ساعي — الشموع المغلقة فقط."""
    kl, end = [], None
    while len(kl) < BARS:
        p = dict(category="linear", symbol=sym, interval="60", limit=1000)
        if end:
            p["end"] = end
        lst = _get("/v5/market/kline", p)["list"]
        if not lst:
            break
        kl += lst
        end = int(lst[-1][0]) - 1
    k = pd.DataFrame(kl, columns=["ts", "open", "high", "low", "close", "volume", "turnover"]).astype(float)
    k["ts"] = pd.to_datetime(k["ts"].astype("int64"), unit="ms", utc=True)
    k = k.drop_duplicates("ts").set_index("ts").sort_index()

    oi, cursor = [], None
    while len(oi) < BARS:
        p = dict(category="linear", symbol=sym, intervalTime="1h", limit=200)
        if cursor:
            p["cursor"] = cursor
        res = _get("/v5/market/open-interest", p)
        oi += res["list"]
        cursor = res.get("nextPageCursor")
        if not cursor or not res["list"]:
            break
    o = pd.DataFrame(oi)
    o["ts"] = pd.to_datetime(o["timestamp"].astype("int64"), unit="ms", utc=True)
    o["oi"] = o["openInterest"].astype(float)
    o = o.drop_duplicates("ts").set_index("ts").sort_index()[["oi"]]

    df = k.join(o, how="inner")
    now_hour = pd.Timestamp.now(tz="UTC").floor("h")
    return df[df.index < now_hour]            # نشيل الشمعة اللي لسا مفتوحة


def evaluate(df, offset=0):
    """يحسب z لشمعة مغلقة واحدة. نفس معادلات الاختبار حرفياً.

    offset=0 آخر شمعة مغلقة، offset=1 التي قبلها. متوسط النافذة وانحرافها
    مُزاحان بـ shift(1) أي من ماضي الشمعة وحده، فقيمة z للشمعة الأقدم هي نفسها
    التي كانت ستُحسب لها في نبضتها — لا تتغيّر بمرور الوقت ولا تنظر للمستقبل.
    """
    df = df.copy()
    df["r4"] = df["close"] / df["close"].shift(LOOKBACK) - 1
    df["oi4"] = df["oi"] / df["oi"].shift(LOOKBACK) - 1
    i = -1 - offset
    out = {}
    for c in ["r4", "oi4"]:
        m = df[c].rolling(Z_WIN, min_periods=Z_WIN // 2).mean().shift(1)
        s = df[c].rolling(Z_WIN, min_periods=Z_WIN // 2).std().shift(1)
        out["z_" + c] = ((df[c] - m) / s).iloc[i]
    last = df.iloc[i]
    return dict(bar=df.index[i], close=float(last["close"]),
                r4=float(df["r4"].iloc[i]), oi4=float(df["oi4"].iloc[i]),
                z_r4=float(out["z_r4"]), z_oi4=float(out["z_oi4"]),
                late_bars=offset,
                signal=bool(out["z_r4"] <= Z_THRESH and out["z_oi4"] <= Z_THRESH))


def scan(symbols=SYMBOLS, eval_bars=EVAL_BARS):
    """يرجع (كل النتائج، الإشارات فقط) مرتبة من الأقوى.

    rows: صف واحد لكل رمز من **آخر شمعة مغلقة**. هذا ما يقيسه scan.checked في
    liq_flush، فعدده يساوي عدد الرموز دائماً مهما كان eval_bars.
    sigs: الإشارات على آخر eval_bars شمعة، بإشارة واحدة لكل رمز على الأكثر —
    لو أشارت الشمعتان فهما نفس حدث التصفية، فنأخذ الأحدث (late_bars الأصغر)
    ونرمي الأقدم هنا بدل الاعتماد على التهدئة لتصفية التكرار لاحقاً.
    """
    rows, sigs = [], []
    for s in symbols:
        try:
            df = fetch(s)
            bars = max(1, min(eval_bars, len(df)))
            evals = [evaluate(df, offset) for offset in range(bars)]
            for r in evals:
                r["symbol"] = s
            rows.append(evals[0])
            hit = next((r for r in evals if r["signal"]), None)
            if hit is not None:
                sigs.append(hit)
        except Exception as e:  # noqa
            rows.append(dict(symbol=s, error=str(e), signal=False))
    sigs.sort(key=lambda r: r["z_r4"] + r["z_oi4"])   # الأكثر تطرفاً أولاً
    return rows, sigs


if __name__ == "__main__":
    rows, sigs = scan()
    print(f"{'symbol':10} {'bar(UTC)':17} {'r4%':>7} {'oi4%':>7} {'z_r4':>6} {'z_oi4':>6}  signal")
    for r in rows:
        if "error" in r:
            print(f"{r['symbol']:10} ERROR {r['error']}")
            continue
        print(f"{r['symbol']:10} {r['bar']:%Y-%m-%d %H:%M} {r['r4']*100:7.2f} {r['oi4']*100:7.2f} "
              f"{r['z_r4']:6.2f} {r['z_oi4']:6.2f}  {'🔔 YES' if r['signal'] else '-'}")
    for s in sigs:
        late = f"  ⏱ متأخرة {s['late_bars']} شمعة" if s.get("late_bars") else ""
        print(f"  🔔 {s['symbol']:10} شمعة {s['bar']:%Y-%m-%d %H:%M} "
              f"z {s['z_r4']:.2f}/{s['z_oi4']:.2f}{late}")
    print(f"\nإشارات الآن: {len(sigs)}")
