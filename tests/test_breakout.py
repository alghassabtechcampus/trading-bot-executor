"""Offline tests for breakout.py (breakout_btc + the BTC daily regime).

Reuses liq_flush's FakeExecutor and only swaps market data: breakout reads
two intervals (4h for the coins, D for BTC's regime), so klines are keyed by
(symbol, interval).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import bo_signal as bs  # noqa: E402
import breakout  # noqa: E402
from test_liq_flush import FakeExecutor as LiqFake  # noqa: E402

H4 = bs.BAR_MS
DAY = bs.DAY_MS
DAY0 = 1_760_000_000_000 - (1_760_000_000_000 % DAY)   # a UTC midnight
N4 = 40                                                # 4h bars per series
LAST_OPEN = DAY0 + 40 * DAY                            # open of the last closed 4h bar
NOW = (LAST_OPEN + H4) // 1000 + 120                   # 2 minutes after it closed


class FakeExecutor(LiqFake):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.kl: dict[tuple[str, str], list] = {}
        self.kline_calls: list[tuple[str, str]] = []

    def bybit_get(self, path, params):
        assert path == "/v5/market/kline" and params["category"] == "spot"
        key = (params["symbol"], params["interval"])
        self.kline_calls.append(key)
        return {"result": {"list": self.kl.get(key, [])}}

    def set_bars(self, symbol, interval, bars):
        self.kl[(symbol, interval)] = [[str(b["t"]), str(b["o"]), str(b["h"]), str(b["l"]),
                                        str(b["c"]), str(b["v"])] for b in reversed(bars)]


# ------------------------------------------------------------
# Series builders
# ------------------------------------------------------------

def btc_daily(up=True):
    days = []
    for i in range(41):
        days.append({"t": DAY0 + i * DAY, "o": 105, "h": 120, "l": 100, "c": 105, "v": 1})
    days[20]["l"] = 90
    days[32]["l"] = 95 if up else 85
    return days


def breakout_series(*, last_close=106.0, vol=400.0, prev_close=103.0):
    """Flat 100 with pivot highs at bars 25 (105) and 31 (104), a pivot low at 28
    (95), then a final bar breaking 104 on high volume. Built so evaluate() fires
    on the last bar; tests below assert that directly."""
    start = LAST_OPEN - (N4 - 1) * H4
    bars = [{"t": start + i * H4, "o": 100, "h": 101, "l": 99, "c": 100, "v": 100.0}
            for i in range(N4)]
    bars[25].update(h=105)
    bars[31].update(h=104)
    bars[28].update(l=95)
    bars[-2].update(o=100, h=103.5, l=99.5, c=prev_close)
    bars[-1].update(o=102, h=last_close + 0.5, l=101.5, c=last_close, v=vol)
    return bars


def setup(ex, symbol="ETHUSDT", up=True, **kw):
    ex.set_bars("BTCUSDT", "D", btc_daily(up))
    bars = breakout_series(**kw)
    ex.set_bars(symbol, "240", bars)
    return bars


@pytest.fixture
def ex():
    return FakeExecutor(price="106")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("BO_ENABLED", "true")
    monkeypatch.setenv("BO_DRY_RUN", "false")
    monkeypatch.setenv("BO_USDT_PER_TRADE", "10")
    monkeypatch.setattr(breakout.time, "time", lambda: NOW)


def tick(ex, symbols=("ETHUSDT",)):
    import bo_signal
    old = bo_signal.SYMBOLS
    bo_signal.SYMBOLS = list(symbols)
    try:
        return breakout.run_tick(ex, {})
    finally:
        bo_signal.SYMBOLS = old


# ------------------------------------------------------------
# The fixture itself
# ------------------------------------------------------------

def test_the_fixture_series_is_a_real_signal():
    sig = bs.evaluate(breakout_series())
    assert sig["signal"], sig
    assert sig["pivot_high"] == 104 and sig["stop"] == 95


def test_the_fixture_regime_is_up_and_down():
    assert bs.regime_at(bs.regime_flags(btc_daily(True)), NOW * 1000)["up"]
    assert not bs.regime_at(bs.regime_flags(btc_daily(False)), NOW * 1000)["up"]


# ------------------------------------------------------------
# Entries
# ------------------------------------------------------------

def test_signal_with_regime_up_buys_and_tags_the_order(ex):
    setup(ex)
    r = tick(ex)
    assert r["status"] == "ok" and r["regime"]["up"] is True
    assert len(ex.buys) == 1
    sym, amount, link = ex.buys[0]
    assert sym == "ETHUSDT" and amount == Decimal("10") and link.startswith("bo_b_ETHUSDT_")
    rec = ex.store["bo:open:ETHUSDT"]
    assert rec["stop"] == 95 and rec["initial_stop"] == 95 and rec["strategy"] == "breakout_btc"
    assert ex.store["bo:used:ETHUSDT"]["pivot_high_t"] == rec["pivot_high_t"]
    assert any("شراء (اختراق)" in m for m in r["messages"])
    assert r["journal"][0]["event"] == "entry"


def test_regime_down_blocks_every_entry_and_skips_the_scan(ex):
    setup(ex, up=False)
    r = tick(ex)
    assert ex.buys == []
    assert r["regime"]["up"] is False
    assert {"symbol": None, "reason": "regime_not_up_scan_skipped"} in r["skipped"]
    assert ("ETHUSDT", "240") not in ex.kline_calls


def test_low_volume_breakout_is_ignored(ex):
    setup(ex, vol=120.0)
    tick(ex)
    assert ex.buys == []


def test_a_signal_older_than_two_hours_is_refused(ex, monkeypatch):
    setup(ex)
    monkeypatch.setattr(breakout.time, "time", lambda: NOW + 2 * 3600 + 60)
    r = tick(ex)
    assert ex.buys == [] and r["skipped"][0]["reason"] == "signal_too_old"


def test_a_missed_tick_is_caught_up_within_two_hours(ex, monkeypatch):
    setup(ex)
    monkeypatch.setattr(breakout.time, "time", lambda: NOW + 3600)
    tick(ex)
    assert len(ex.buys) == 1


def test_same_pivot_is_not_entered_twice(ex):
    setup(ex)
    tick(ex)
    del ex.store["bo:open:ETHUSDT"]          # as if it closed
    r = tick(ex)
    assert len(ex.buys) == 1
    assert any(s["reason"] == "pivot_already_used" for s in r["skipped"])


def test_symbol_held_by_liq_flush_is_skipped(ex):
    setup(ex)
    ex.store["liq:open:ETHUSDT"] = {"symbol": "ETHUSDT"}
    r = tick(ex)
    assert ex.buys == [] and any(s["reason"] == "foreign_liq_flush" for s in r["skipped"])


def test_symbol_held_by_strategy_d_is_skipped(ex):
    setup(ex)
    ex.tracked["ETHUSDT"] = {"x": 1}
    tick(ex)
    assert ex.buys == []


def test_insufficient_balance_skips_and_warns(ex):
    setup(ex)
    ex.usdt = Decimal("5")
    r = tick(ex)
    assert ex.buys == [] and any("رصيد غير كافٍ" in m for m in r["messages"])


def test_dry_run_places_no_orders_and_uses_its_own_prefix(ex, monkeypatch):
    monkeypatch.setenv("BO_DRY_RUN", "true")
    setup(ex)
    tick(ex)
    assert ex.buys == [] and "bo:dry:open:ETHUSDT" in ex.store and "bo:open:ETHUSDT" not in ex.store


def test_state_never_touches_other_strategies_keys(ex):
    setup(ex)
    tick(ex)
    assert all(k.startswith("bo:") for k in ex.store)


# ------------------------------------------------------------
# Exits
# ------------------------------------------------------------

def _advance(ex, bars, new_bars, monkeypatch, hours_after=0):
    bars = bars + new_bars
    ex.set_bars("ETHUSDT", "240", bars)
    last_close_s = (bars[-1]["t"] + H4) // 1000
    monkeypatch.setattr(breakout.time, "time", lambda: last_close_s + 120 + hours_after * 3600)
    return bars


def _next(bars, **kw):
    t = bars[-1]["t"] + H4
    b = {"t": t, "o": 106, "h": 107, "l": 105, "c": 106, "v": 100.0}
    b.update(kw)
    return b


def test_holds_while_closes_stay_above_the_stop(ex, monkeypatch):
    bars = setup(ex)
    tick(ex)
    bars = _advance(ex, bars, [_next(bars)], monkeypatch)
    r = tick(ex)
    assert ex.sells == [] and r["exits"][0]["status"] == "holding"


def test_a_wick_below_the_stop_is_not_an_exit_only_a_close_is(ex, monkeypatch):
    bars = setup(ex)
    tick(ex)
    bars = _advance(ex, bars, [_next(bars, l=90, c=100)], monkeypatch)
    tick(ex)
    assert ex.sells == []


def test_a_4h_close_below_the_stop_sells(ex, monkeypatch):
    bars = setup(ex)
    tick(ex)
    ex.prices["ETHUSDT"] = Decimal("94")
    bars = _advance(ex, bars, [_next(bars, o=96, h=96, l=93, c=94)], monkeypatch)
    r = tick(ex)
    assert len(ex.sells) == 1 and ex.sells[0][2].startswith("bo_s_ETHUSDT_")
    assert "bo:open:ETHUSDT" not in ex.store
    assert r["journal"][-1]["event"] == "exit" and r["journal"][-1]["exit_reason"] == "trail_stop"
    assert any("بيع (اختراق)" in m for m in r["messages"])


def test_stop_trails_up_to_a_new_higher_pivot_low(ex, monkeypatch):
    bars = setup(ex)
    tick(ex)
    # two bars, a dip to 104, then two higher lows -> pivot low 104 confirmed
    new = []
    for lo in (106, 106.5, 104, 106.5, 107):
        b = _next(bars + new, l=lo, h=lo + 3, o=lo + 1, c=lo + 2)
        new.append(b)
    bars = _advance(ex, bars, new, monkeypatch)
    r = tick(ex)
    assert ex.store["bo:open:ETHUSDT"]["stop"] == 104
    assert r["exits"][0].get("stop_raised_from") == 95


def test_an_exit_bar_missed_by_a_tick_is_still_caught(ex, monkeypatch):
    bars = setup(ex)
    tick(ex)
    ex.prices["ETHUSDT"] = Decimal("97")
    new = [_next(bars, o=96, h=96, l=93, c=94)]
    new.append(_next(bars + new, o=95, h=98, l=95, c=97))   # recovered after
    bars = _advance(ex, bars, new, monkeypatch)
    tick(ex)
    assert len(ex.sells) == 1


def test_kill_switch_blocks_entries_but_still_exits(ex, monkeypatch):
    bars = setup(ex)
    tick(ex)
    monkeypatch.setenv("BO_ENABLED", "false")
    ex.prices["ETHUSDT"] = Decimal("94")
    bars = _advance(ex, bars, [_next(bars, o=96, h=96, l=93, c=94)], monkeypatch)
    r = tick(ex)
    assert r["status"] == "disabled_exits_only" and len(ex.sells) == 1


def test_disabled_with_nothing_open_does_nothing(ex, monkeypatch):
    monkeypatch.setenv("BO_ENABLED", "false")
    setup(ex)
    r = tick(ex)
    assert r["status"] == "disabled" and ex.buys == [] and ex.kline_calls == []


def test_defaults_are_off(monkeypatch):
    monkeypatch.delenv("BO_ENABLED", raising=False)
    monkeypatch.delenv("BO_DRY_RUN", raising=False)
    assert breakout.is_enabled() is False and breakout.is_dry_run() is False


# ------------------------------------------------------------
# Regime bookkeeping, lock, robustness
# ------------------------------------------------------------

def test_regime_is_stored_and_a_flip_sends_one_message(ex):
    setup(ex, up=False)
    r1 = tick(ex)
    assert ex.store["bo:regime"]["up"] is False
    assert not any("الحَكَم" in m for m in r1["messages"])   # first reading: no flip
    setup(ex, up=True)
    r2 = tick(ex)
    assert any("الحَكَم: البيتكوين صار صاعد" in m for m in r2["messages"])
    r3 = tick(ex)
    assert not any("الحَكَم" in m for m in r3["messages"])


def test_regime_failure_blocks_entries_but_exits_already_ran(ex, monkeypatch):
    bars = setup(ex)
    tick(ex)
    ex.kl.pop(("BTCUSDT", "D"))
    ex.prices["ETHUSDT"] = Decimal("94")
    bars = _advance(ex, bars, [_next(bars, o=96, h=96, l=93, c=94)], monkeypatch)
    r = tick(ex)
    assert len(ex.sells) == 1                     # the exit happened first
    assert r["regime"]["up"] is False or r["errors"]


def test_concurrent_tick_is_locked_out(ex):
    setup(ex)
    ex.store["bo:tick:lock"] = {"since": NOW}
    r = tick(ex)
    assert r["status"] == "locked" and ex.buys == []


def test_lock_is_released_after_a_tick(ex):
    setup(ex)
    tick(ex)
    assert "bo:tick:lock" not in ex.store


def test_one_broken_symbol_does_not_stop_the_others(ex):
    setup(ex, symbol="SOLUSDT")
    ex.kl[("ETHUSDT", "240")] = [["garbage"]]
    r = tick(ex, symbols=("ETHUSDT", "SOLUSDT"))
    assert [b[0] for b in ex.buys] == ["SOLUSDT"]
    assert r["status"] == "ok"


def test_overrides_rejected_when_live(ex):
    r = breakout.run_tick(ex, {"now": NOW})
    assert r["status"] == "overrides_rejected"
