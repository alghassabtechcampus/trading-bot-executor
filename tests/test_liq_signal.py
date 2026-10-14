"""Offline tests for liq_signal.py's two-bar evaluation. No network.

liq_signal used to look at the last closed bar only, which meant the signal was
visible for exactly one tick. On 2026-10-08 that tick (18:02) died with a DNS
error and AVAXUSDT's 17:00 signal was gone for good -- see
handoff/LIQ_STATUS_CHECK_AR.md. It now evaluates the last EVAL_BARS bars.

What has to stay true for that to be safe:

  causality     an older bar's z is the same number it would have been on its
                own tick -- the window is past-only, so time passing must not
                change a verdict already given
  one signal    the same flush offered on two consecutive ticks must not become
                two signals for the same symbol
  rows          scan.checked in liq_flush counts rows, so rows stays one per
                symbol however many bars are evaluated
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import liq_signal  # noqa: E402


def frame(n=800, *, dips=(), seed=7):
    """Hourly close/oi with small noise, plus a one-bar dip at each index in
    `dips` (negative indexes, -1 is the last bar).

    The dip is one bar wide on purpose: a drop that persists would also drag
    r4 down for the next three bars, and then a test could not tell which bar
    the signal came from.
    """
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-09-01", periods=n, freq="h", tz="UTC")
    close = 100 * (1 + rng.normal(0, 0.001, n)).cumprod()
    oi = 1_000_000 * (1 + rng.normal(0, 0.001, n)).cumprod()
    df = pd.DataFrame({"close": close, "oi": oi}, index=idx)
    for d in dips:
        df.iloc[d, df.columns.get_loc("close")] *= 0.95
        df.iloc[d, df.columns.get_loc("oi")] *= 0.95
    return df


# ============================================================
# evaluate
# ============================================================

def test_a_quiet_market_signals_nothing():
    out = liq_signal.evaluate(frame())
    assert out["signal"] is False
    assert out["late_bars"] == 0
    assert out["z_r4"] > -3 and out["z_oi4"] > -3


def test_the_last_closed_bar_signals_at_offset_zero():
    out = liq_signal.evaluate(frame(dips=(-1,)))
    assert out["signal"] is True
    assert out["late_bars"] == 0
    assert out["z_r4"] <= -3 and out["z_oi4"] <= -3


def test_offset_one_reads_the_bar_before_the_last():
    df = frame(dips=(-2,))

    assert liq_signal.evaluate(df, 0)["signal"] is False    # the recovered bar
    late = liq_signal.evaluate(df, 1)
    assert late["signal"] is True
    assert late["late_bars"] == 1
    assert late["bar"] == df.index[-2]


def test_an_older_bars_verdict_does_not_change_as_time_passes():
    """The whole catch-up rests on this: z is computed from the bar's own past
    (mean and std are shifted by one), so evaluating a bar an hour later must
    give the number its own tick would have given. If this drifts, the live log
    stops being comparable with the backtest."""
    df = frame(dips=(-2,))

    on_its_own_tick = liq_signal.evaluate(df.iloc[:-1], 0)   # the bar was last
    one_tick_later = liq_signal.evaluate(df, 1)               # now it is older

    assert on_its_own_tick["bar"] == one_tick_later["bar"]
    assert on_its_own_tick["z_r4"] == pytest.approx(one_tick_later["z_r4"])
    assert on_its_own_tick["z_oi4"] == pytest.approx(one_tick_later["z_oi4"])
    assert on_its_own_tick["signal"] == one_tick_later["signal"] is True


# ============================================================
# scan
# ============================================================

@pytest.fixture
def frames(monkeypatch):
    """Route liq_signal.fetch to a dict of per-symbol frames instead of Bybit."""
    table: dict[str, pd.DataFrame] = {}

    def fake_fetch(sym):
        if sym not in table:
            raise RuntimeError(f"no frame for {sym}")
        return table[sym]

    monkeypatch.setattr(liq_signal, "fetch", fake_fetch)
    return table


def test_scan_catches_the_bar_a_failed_tick_would_have_missed(frames):
    frames["AVAXUSDT"] = frame(dips=(-2,))

    rows, sigs = liq_signal.scan(["AVAXUSDT"])

    assert len(sigs) == 1
    assert sigs[0]["symbol"] == "AVAXUSDT"
    assert sigs[0]["late_bars"] == 1
    # the reporting row is still the last closed bar, which signals nothing
    assert len(rows) == 1 and rows[0]["signal"] is False


def test_two_signalling_bars_on_one_symbol_produce_one_signal(frames):
    """Two consecutive bars signalling is one flush, not two. The tick after an
    entry sees the bar it just entered a second time (as late_bars=1); dropping
    the older one here means liq_flush's cooldown is the second line of defence,
    not the first."""
    frames["AVAXUSDT"] = frame(dips=(-1, -2))

    rows, sigs = liq_signal.scan(["AVAXUSDT"])

    assert len(sigs) == 1, "the same flush was reported as two signals"
    assert sigs[0]["late_bars"] == 0, "the freshest bar is the one to enter"


def test_scan_returns_one_row_per_symbol_whatever_the_window(frames):
    """liq_flush reports scan.checked = len(rows) and the runbook expects 17.
    Evaluating two bars must not double it."""
    for s in ("AVAXUSDT", "ETCUSDT", "SOLUSDT"):
        frames[s] = frame(dips=(-2,))

    rows, sigs = liq_signal.scan(["AVAXUSDT", "ETCUSDT", "SOLUSDT"])

    assert len(rows) == 3
    assert [r["symbol"] for r in rows] == ["AVAXUSDT", "ETCUSDT", "SOLUSDT"]
    assert len(sigs) == 3


def test_signals_stay_sorted_most_extreme_first(frames):
    frames["MILDUSDT"] = frame(dips=(-1,), seed=3)
    frames["HARDUSDT"] = frame(dips=(-1,), seed=3)
    # make HARD twice as extreme as MILD on the signalling bar
    df = frames["HARDUSDT"].copy()
    df.iloc[-1, df.columns.get_loc("close")] *= 0.95
    df.iloc[-1, df.columns.get_loc("oi")] *= 0.95
    frames["HARDUSDT"] = df

    _, sigs = liq_signal.scan(["MILDUSDT", "HARDUSDT"])

    assert [s["symbol"] for s in sigs] == ["HARDUSDT", "MILDUSDT"]


def test_one_symbol_failing_does_not_hide_the_others(frames):
    """A per-symbol error becomes a row with 'error' -- liq_flush counts those
    into scan.errors and warns. A second evaluated bar must not change that."""
    frames["AVAXUSDT"] = frame(dips=(-2,))
    # ETCUSDT deliberately absent -> fetch raises

    rows, sigs = liq_signal.scan(["ETCUSDT", "AVAXUSDT"])

    assert len(rows) == 2
    assert "error" in rows[0] and rows[0]["signal"] is False
    assert len(sigs) == 1 and sigs[0]["symbol"] == "AVAXUSDT"


def test_a_frame_shorter_than_the_window_does_not_crash(frames):
    """min_periods is 360, so a short frame gives NaN z rather than an error --
    and a one-row frame must not make evaluate(df, 1) walk off the end."""
    frames["AVAXUSDT"] = frame(n=1)

    rows, sigs = liq_signal.scan(["AVAXUSDT"])

    assert len(rows) == 1
    assert sigs == []
