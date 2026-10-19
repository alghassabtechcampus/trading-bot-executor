"""bo_signal.py must reproduce the backtest that justified the strategy.

The backtest ran in a browser (JavaScript) against Bybit spot 4h data. Its
`bt()` loop is ported here line by line as `reference_backtest`. The live code
does not run that loop: each hourly tick only sees the closed bars so far and
calls `evaluate` (entries) and `trail_and_check` (exits). So the key test
replays a long synthetic series bar by bar through the LIVE functions and
demands the exact same trades as the reference loop -- same entry bar, same
exit bar, same stop. Any look-ahead or off-by-one in the live path breaks it.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bo_signal as bs  # noqa: E402

H4 = bs.BAR_MS
DAY = bs.DAY_MS
T_START = 1_672_531_200_000  # 2023-01-01 00:00 UTC


# ============================================================
# Reference: the JS backtest, ported verbatim
# ============================================================

def reference_backtest(b, regime_up, k=2):
    highs, lows = bs.pivots(b, k)
    vs, s = [], 0.0
    for i in range(len(b)):
        s += b[i]["v"]
        if i >= 20:
            s -= b[i - 20]["v"]
        vs.append(s / 20 if i >= 19 else None)
    pos, trades, hi, li, used_high = None, [], 0, 0, -1
    last_h, last_l = [], []
    for i in range(len(b)):
        while hi < len(highs) and highs[hi]["conf"] <= i:
            last_h.append(highs[hi]); hi += 1
        while li < len(lows) and lows[li]["conf"] <= i:
            last_l.append(lows[li]); li += 1
        c = b[i]
        if pos:
            if last_l:
                pl = last_l[-1]
                if pl["i"] > pos["ei"] - k and pl["p"] > pos["stop"]:
                    pos["stop"] = pl["p"]
            if c["c"] < pos["stop"]:
                trades.append({"ei": pos["ei"], "xi": i, "e": pos["e"], "x": c["c"],
                               "stop": pos["stop"]})
                pos = None
            continue
        if len(last_h) < 2 or len(last_l) < 1 or vs[i] is None:
            continue
        h1, pl = last_h[-1], last_l[-1]
        if h1["i"] == used_high:
            continue
        if c["c"] <= h1["p"] or b[i - 1]["c"] > h1["p"]:
            continue
        if not c["v"] > 1.5 * vs[i]:
            continue
        if not (c["c"] > c["o"] and (c["c"] - c["l"]) >= 0.5 * (c["h"] - c["l"])):
            continue
        if not regime_up(c["t"]):
            continue
        if c["c"] <= pl["p"]:
            continue
        used_high = h1["i"]
        pos = {"e": c["c"], "ei": i, "stop": pl["p"]}
    return trades


# ============================================================
# Live replay: what the hourly ticks would do, bar by bar
# ============================================================

def live_replay(b, flags):
    trades, pos, used_t = [], None, None
    for i in range(len(b)):
        seen = b[:i + 1]
        if pos:
            r = bs.trail_and_check(seen, entry_bar_t=pos["bar_t"], stop=pos["stop"],
                                   checked_through=pos["checked"])
            pos["stop"], pos["checked"] = r["stop"], r["checked_through"]
            if r["exit_bar"] is not None:
                xi = next(j for j, x in enumerate(b) if x["t"] == r["exit_bar"]["t"])
                trades.append({"ei": pos["ei"], "xi": xi, "e": pos["e"],
                               "x": r["exit_bar"]["c"], "stop": pos["stop"]})
                pos = None
            continue
        sig = bs.evaluate(seen)
        if not sig["signal"]:
            continue
        reg = bs.regime_at(flags, sig["bar_t"])
        if not (reg and reg["up"]):
            continue
        if used_t == sig["pivot_high_t"]:
            continue
        used_t = sig["pivot_high_t"]
        pos = {"e": sig["close"], "ei": i, "bar_t": sig["bar_t"], "stop": sig["stop"],
               "checked": sig["bar_t"]}
    return trades


def synthetic(n, seed):
    rnd = random.Random(seed)
    bars, p, drift = [], 100.0, 0.0
    for i in range(n):
        if i % 150 == 0:
            drift = rnd.choice([-0.002, 0.0, 0.003, 0.004])
        o = p
        c = max(1.0, o * (1 + drift + rnd.gauss(0, 0.012)))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.004)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, 0.004)))
        v = rnd.uniform(50, 150) * (rnd.choice([1, 1, 1, 1, 3]) if c > o else 1)
        bars.append({"t": T_START + i * H4, "o": o, "h": h, "l": l, "c": c, "v": v})
        p = c
    return bars


def daily_from_4h(b):
    out = []
    for j in range(0, len(b) - 5, 6):
        g = b[j:j + 6]
        out.append({"t": g[0]["t"], "o": g[0]["o"], "h": max(x["h"] for x in g),
                    "l": min(x["l"] for x in g), "c": g[-1]["c"], "v": sum(x["v"] for x in g)})
    return out


@pytest.mark.parametrize("seed", [1, 2, 3, 5, 6, 7, 8, 9, 10, 11])
def test_live_replay_matches_the_backtest_trade_for_trade(seed):
    b = synthetic(900, seed)
    flags = bs.regime_flags(daily_from_4h(b))

    def regime_up(t):
        r = bs.regime_at(flags, t)
        return bool(r and r["up"])

    ref = reference_backtest(b, regime_up)
    live = live_replay(b, flags)
    assert len(ref) > 0, "synthetic series should produce some trades"
    assert [(t["ei"], t["xi"]) for t in live] == [(t["ei"], t["xi"]) for t in ref]
    for a, r in zip(live, ref):
        assert a["stop"] == pytest.approx(r["stop"])
        assert a["e"] == pytest.approx(r["e"]) and a["x"] == pytest.approx(r["x"])


def test_regime_off_means_no_trades():
    b = synthetic(600, 3)
    assert reference_backtest(b, lambda t: False) == []
    assert live_replay(b, []) == []


# ============================================================
# Small exact cases
# ============================================================

def bar(i, o, h, l, c, v=100.0):
    return {"t": T_START + i * H4, "o": o, "h": h, "l": l, "c": c, "v": v}


def test_a_pivot_needs_two_strictly_lower_bars_each_side():
    b = [bar(0, 1, 10, 1, 1), bar(1, 1, 11, 1, 1), bar(2, 1, 12, 1, 1),
         bar(3, 1, 11, 1, 1), bar(4, 1, 12, 1, 1)]
    highs, _ = bs.pivots(b)
    assert highs == []          # bar 4 equals bar 2 -> not strictly lower
    b[4] = bar(4, 1, 10, 1, 1)
    highs, _ = bs.pivots(b)
    assert [h["i"] for h in highs] == [2] and highs[0]["conf"] == 4


def test_parse_drops_the_open_candle_and_sorts_oldest_first():
    now = T_START + 3 * H4 + 60_000
    rows = [[str(T_START + i * H4), "1", "2", "0.5", "1.5", "10"] for i in (3, 2, 1, 0)]
    bars = bs.parse_klines(rows, H4, now)
    assert [x["t"] for x in bars] == [T_START, T_START + H4, T_START + 2 * H4]


def test_regime_needs_rising_lows_and_close_above_the_last_low():
    def day(i, l, c):
        return {"t": T_START + i * DAY, "o": c, "h": 120, "l": l, "c": c, "v": 1}
    days = [day(i, 100, 105) for i in range(30)]
    days[10] = day(10, 90, 105)
    days[20] = day(20, 95, 105)
    flags = bs.regime_flags(days)
    assert flags[-1]["up"] is True
    days[20] = day(20, 85, 105)          # lower low -> not up
    assert bs.regime_flags(days)[-1]["up"] is False
    days[20] = day(20, 95, 105)
    days[-1] = day(29, 100, 94)           # close below the last low -> not up
    assert bs.regime_flags(days)[-1]["up"] is False


def test_regime_at_only_sees_days_that_closed():
    days = [{"t": T_START + i * DAY, "o": 1, "h": 2, "l": 1, "c": 1, "v": 1} for i in range(3)]
    flags = bs.regime_flags(days)
    assert bs.regime_at(flags, T_START + DAY - 1) is None
    assert bs.regime_at(flags, T_START + DAY)["day"] == T_START
    assert bs.regime_at(flags, T_START + 3 * DAY - 1)["day"] == T_START + DAY
