"""
اختبار سريع: فكرة "التصفيات الإجبارية" (Liquidation Flush)
================================================================
الفرضية (مكتوبة قبل الاختبار):
  لما ينزل السعر بقوة خلال 4 ساعات، وينهار الـ Open Interest معه في نفس الوقت
  (= بيع إجباري / تصفيات)، السعر يرتد بعدها أكثر من العادي.
  شراء سبوت فقط — بدون رافعة وبدون بيع على المكشوف.

الإعدادات مثبتة مسبقاً (لا تعدلها بعد ما تشوف النتيجة):
  - 9 عملات، شموع ساعة، فترة التطوير فقط: 2022-01-01 → 2026-08-25
    (فترة الحكم من 2026-08-26 مقفولة — لا تستخدمها هنا)
  - الحدث: z(عائد 4 ساعات) <= -3  و  z(تغير OI خلال 4 ساعات) <= -3
    (z محسوب على نافذة 30 يوم سابقة فقط — بدون نظر للمستقبل)
  - الدخول: افتتاح الشمعة التالية. الآفاق: 4 / 12 / 24 / 48 ساعة
  - التكلفة: 0.20% ذهاب وعودة (سبوت taker)
  - بعد كل حدث: تجاهل نفس العملة 24 ساعة
  - الأحداث المتزامنة بين العملات (خلال 6 ساعات) = حلقة واحدة مستقلة

التشغيل:
  pip install pandas numpy requests
  python liquidation_flush_test.py
  (أول مرة يحمّل البيانات من Bybit ويحفظها في مجلد data_liq/ — المرات الجاية يقرأ منها)
"""

import os
import sys
import time

import numpy as np
import pandas as pd

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
           "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LTCUSDT"]
START = "2022-01-01"
DEV_END = "2026-08-25 23:00"      # آخر ساعة في فترة التطوير
Z_WIN = 24 * 30                   # نافذة z-score: 30 يوم
LOOKBACK = 4                      # ساعات
Z_THRESH = -3.0                   # الحد الرئيسي (مثبت مسبقاً)
Z_SENS = [-2.5, -3.5]             # فحص حساسية فقط — مو للاختيار
HORIZONS = [4, 12, 24, 48]
COST = 0.002
COOLDOWN = 24
EPISODE_GAP = 6
N_RAND = 2000
SEED = 42
DATA_DIR = "data_liq"
BASE = "https://api.bybit.com"


# ----------------------------------------------------------------- تحميل البيانات
def _get(path, params):
    import requests
    for attempt in range(5):
        try:
            r = requests.get(BASE + path, params=params, timeout=20)
            j = r.json()
            if j.get("retCode") == 0:
                return j["result"]
            print("  retCode", j.get("retCode"), j.get("retMsg"))
        except Exception as e:  # noqa
            print("  retry:", e)
        time.sleep(1 + attempt)
    raise RuntimeError(f"failed {path} {params}")


def fetch_klines(sym, start_ms, end_ms):
    rows, end = [], end_ms
    while True:
        res = _get("/v5/market/kline", dict(category="linear", symbol=sym, interval="60",
                                             start=start_ms, end=end, limit=1000))
        lst = res.get("list", [])
        if not lst:
            break
        rows += lst
        oldest = int(lst[-1][0])
        if oldest <= start_ms or len(lst) < 1000:
            break
        end = oldest - 1
        time.sleep(0.1)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    df = df.astype(float).drop_duplicates("ts")
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    return df.set_index("ts").sort_index()[["open", "high", "low", "close", "volume"]]


def fetch_oi(sym, start_ms, end_ms):
    rows, end = [], end_ms
    while True:
        res = _get("/v5/market/open-interest", dict(category="linear", symbol=sym, intervalTime="1h",
                                                     startTime=start_ms, endTime=end, limit=200))
        lst = res.get("list", [])
        if not lst:
            break
        rows += lst
        oldest = min(int(x["timestamp"]) for x in lst)
        if oldest <= start_ms or len(lst) < 200:
            break
        end = oldest - 1
        time.sleep(0.1)
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True)
    df["oi"] = df["openInterest"].astype(float)
    return df.drop_duplicates("ts").set_index("ts").sort_index()[["oi"]]


