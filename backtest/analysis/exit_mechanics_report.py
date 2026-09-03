"""Per-combo verdict on each exit mechanic, against the deployed `fixed` exit.

Never pools the combos -- that mistake was made once in the stop study and is
not repeated here. Each combo gets its own chronological dev/OOS split, its own
4 walk-forward windows, and its own pass/fail.

Also reports a fragility read on every variant that passes, because "3 of 4
windows" cannot distinguish a window won by 0.20R from one won by 0.002R.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

RESULTS = Path(__file__).resolve().parent / "exit_mechanics_results.parquet"
REACHABILITY_LIMIT_PCT = 3.0
N_WINDOWS = 4
OOS_FRACTION = 0.30
BASE = "fixed"
COMBO_LABEL = {"fast": "fast (سريعة)  1h trend / 15m entry",
               "medium": "medium (متوسطة)  4h trend / 1h entry",
               "slow": "slow (بطيئة)  1d trend / 4h entry"}
ORDER = ["fixed", "be_0.5atr", "be_1.0atr", "trail_1.0atr", "trail_1.5atr",
         "ratchet_50", "ratchet_70", "partial_50", "partial_70"]
IDEA = {"be_0.5atr": "1 trailing", "be_1.0atr": "1 trailing",
        "trail_1.0atr": "1 trailing", "trail_1.5atr": "1 trailing",
        "ratchet_50": "2 ratchet", "ratchet_70": "2 ratchet",
        "partial_50": "3 partial", "partial_70": "3 partial"}


def metrics(g: pd.DataFrame) -> dict:
    scored = g[g["R"].notna()]
    filled = g[g["outcome"] != "never_entered"]
    won = g["R"] > 0
    return {
        "signals": len(g),
        "avg_R": scored["R"].mean() if len(scored) else np.nan,
        "avg_net_pct": scored["net_pct"].mean() if len(scored) else np.nan,
        "win_rate_filled": (won & (g["outcome"] != "never_entered")).sum() / len(filled) * 100
                           if len(filled) else np.nan,
        "target": int((g["outcome"].isin(["hit_target", "partial_then_target"])).sum()),
        "full_stop": int((g["outcome"] == "hit_stop").sum()),
        "stop_be": int((g["outcome"] == "stopped_at_be").sum()),
        "stop_profit": int((g["outcome"] == "stopped_in_profit").sum()),
        "time_exit": int((g["outcome"] == "time_exit").sum()),
    }


def seg_table(df: pd.DataFrame, title: str) -> pd.DataFrame:
    out = pd.DataFrame([{"variant": v, **metrics(df[df["variant"] == v])} for v in ORDER]).set_index("variant")
    base_R, base_net = out.loc[BASE, "avg_R"], out.loc[BASE, "avg_net_pct"]
    out.insert(2, "dR_vs_fixed", out["avg_R"] - base_R)
    out.insert(4, "dNet_vs_fixed", out["avg_net_pct"] - base_net)
    print(f"\n  {title}")
    print("  " + "-" * len(title))
    print("  " + out.round(4).to_string().replace("\n", "\n  "))
    return out


def main() -> None:
    df = pd.read_parquet(RESULTS)
    df["time"] = pd.to_datetime(df["time"], utc=True)

    # same reachability filter the deployed system now applies
    b = df[df["variant"] == BASE]
    far = b["entry_distance_pct"] > REACHABILITY_LIMIT_PCT
    keep = set(zip(b.loc[~far, "symbol"], b.loc[~far, "combo"], b.loc[~far, "bar"]))
    df = df[[k in keep for k in zip(df["symbol"], df["combo"], df["bar"])]].copy()

    print("=" * 100)
    print("EXIT MECHANICS -- per-combo verdict vs the deployed fixed stop/target")
    print("=" * 100)
    print(f"reachable signals only (zone <= {REACHABILITY_LIMIT_PCT}% below price); cost 0.20% round trip;")
    print("avg R is per SIGNAL (unfilled = 0R); intrabar ties resolved against the trade")

    summary = []
    for combo in ["fast", "medium", "slow"]:
        g = df[df["combo"] == combo]
        n = len(g) // len(ORDER)
        print("\n" + "=" * 100)
        print(f"{COMBO_LABEL[combo]}   --   {n} reachable signals, "
              f"{g['time'].min().date()} -> {g['time'].max().date()}")
        print("=" * 100)

        times = np.sort(g["time"].unique())
        cut = times[int(len(times) * (1 - OOS_FRACTION))]
        dev, oos = g[g["time"] < cut], g[g["time"] >= cut]
        print(f"  split at {pd.Timestamp(cut).date()}  ->  dev {len(dev)//len(ORDER)} / OOS {len(oos)//len(ORDER)} signals")
        seg_table(dev, "DEVELOPMENT")
        oos_t = seg_table(oos, "OUT-OF-SAMPLE  [criterion (a)]")

        # this combo's own windows, inside its own OOS
        ot = np.sort(oos["time"].unique())
        edges = [ot[int(len(ot) * i / N_WINDOWS)] for i in range(N_WINDOWS)]
        edges.append(ot[-1] + np.timedelta64(1, "s"))
        rows = []
        for w in range(N_WINDOWS):
            seg = oos[(oos["time"] >= edges[w]) & (oos["time"] < edges[w + 1])]
            for v in ORDER:
                sv = seg[seg["variant"] == v]
                rows.append({"window": w + 1, "variant": v, "signals": len(sv),
                             "avg_R": sv["R"].mean()})
        wf = pd.DataFrame(rows).pivot_table(index="variant", columns="window", values="avg_R").loc[ORDER]
        print(f"\n  WALK-FORWARD avg R by window  [criterion (b)]")
        print("  " + "-" * 46)
        print("  " + wf.round(4).to_string().replace("\n", "\n  "))

        print(f"\n  VERDICT for {combo}  (baseline fixed: OOS avg R {oos_t.loc[BASE,'avg_R']:+.4f}, "
              f"net {oos_t.loc[BASE,'avg_net_pct']:+.3f}%)")
        print("  " + "-" * 96)
        print(f"  {'variant':<14}{'idea':<11}{'OOS dR':>9}{'OOS dNet%':>11}{'(a)':>6}"
              f"{'wins':>6}{'(b)':>6}   {'verdict':<9} margin of 3rd-best window")
        for v in ORDER:
            if v == BASE:
                continue
            a = oos_t.loc[v, "avg_R"] > oos_t.loc[BASE, "avg_R"]
            diffs = (wf.loc[v] - wf.loc[BASE]).sort_values(ascending=False)
            wins = int((diffs > 0).sum())
            bcrit = wins >= 3
            ok = a and bcrit
            third = diffs.iloc[2]      # margin of the 3rd-best window = how close (b) was
            print(f"  {v:<14}{IDEA[v]:<11}{oos_t.loc[v,'dR_vs_fixed']:>+9.4f}"
                  f"{oos_t.loc[v,'dNet_vs_fixed']:>+11.3f}{'PASS' if a else 'FAIL':>6}"
                  f"{wins:>6}{'PASS' if bcrit else 'FAIL':>6}   "
                  f"{'ADOPT' if ok else 'reject':<9} {third:+.4f}")
            summary.append({"combo": combo, "variant": v, "idea": IDEA[v],
                            "dR": oos_t.loc[v, "dR_vs_fixed"],
                            "dNet": oos_t.loc[v, "dNet_vs_fixed"],
                            "a": a, "wins": wins, "b": bcrit, "adopt": ok,
                            "third_margin": third})

    s = pd.DataFrame(summary)
    print("\n" + "=" * 100)
    print("SUMMARY -- which (idea, combo) pairs cleared the bar?")
    print("=" * 100)
    grid = s.pivot_table(index="variant", columns="combo", values="adopt", aggfunc="first").loc[
        [v for v in ORDER if v != BASE]][["fast", "medium", "slow"]]
    print(grid.replace({True: "ADOPT", False: "-"}).to_string())
    adopted = s[s["adopt"]]
    print(f"\n{len(adopted)} of {len(s)} (variant, combo) pairs cleared both criteria.")
    if len(adopted):
        print("\nFragility check on every pass (3rd-best window margin = how close (b) came to failing):")
        for _, r in adopted.iterrows():
            flag = "  <-- RAZOR THIN" if abs(r["third_margin"]) < 0.01 else ""
            print(f"  {r['combo']:<7} {r['variant']:<14} dR {r['dR']:+.4f}  dNet {r['dNet']:+.3f}%  "
                  f"wins {int(r['wins'])}/4  3rd margin {r['third_margin']:+.4f}{flag}")


if __name__ == "__main__":
    main()
