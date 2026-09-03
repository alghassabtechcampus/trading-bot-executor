"""Per-combo re-analysis of the stop-width study.

The first pass (stop_width_report.py) merged all three combos before applying
the decision bar: one pooled OOS average R, and walk-forward windows averaged
across combos. That answers "what is the best single stop for the system as a
whole", which is NOT the question -- the three combos trade different
timeframes (15m / 1h / 4h) with different ATR scales, and nothing forces them
to share a stop multiplier. alert_watcher already runs them independently.

This script runs the SAME pre-committed bar three times, once per combo, on
that combo's own OOS segment and its own 4 walk-forward windows:

    a wider stop replaces 1.0xATR for a combo only if, FOR THAT COMBO,
      (a) OOS average R per signal is higher, AND
      (b) it is higher in >= 3 of that combo's 4 walk-forward windows.

Two further corrections to the pooled version:
  - the pooled walk-forward gave each combo equal weight per window, so a
    slow window of ~50 signals counted as much as a fast one of ~230;
  - the per-combo tables it printed were whole-history, not OOS, so they
    could not be compared against the bar even informally.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

RESULTS = Path(__file__).resolve().parent / "stop_width_results.parquet"
REACHABILITY_LIMIT_PCT = 3.0
N_WINDOWS = 4
OOS_FRACTION = 0.30
BASE_MULT = 1.0
COMBO_LABEL = {"fast": "fast (سريعة)  trend 1h / entry 15m / levels 1d",
               "medium": "medium (متوسطة) trend 4h / entry 1h / levels 1d",
               "slow": "slow (بطيئة)  trend 1d / entry 4h / levels 1w"}


def metrics(g: pd.DataFrame) -> dict:
    closed = g[g["outcome"].isin(["hit_target", "hit_stop"])]
    filled = g[g["outcome"] != "never_entered"]
    scored = g[g["R"].notna()]
    wins = (closed["outcome"] == "hit_target").sum()
    return {
        "signals": len(g),
        "fill_rate": len(filled) / len(g) * 100 if len(g) else np.nan,
        "target": int((g["outcome"] == "hit_target").sum()),
        "stop": int((g["outcome"] == "hit_stop").sum()),
        "never_ent": int((g["outcome"] == "never_entered").sum()),
        "win_rate": wins / len(closed) * 100 if len(closed) else np.nan,
        "avg_R": scored["R"].mean() if len(scored) else np.nan,
        "avg_net_pct": scored["net_pct"].mean() if len(scored) else np.nan,
        "med_risk_pct": g["risk_pct"].median(),
    }


def show(df: pd.DataFrame, title: str) -> pd.DataFrame:
    out = pd.DataFrame([{"stop_mult": k, **metrics(g)} for k, g in df.groupby("stop_mult")]).set_index("stop_mult")
    print(f"\n  {title}")
    print("  " + "-" * len(title))
    print("  " + out.round(3).to_string().replace("\n", "\n  "))
    return out


def main() -> None:
    df = pd.read_parquet(RESULTS)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    mults = sorted(df["stop_mult"].unique())

    # same reachability filter as before: decide for the system as it now is
    base = df[df["stop_mult"] == BASE_MULT]
    far = base["entry_distance_pct"] > REACHABILITY_LIMIT_PCT
    keep = set(zip(base.loc[~far, "symbol"], base.loc[~far, "combo"], base.loc[~far, "bar"]))
    df = df[[k in keep for k in zip(df["symbol"], df["combo"], df["bar"])]].copy()

    print("=" * 92)
    print("PER-COMBO STOP-WIDTH DECISION  (each combo judged alone, on its own OOS + windows)")
    print("=" * 92)
    print(f"reachable signals only (entry zone <= {REACHABILITY_LIMIT_PCT}% below price); "
          f"cost 0.20% round trip; avg R is per SIGNAL (unfilled = 0R)")

    verdicts = {}
    for combo in ["fast", "medium", "slow"]:
        g = df[df["combo"] == combo]
        n_sig = len(g) // len(mults)
        print("\n" + "=" * 92)
        print(f"{COMBO_LABEL[combo]}")
        print("=" * 92)
        print(f"  {n_sig} reachable signals  |  "
              f"{g['time'].min().date()} -> {g['time'].max().date()}  "
              f"({(g['time'].max() - g['time'].min()).days} days)")

        show(g, "WHOLE HISTORY (context only -- not part of the bar)")

        times = np.sort(g["time"].unique())
        cut = times[int(len(times) * (1 - OOS_FRACTION))]
        dev, oos = g[g["time"] < cut], g[g["time"] >= cut]
        print(f"\n  chronological split at {pd.Timestamp(cut).date()}  "
              f"-> dev {len(dev)//len(mults)} signals / OOS {len(oos)//len(mults)} signals")
        show(dev, "DEVELOPMENT")
        oos_t = show(oos, "OUT-OF-SAMPLE  [criterion (a)]")

        # this combo's own 4 sequential windows, inside its own OOS
        ot = np.sort(oos["time"].unique())
        edges = [ot[int(len(ot) * i / N_WINDOWS)] for i in range(N_WINDOWS)]
        edges.append(ot[-1] + np.timedelta64(1, "s"))
        rows = []
        for w in range(N_WINDOWS):
            seg = oos[(oos["time"] >= edges[w]) & (oos["time"] < edges[w + 1])]
            for m in mults:
                sm = seg[seg["stop_mult"] == m]
                rows.append({"window": w + 1, "stop_mult": m, "signals": len(sm), "avg_R": sm["R"].mean()})
        wf = pd.DataFrame(rows)
        piv = wf.pivot_table(index="window", columns="stop_mult", values="avg_R")
        cnt = wf.pivot_table(index="window", columns="stop_mult", values="signals")[BASE_MULT].astype(int)
        print(f"\n  WALK-FORWARD  ({N_WINDOWS} windows inside this combo's OOS)  [criterion (b)]")
        print("  " + "-" * 60)
        wfp = piv.copy()
        wfp.insert(0, "signals", cnt)
        print("  " + wfp.round(3).to_string().replace("\n", "\n  "))

        base_oos = oos_t.loc[BASE_MULT, "avg_R"]
        print(f"\n  DECISION for {combo}:  baseline 1.0xATR OOS avg R = {base_oos:+.4f}")
        combo_verdict = {}
        for m in mults:
            if m == BASE_MULT:
                continue
            a = oos_t.loc[m, "avg_R"] > base_oos
            wins = int((piv[m] > piv[BASE_MULT]).sum())
            b = wins >= 3
            combo_verdict[m] = a and b
            print(f"    {m}xATR: (a) OOS R {oos_t.loc[m,'avg_R']:+.4f} vs {base_oos:+.4f} -> {'PASS' if a else 'FAIL'}"
                  f"   (b) better in {wins}/{N_WINDOWS} windows -> {'PASS' if b else 'FAIL'}"
                  f"   => {'ADOPT' if combo_verdict[m] else 'REJECT'}")
        adopted = [m for m, ok in combo_verdict.items() if ok]
        verdicts[combo] = max(adopted, key=lambda m: oos_t.loc[m, "avg_R"]) if adopted else BASE_MULT
        print(f"    -> {combo}: stop_atr_mult = {verdicts[combo]}"
              f"{'  (CHANGED)' if verdicts[combo] != BASE_MULT else '  (unchanged)'}")

    print("\n" + "=" * 92)
    print("SUMMARY -- three independent decisions")
    print("=" * 92)
    for combo, m in verdicts.items():
        print(f"  {combo:<7} -> {m}xATR" + ("   CHANGED" if m != BASE_MULT else ""))
    if len(set(verdicts.values())) == 1:
        print(f"\n  All three agree on {list(verdicts.values())[0]}xATR independently.")
    else:
        print("\n  The combos do NOT agree -- a single shared multiplier is the wrong shape.")


if __name__ == "__main__":
    main()