def load_symbol(sym):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{sym}_1h.csv")
    if os.path.exists(path):
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df
    s = int(pd.Timestamp(START, tz="UTC").timestamp() * 1000)
    e = int(pd.Timestamp(DEV_END, tz="UTC").timestamp() * 1000)
    print(f"downloading {sym} ...")
    k = fetch_klines(sym, s, e)
    o = fetch_oi(sym, s, e)
    df = k.join(o, how="inner")
    df.to_csv(path)
    return df


# ----------------------------------------------------------------- التحليل
def add_features(df):
    df = df.copy()
    df["r4"] = df["close"] / df["close"].shift(LOOKBACK) - 1
    df["oi4"] = df["oi"] / df["oi"].shift(LOOKBACK) - 1
    for c in ["r4", "oi4"]:
        m = df[c].rolling(Z_WIN, min_periods=Z_WIN // 2).mean().shift(1)   # الماضي فقط
        s = df[c].rolling(Z_WIN, min_periods=Z_WIN // 2).std().shift(1)
        df["z_" + c] = (df[c] - m) / s
    entry = df["open"].shift(-1)                                         # افتتاح الشمعة التالية
    for h in HORIZONS:
        df[f"f{h}"] = df["close"].shift(-h) / entry - 1 - COST
    return df


def find_events(df, thresh):
    cand = df.index[(df["z_r4"] <= thresh) & (df["z_oi4"] <= thresh)]
    out, last = [], None
    for t in cand:
        if last is None or (t - last) >= pd.Timedelta(hours=COOLDOWN):
            out.append(t)
            last = t
    return out


def build_episodes(ev):
    """ev: DataFrame [ts, sym, f4..]  -> episode id per row (أحداث متقاربة بين العملات = حلقة واحدة)"""
    ev = ev.sort_values("ts").reset_index(drop=True)
    ep, cur, start = [], -1, None
    for t in ev["ts"]:
        if start is None or (t - start) > pd.Timedelta(hours=EPISODE_GAP):
            cur += 1
            start = t
        ep.append(cur)
    ev["episode"] = ep
    return ev


def analyse(data, thresh, verbose=True):
    rng = np.random.default_rng(SEED)
    rows = []
    for sym, df in data.items():
        for t in find_events(df, thresh):
            r = df.loc[t]
            rows.append(dict(ts=t, sym=sym, z_r4=r["z_r4"], z_oi4=r["z_oi4"],
                             **{f"f{h}": r[f"f{h}"] for h in HORIZONS}))
    if not rows:
        print(f"[z<={thresh}] no events")
        return None
    ev = build_episodes(pd.DataFrame(rows))

    # قوائم الساعات الصالحة لكل عملة (للعشوائي)
    valid = {s: d.dropna(subset=["z_r4", "z_oi4"] + [f"f{h}" for h in HORIZONS]) for s, d in data.items()}

    results = []
    for h in HORIZONS:
        col = f"f{h}"
        e = ev.dropna(subset=[col])
        epi = e.groupby("episode")[col].mean()
        obs_mean, obs_epi = e[col].mean(), epi.mean()

        # عشوائي مطابق: نفس العملات ونفس العدد، أوقات عشوائية
        syms = e["sym"].values
        pools = {s: valid[s][col].values for s in set(syms)}
        total = np.zeros(N_RAND)
        for s, cnt in pd.Series(syms).value_counts().items():
            draws = pools[s][rng.integers(len(pools[s]), size=(N_RAND, cnt))]
            total += draws.sum(axis=1)
        rand_means = total / len(syms)
        p_coin = (np.sum(rand_means >= obs_mean) + 1) / (N_RAND + 1)

        # مستوى الحلقة: لكل حلقة وقت عشوائي واحد يطبق على نفس عملاتها
        ulist = sorted(set(syms))
        M = pd.concat({s: valid[s][col] for s in ulist}, axis=1).dropna().values   # ساعات مشتركة
        ep_syms = e.groupby("episode")["sym"].apply(set)
        mask = np.array([[s in ss for s in ulist] for ss in ep_syms], dtype=float)  # (حلقات × عملات)
        cnt = mask.sum(axis=1)
        rand_epi = np.empty(N_RAND)
        for i in range(N_RAND):
            rows_ = M[rng.integers(len(M), size=len(mask))]
            rand_epi[i] = ((rows_ * mask).sum(axis=1) / cnt).mean()
        p_evt = (np.sum(rand_epi >= obs_epi) + 1) / (N_RAND + 1)

        # بعد حذف أكبر 3 حلقات
        epi_m3 = epi.drop(epi.nlargest(3).index)
        p_m3 = (np.sum(rand_epi >= epi_m3.mean()) + 1) / (N_RAND + 1)

        # اختبار الإشارة مقابل نسبة الربح الفعلية في العشوائي (مو 50/50)
        base_win = np.mean(np.concatenate([pools[s] for s in pools]) > 0)
        from math import comb
        k, n = int((epi > 0).sum()), len(epi)
        p_sgnR = sum(comb(n, j) * base_win**j * (1 - base_win)**(n - j) for j in range(k, n + 1))

        results.append(dict(h=h, events=len(e), episodes=n,
                            mean=obs_mean, rand_mean=rand_means.mean(),
                            epi_mean=obs_epi, rand_epi=rand_epi.mean(),
                            win=(epi > 0).mean(), base_win=base_win,
                            p_coin=p_coin, p_evt=p_evt, p_minus3=p_m3, p_sgnR=p_sgnR))

    res = pd.DataFrame(results)
    if verbose:
        pd.set_option("display.width", 200)
        print(f"\n=== z <= {thresh} | events={len(ev)} | episodes={ev['episode'].nunique()} | cost {COST:.2%} included ===")
        show = res.copy()
        for c in ["mean", "rand_mean", "epi_mean", "rand_epi", "win", "base_win"]:
            show[c] = (show[c] * 100).round(2)
        for c in ["p_coin", "p_evt", "p_minus3", "p_sgnR"]:
            show[c] = show[c].round(3)
        print(show.to_string(index=False))
    return ev, res


def by_year(ev, h=24):
    col = f"f{h}"
    e = ev.dropna(subset=[col]).copy()
    e["year"] = e["ts"].dt.year
    t = e.groupby("year").agg(events=(col, "size"), episodes=("episode", "nunique"),
                               mean_pct=(col, lambda x: round(x.mean() * 100, 2)),
                               win_pct=(col, lambda x: round((x > 0).mean() * 100, 1)))
    print(f"\n--- by year @ {h}h ---")
    print(t.to_string())


def verdict(res):
    r = res.set_index("h")
    passed = [h for h in HORIZONS
              if r.loc[h, "epi_mean"] > 0 and r.loc[h, "p_evt"] < 0.05
              and r.loc[h, "p_minus3"] < 0.10 and r.loc[h, "episodes"] >= 30]
    print("\n=== الحكم ===")
    if passed:
        print(f"✅ في أثر يستحق المتابعة على الآفاق: {passed} ساعة "
              "(موجب بعد الرسوم، يتفوق على العشوائي على مستوى الحلقات، ويصمد بعد حذف أكبر 3)")
        print("   الخطوة الجاية: المنهجية الكاملة + فترة الحكم المقفولة.")
    else:
        print("❌ ما في أثر يتجاوز العشوائي بعد الرسوم بشكل مستقل — الفكرة تنقل للمقبرة.")


def main():
    data = {}
    for s in SYMBOLS:
        df = load_symbol(s)
        df = df[df.index <= pd.Timestamp(DEV_END, tz="UTC")]   # حماية فترة الحكم
        data[s] = add_features(df)
        print(f"{s}: {len(df)} hours  {df.index.min().date()} → {df.index.max().date()}")

    out = analyse(data, Z_THRESH)
    if out is None:
        sys.exit()
    ev, res = out
    by_year(ev, 24)
    ev.to_csv("liq_events.csv", index=False)

    print("\n(فحص حساسية فقط — لا تختار الحد الأفضل منها)")
    for z in Z_SENS:
        analyse(data, z)

    verdict(res)


if __name__ == "__main__":
    main()
