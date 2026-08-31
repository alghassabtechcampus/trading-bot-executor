"""Algorithmic support/resistance levels (no manual chart-drawing).

Over the last `lookback_bars` candles on the levels timeframe: every bar's
high is a resistance CANDIDATE, every bar's low is a support CANDIDATE.
Candidates within `touch_tolerance_pct` of each other are merged into one
cluster (a level's price = the running mean of its members, so the level
drifts slightly toward the center of mass as more touches are added,
rather than freezing at the first touch seen). A cluster counts as a real
level only once it has been touched at least `min_touches` times.

IMPORTANT: a cluster built from historical highs is only actually
"resistance" if it still sits ABOVE the current price -- if price has
since broken through it, it no longer overhangs the market and is
reported separately as `broken_resistance_levels` (a former high the
market has already cleared; classic TA treats these as candidate support
on a retest, but this module does not assert that itself). The mirror
applies to lows: a low cluster still below price is real support;
one price has fallen through since is `broken_support_levels`. Each level
also carries `last_touched` (the most recent bar's timestamp in that
cluster) so a big price gap between two levels is self-explanatory --
e.g. an old level from before a breakout will visibly be old.

From the resulting (position-filtered) levels: nearest resistance = the
lowest resistance level still above price; nearest support = the highest
support level still below it.
"""

from __future__ import annotations

import pandas as pd


def _cluster(points: list[tuple[float, pd.Timestamp]], tolerance_pct: float) -> list[dict]:
    """Greedy single-pass clustering on value, sorted ascending: start a new
    cluster whenever the next value is more than `tolerance_pct` from the
    current cluster's running mean. Tracks touch count and the most recent
    timestamp among a cluster's members."""
    if not points:
        return []
    ordered = sorted(points, key=lambda p: p[0])
    clusters: list[dict] = [{"sum": ordered[0][0], "count": 1, "last_touched": ordered[0][1]}]
    for value, ts in ordered[1:]:
        current = clusters[-1]
        mean = current["sum"] / current["count"]
        if abs(value - mean) / mean * 100 <= tolerance_pct:
            current["sum"] += value
            current["count"] += 1
            current["last_touched"] = max(current["last_touched"], ts)
        else:
            clusters.append({"sum": value, "count": 1, "last_touched": ts})
    return [{"level": c["sum"] / c["count"], "touches": c["count"], "last_touched": c["last_touched"]}
            for c in clusters]


def _serialize(cluster: dict) -> dict:
    return {"level": round(cluster["level"], 8), "touches": cluster["touches"],
            "last_touched": cluster["last_touched"].isoformat()}


def compute(df: pd.DataFrame, current_price: float, lookback_bars: int = 20,
            touch_tolerance_pct: float = 0.5, min_touches: int = 2) -> dict:
    if len(df) < lookback_bars:
        return {"nearest_resistance": None, "nearest_support": None, "details": {"reason": "insufficient history"}}

    window = df.tail(lookback_bars)
    high_points = list(zip(window["high"].tolist(), window["time"].tolist()))
    low_points = list(zip(window["low"].tolist(), window["time"].tolist()))

    resistance_clusters = [c for c in _cluster(high_points, touch_tolerance_pct) if c["touches"] >= min_touches]
    support_clusters = [c for c in _cluster(low_points, touch_tolerance_pct) if c["touches"] >= min_touches]

    resistance_above = sorted((c for c in resistance_clusters if c["level"] > current_price),
                               key=lambda c: c["level"])
    broken_resistance = sorted((c for c in resistance_clusters if c["level"] <= current_price),
                                key=lambda c: c["level"], reverse=True)
    support_below = sorted((c for c in support_clusters if c["level"] < current_price),
                            key=lambda c: c["level"], reverse=True)
    broken_support = sorted((c for c in support_clusters if c["level"] >= current_price),
                             key=lambda c: c["level"])

    nearest_resistance = resistance_above[0] if resistance_above else None
    nearest_support = support_below[0] if support_below else None

    return {
        "nearest_resistance": round(nearest_resistance["level"], 8) if nearest_resistance else None,
        "nearest_resistance_touches": nearest_resistance["touches"] if nearest_resistance else None,
        "nearest_support": round(nearest_support["level"], 8) if nearest_support else None,
        "nearest_support_touches": nearest_support["touches"] if nearest_support else None,
        "details": {
            "lookback_bars": lookback_bars,
            "resistance_levels": [_serialize(c) for c in resistance_above],
            "support_levels": [_serialize(c) for c in support_below],
            "broken_resistance_levels": [_serialize(c) for c in broken_resistance],
            "broken_support_levels": [_serialize(c) for c in broken_support],
        },
    }


__all__ = ["compute"]
