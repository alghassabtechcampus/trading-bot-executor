"""
اختبار الضغط: فكرة "التصفيات" على كل العملات (18 عملة)
========================================================
نفس قواعد الدخول بالضبط (z <= -3). يفحص:
  1) رسوم أعلى: 0.2% / 0.4% / 0.6%
  2) أسوأ 10 حلقات
  3) وقف خسارة: بدون / 3% / 5% / 8%
  4) أكبر تراجع في الرصيد (لو دخلت كل حلقة بنفس الحجم)
  5) وصفي فقط: النتيجة لما البيتكوين فوق/تحت متوسط 200 يوم
     (للفهم بس — مو فلتر نختاره من هنا)

يحتاج: liquidation_flush_test.py في نفس المجلد + بيانات data_liq (محملة من قبل)
التشغيل:
  python liquidation_stress_test.py
"""

import numpy as np
import pandas as pd

import liquidation_flush_test as base

ALL = base.SYMBOLS + ["LINKUSDT", "DOTUSDT", "TRXUSDT", "ATOMUSDT", "NEARUSDT",
                      "BCHUSDT", "ETCUSDT", "FILUSDT", "UNIUSDT"]
H = 24                       # الأفق الرئيسي
STOPS = [None, 0.03, 0.05, 0.08]
STOP_SLIP = 0.003            # انزلاق إضافي عند ضرب الوقف (السوق سريع وقت الانهيار)
COSTS = [0.002, 0.004, 0.006]


def load_all():
    data = {}
    for s in ALL:
        try:
            df = base.load_symbol(s)
        except Exception as e:  # noqa
            print(f"⚠️ {s} skipped: {e}")
            continue
        df = df[df.index <= pd.Timestamp(base.DEV_END, tz="UTC")]
        data[s] = base.add_features(df)
    print(f"loaded {len(data)} coins")
    return data


def trade_return(df, t, h, stop):
    """عائد صفقة واحدة: دخول افتتاح الشمعة التالية، خروج بعد h ساعة أو عند الوقف. بدون رسوم."""
    i = df.index.get_loc(t)
    if i + h >= len(df):
        return np.nan, False
    entry = df["open"].iloc[i + 1]
    path = df.iloc[i + 1: i + h + 1]
    if stop is not None:
        lvl = entry * (1 - stop)
        hit = path["low"].values <= lvl
        if hit.any():
            j = int(np.argmax(hit))
            px = min(lvl, path["open"].values[j])          # لو فتحت الشمعة تحت الوقف = تنفيذ أسوأ
            return px * (1 - STOP_SLIP) / entry - 1, True
    return path["close"].values[-1] / entry - 1, False


def main():
    data = load_all()
    rows = []
    for s, df in data.items():
        for t in base.find_events(df, base.Z_THRESH):
            rec = dict(ts=t, sym=s)
            for st in STOPS:
                r, hit = trade_return(df, t, H, st)
                key = "none" if st is None else f"{int(st*100)}"
                rec[f"r_{key}"] = r
                rec[f"hit_{key}"] = hit
            rows.append(rec)
    ev = base.build_episodes(pd.DataFrame(rows).dropna(subset=["r_none"]))
    epi = ev.groupby("episode").agg(ts=("ts", "first"), coins=("sym", lambda x: ",".join(c.replace("USDT", "") for c in x)),
                                    **{c: (c, "mean") for c in ev.columns if c.startswith("r_")})
    print(f"\nevents={len(ev)}  episodes={len(epi)}  horizon={H}h")

    # 1) الرسوم
    print("\n=== 1) الرسوم (بدون وقف) ===")
    for c in COSTS:
        x = epi["r_none"] - c
        print(f"رسوم {c:.1%}: متوسط الحلقة {x.mean()*100:+.2f}% | ربح {(x>0).mean()*100:.1f}%")

    # 2) أسوأ 10
    print("\n=== 2) أسوأ 10 حلقات (بدون وقف، بعد 0.2%) ===")
    w = epi.assign(ret=(epi["r_none"] - 0.002) * 100).nsmallest(10, "ret")
    for _, r in w.iterrows():
        print(f"{r['ts']:%Y-%m-%d %H:%M}  {r['ret']:+6.2f}%   {r['coins']}")
    print("السنين:", w["ts"].dt.year.value_counts().sort_index().to_dict())

    # 3) وقف الخسارة
    print("\n=== 3) وقف الخسارة (رسوم 0.2% + انزلاق 0.3% عند الوقف) ===")
    for st in STOPS:
        key = "none" if st is None else f"{int(st*100)}"
        x = epi[f"r_{key}"] - 0.002
        hits = ev[f"hit_{key}"].mean() * 100
        y22 = x[epi["ts"].dt.year == 2022].mean() * 100
        name = "بدون وقف" if st is None else f"وقف {st:.0%}"
        print(f"{name:9}: متوسط {x.mean()*100:+.2f}% | ربح {(x>0).mean()*100:.1f}% | أسوأ {x.min()*100:+.2f}% "
              f"| ضرب الوقف {hits:4.1f}% | 2022: {y22:+.2f}%")

    # 4) التراجع
    print("\n=== 4) أكبر تراجع (مجموع عوائد الحلقات بالتسلسل، بدون وقف، 0.2%) ===")
    eq = (epi.sort_values("ts")["r_none"] - 0.002).cumsum() * 100
    dd = (eq - eq.cummax())
    print(f"مجموع كل الحلقات: {eq.iloc[-1]:+.1f}%  | أكبر تراجع: {dd.min():.1f}% "
          f"(وصل له في {epi.sort_values('ts')['ts'].iloc[int(np.argmin(dd.values))]:%Y-%m-%d})")

    # 5) وصفي: حالة البيتكوين
    print("\n=== 5) وصفي فقط: البيتكوين فوق/تحت متوسط 200 يوم وقت الحدث ===")
    btc = data["BTCUSDT"]["close"]
    sma = btc.rolling(24 * 200).mean()
    above = (btc > sma).reindex(epi["ts"], method="ffill").values
    x = epi["r_none"] - 0.002
    for flag, name in [(True, "فوق"), (False, "تحت")]:
        m = above == flag
        print(f"BTC {name} 200 يوم: {m.sum():3d} حلقة | متوسط {x[m].mean()*100:+.2f}% | ربح {(x[m]>0).mean()*100:.1f}%")
    print("(لا نعتمد هذا كفلتر الحين — لو بنستخدمه لازم يثبت في التداول التجريبي أولاً)")

    epi.to_csv("liq_stress_episodes.csv")
    print("\nsaved: liq_stress_episodes.csv")


if __name__ == "__main__":
    main()
