"""
تجميع شموع 5 دقائق لفريم ساعة واحدة (Open=أول، High=أعلى، Low=أدنى،
Close=آخر، Volume=مجموع) — بيانات المحرك الحالي (5 دقائق) هي المصدر
الوحيد، بدون سحب شموع ساعة أصلية من Bybit.
"""

from __future__ import annotations

HOUR_MS = 60 * 60 * 1000


def resample_to_1h(candles_5m: list[dict]) -> list[dict]:
    if not candles_5m:
        return []

    buckets: dict[int, list[dict]] = {}
    for c in candles_5m:
        hour_start = c["timestamp"] - (c["timestamp"] % HOUR_MS)
        buckets.setdefault(hour_start, []).append(c)

    out = []
    for hour_start in sorted(buckets.keys()):
        group = sorted(buckets[hour_start], key=lambda c: c["timestamp"])
        out.append({
            "timestamp": hour_start,
            "open": group[0]["open"],
            "high": max(c["high"] for c in group),
            "low": min(c["low"] for c in group),
            "close": group[-1]["close"],
            "volume": sum(c["volume"] for c in group),
        })

    return out