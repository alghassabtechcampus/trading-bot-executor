"""
اختبار الحكم: نفس قواعد "التصفيات" بالضبط — على 9 عملات جديدة ما دخلت في الاختبار الأول
=========================================================================================
- ما في أي تعديل: نفس الحد (-3)، نفس الرسوم، نفس الآفاق، نفس الفترة.
- يحتاج الملف liquidation_flush_test.py يكون في نفس المجلد.
- شغّله مرة وحدة بس، والنتيجة نهائية — لا تعدّل وتعيد.

التشغيل:
  python liquidation_new_coins_test.py
"""

import pandas as pd

import liquidation_flush_test as base

NEW_SYMBOLS = ["LINKUSDT", "DOTUSDT", "TRXUSDT", "ATOMUSDT", "NEARUSDT",
               "BCHUSDT", "ETCUSDT", "FILUSDT", "UNIUSDT"]


def main():
    data = {}
    for s in NEW_SYMBOLS:
        try:
            df = base.load_symbol(s)
        except Exception as e:  # noqa
            print(f"⚠️ {s} skipped (download failed: {e}) — شغّل مرة ثانية لإكماله")
            continue
        df = df[df.index <= pd.Timestamp(base.DEV_END, tz="UTC")]
        if len(df) < base.Z_WIN * 2:
            print(f"⚠️ {s} skipped (بيانات قليلة: {len(df)} ساعة)")
            continue
        data[s] = base.add_features(df)
        print(f"{s}: {len(df)} hours  {df.index.min().date()} → {df.index.max().date()}")

    if len(data) < 6:
        print("\n❗ أقل من 6 عملات تحمّلت — أعد التشغيل لإكمال التحميل قبل ما تعتمد النتيجة.")
        return

    out = base.analyse(data, base.Z_THRESH)
    if out is None:
        return
    ev, res = out
    base.by_year(ev, 24)
    ev.to_csv("liq_events_new_coins.csv", index=False)

    print("\n=== المقارنة مع الاختبار الأول (24 ساعة) ===")
    r = res.set_index("h").loc[24]
    print(f"الأول (العملات القديمة): متوسط الحلقة +1.61%  | ربح 56.3% | عشوائي -0.12%")
    print(f"الجديد (عملات جديدة):    متوسط الحلقة {r['epi_mean']*100:+.2f}% | ربح {r['win']*100:.1f}% "
          f"| عشوائي {r['rand_epi']*100:+.2f}%")

    base.verdict(res)


if __name__ == "__main__":
    main()
