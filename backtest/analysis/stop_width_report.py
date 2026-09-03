"""Reads stop_width_results.parquet and applies the PRE-COMMITTED decision
bar from stop_width_study.py's docstring. Reports pooled, dev/OOS, and
per-window numbers, plus how many signals the 3% reachability filter removes.

Average R is taken PER SIGNAL, with never-filled signals counted as 0R. That
matters: a wider stop is further from the entry, so it can only ever change
what happens AFTER a fill -- comparing only filled trades would let a variant
look better simply by surviving longer, while a per-signal average keeps every
variant answerable for the same opportunity set.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

RESULTS = Path(__file__).resolve().parent / "stop_width_results.parquet"
REACHABILITY_LIMIT_PCT = 3.0
N_WINDOWS = 4
OOS_FRACTION = 0.30


def load() -> pd.DataFrame:
    df = pd.read_parquet(RESULTS)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def metrics(g: pd.DataFrame) -> dict:
    closed = g[g["outcome"].isin(["hit_target", "hit_stop"])]
    filled = g[g["outcome"] != "never_entered"]
    scored = g[g["R"].notna()]
    wins = (closed["outcome"] == "hit_target").sum()
    return {
        "signals": len(g),
        "filled": len(filled),
        "fill_rate": len(filled) / len(g) * 100 if len(g) else np.nan,
        "target": int((g["outcome"] == "hit_target").sum()),
        "stop": int((g["outcome"] == "hit_stop").sum()),
        "time_exit": int((g["outcome"] == "time_exit").sum()),
        "never_entered": int((g["outcome"] == "never_entered").sum()),
        "win_rate": wins / len(closed) * 100 if len(closed) else np.nan,
        "avg_R": scored["R"].mean() if len(scored) else np.nan,
        "avg_net_pct": scored["net_pct"].mean() if len(scored) else np.nan,
        "median_risk_pct": g["risk_pct"].median(),
    }


def table(df: pd.DataFrame, title: str, by: str = "stop_mult") -> pd.DataFrame:
    out = pd.DataFrame([{by: k, **metrics(g)} for k, g in df.groupby(by)]).set_index(by)
    print(f"\n{title}")
    print("-" * len(title))
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        print(out.round(3).to_string())
    return out


def main() -> None:
    df = load()
    mults = sorted(df["stop_mult"].unique())

    print("=" * 78)
    print("SIGNAL SET")
    print("=" * 78)
    base = df[df["stop_mult"] == mults[0]]
    print(f"total signals replayed : {len(base)}")
    print(f"symbols                : {base['symbol'].nunique()}")
    for combo, g in base.groupby("combo"):
        span = (g['time'].max() - g['time'].min()).days
        print(f"  {combo:<7} {len(g):>5} signals   {g['time'].min().date()} -> {g['time'].max().date()}  ({span} days)")
    print(f"\noutcome vocabulary: hit_target / hit_stop / time_exit / never_entered")
    print(f"invalid or still-open rows (excluded from R): "
          f"{int(df['R'].isna().sum())} of {len(df)}")

    # ---------------------------------------------------------- reachability
    print("\n" + "=" * 78)
    print(f"REACHABILITY FILTER  (entry zone more than {REACHABILITY_LIMIT_PCT}% below price)")
    print("=" * 78)
    far = base["entry_distance_pct"] > REACHABILITY_LIMIT_PCT
    print(f"signals that would now be classified 'unreachable': {int(far.sum())} "
          f"of {len(base)}  ({far.mean()*100:.1f}%)")
    for combo, g in base.groupby("combo"):
        f = g["entry_distance_pct"] > REACHABILITY_LIMIT_PCT
        print(f"  {combo:<7} {int(f.sum()):>5} of {len(g):>5}  ({f.mean()*100:5.1f}%)   "
              f"median distance {g['entry_distance_pct'].median():6.2f}%   "
              f"p95 {g['entry_distance_pct'].quantile(0.95):6.2f}%")
    filtered_out = base[far]
    if len(filtered_out):
        fr = filtered_out["outcome"].value_counts(normalize=True) * 100
        print(f"\n  what those far signals actually did (at 1.0xATR):")
        for k in ("never_entered", "hit_stop", "hit_target", "time_exit"):
            print(f"    {k:<14} {fr.get(k, 0.0):5.1f}%")
        print(f"    avg R of the filtered-out set: {filtered_out['R'].mean():+.3f}")
        print(f"    avg R of the kept set        : {base[~far]['R'].mean():+.3f}")

    # keep only reachable signals for the stop comparison -- that is the system
    # the decision is actually being made for
    reachable_keys = set(zip(base.loc[~far, "symbol"], base.loc[~far, "combo"], base.loc[~far, "bar"]))
    df = df[[k in reachable_keys for k in zip(df["symbol"], df["combo"], df["bar"])]].copy()

    print("\n" + "=" * 78)
    print("STOP WIDTH COMPARISON  (reachable signals only, cost 0.20% round trip)")
    print("=" * 78)
    table(df, "POOLED - all combos, whole history  [NOT decisive on its own]")

    for combo in sorted(df["combo"].unique()):
        table(df[df["combo"] == combo], f"POOLED - combo: {combo}")

    # ------------------------------------------------------------ dev / OOS
    print("\n" + "=" * 78)
    print("DEVELOPMENT / OUT-OF-SAMPLE  (chronological split per combo)")
    print("=" * 78)
    dev_parts, oos_parts = [], []
    for combo, g in df.groupby("combo"):
        times = np.sort(g["time"].unique())
        cut = times[int(len(times) * (1 - OOS_FRACTION))]
        dev_parts.append(g[g["time"] < cut])
        oos_parts.append(g[g["time"] >= cut])
        print(f"  {combo:<7} split at {pd.Timestamp(cut).date()}  "
              f"dev {len(g[g['time'] < cut])//len(mults)} sig / oos {len(g[g['time'] >= cut])//len(mults)} sig")
    dev, oos = pd.concat(dev_parts), pd.concat(oos_parts)
    dev_t = table(dev, "DEVELOPMENT segment")
    oos_t = table(oos, "OUT-OF-SAMPLE segment  [criterion (a)]")

    # --------------------------------------------------------- walk-forward
    print("\n" + "=" * 78)
    print(f"WALK-FORWARD  ({N_WINDOWS} sequential windows, OOS segment)  [criterion (b)]")
    print("=" * 78)
    win_rows = []
    for combo, g in oos.groupby("combo"):
        times = np.sort(g["time"].unique())
        edges = [times[int(len(times) * i / N_WINDOWS)] for i in range(N_WINDOWS)] + [times[-1] + np.timedelta64(1, "s")]
        for w in range(N_WINDOWS):
            seg = g[(g["time"] >= edges[w]) & (g["time"] < edges[w + 1])]
            for m in mults:
                sm = seg[seg["stop_mult"] == m]
                win_rows.append({"combo": combo, "window": w + 1, "stop_mult": m,
                                 "signals": len(sm), "avg_R": sm["R"].mean()})
    wf = pd.DataFrame(win_rows)
    pivot = wf.pivot_table(index=["combo", "window"], columns="stop_mult", values="avg_R")
    counts = wf.pivot_table(index=["combo", "window"], columns="stop_mult", values="signals")
    print("\navg R per window (columns = stop multiplier):")
    print(pivot.round(3).to_string())
    print("\nsignals per window:")
    print(counts.astype(int).to_string())

    # pooled walk-forward across combos, window by window
    allw = wf.groupby(["window", "stop_mult"])["avg_R"].mean().unstack()
    print("\navg R by window, all combos pooled:")
    print(allw.round(3).to_string())

    # ----------------------------------------------------------- the verdict
    print("\n" + "=" * 78)
    print("PRE-COMMITTED DECISION BAR")
    print("=" * 78)
    base_mult = 1.0
    base_oos = oos_t.loc[base_mult, "avg_R"]
    print(f"baseline 1.0xATR OOS avg R = {base_oos:+.4f}")
    verdicts = {}
    for m in mults:
        if m == base_mult:
            continue
        a = oos_t.loc[m, "avg_R"] > base_oos
        wins = int((allw[m] > allw[base_mult]).sum())
        b = wins >= 3
        verdicts[m] = a and b
        print(f"\n  {m}xATR:")
        print(f"    (a) OOS avg R {oos_t.loc[m,'avg_R']:+.4f} > {base_oos:+.4f} ....... {'PASS' if a else 'FAIL'}")
        print(f"    (b) better in {wins}/{N_WINDOWS} walk-forward windows (need >=3) ... {'PASS' if b else 'FAIL'}")
        print(f"    => {'ADOPT' if verdicts[m] else 'REJECT'}")

    adopted = [m for m, ok in verdicts.items() if ok]
    print("\n" + "-" * 78)
    if adopted:
        best = max(adopted, key=lambda m: oos_t.loc[m, "avg_R"])
        print(f"VERDICT: change stop_atr_mult 1.0 -> {best}")
    else:
        print("VERDICT: KEEP stop_atr_mult = 1.0  (no wider stop cleared the pre-committed bar)")
    print("-" * 78)


if __name__ == "__main__":
    main()
