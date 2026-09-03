"""Covers the two guards added after the first live-alert review:

  1. trade_zone must refuse to package an entry zone the market is nowhere
     near as an actionable "long" (the LTCUSDT slow-combo case: a zone
     ~10.6% below price, quoted as a buy opportunity).
  2. alert_watcher's archive must be append-only -- a second run may never
     shrink, reorder, or rewrite what a first run wrote.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicators import trade_zone  # noqa: E402

PRICE = 100.0
ATR = 1.0


def zone_at(distance_pct: float) -> dict:
    """A support level placed so the TOP of the entry zone sits ~distance_pct
    below PRICE. entry_high = support * (1 + 0.3/100), so solve for support."""
    entry_high = PRICE * (1 - distance_pct / 100)
    support = entry_high / 1.003
    return {"nearest_support": support, "nearest_resistance": PRICE * 1.10}


def test_reachable_zone_still_produces_a_long():
    tz = trade_zone.compute("bullish", PRICE, zone_at(1.0), ATR, max_entry_distance_pct=3.0)
    assert tz["setup"] == "long"
    assert tz["entry_distance_pct"] < 3.0


def test_far_zone_becomes_unreachable_not_long():
    tz = trade_zone.compute("bullish", PRICE, zone_at(10.6), ATR, max_entry_distance_pct=3.0)
    assert tz["setup"] == "unreachable"
    assert tz["direction"] == "bullish"          # the read itself is still reported
    assert 10.0 < tz["entry_distance_pct"] < 11.0
    assert "stop_loss" not in tz and "target" not in tz  # nothing to act on is offered


def test_threshold_is_a_boundary_not_a_range():
    """Just inside the limit is tradeable; just outside is not."""
    assert trade_zone.compute("bullish", PRICE, zone_at(2.9), ATR,
                              max_entry_distance_pct=3.0)["setup"] == "long"
    assert trade_zone.compute("bullish", PRICE, zone_at(3.1), ATR,
                              max_entry_distance_pct=3.0)["setup"] == "unreachable"


def test_threshold_is_configurable():
    far = zone_at(5.0)
    assert trade_zone.compute("bullish", PRICE, far, ATR, max_entry_distance_pct=3.0)["setup"] == "unreachable"
    assert trade_zone.compute("bullish", PRICE, far, ATR, max_entry_distance_pct=8.0)["setup"] == "long"


def test_price_inside_or_below_zone_is_always_reachable():
    """No support below price -> reference falls back to price itself, so the
    zone straddles the market and the distance is negative."""
    tz = trade_zone.compute("bullish", PRICE, {"nearest_support": None, "nearest_resistance": 110.0},
                            ATR, max_entry_distance_pct=3.0)
    assert tz["setup"] == "long"
    assert tz["entry_distance_pct"] < 0


def test_unreachable_never_reaches_the_entry_alert_path():
    """alert_watcher fires an entry alert on setup == "long" and nothing
    else, so an unreachable setup cannot produce one -- and if a live long
    turns unreachable, that is an EXIT, never a new buy."""
    from indicators.alert_watcher import process_one

    combo_tf = {"trend": "1d", "entry": "4h", "levels": "1w"}
    result = {"trade_zone": trade_zone.compute("bullish", PRICE, zone_at(10.6), ATR,
                                               max_entry_distance_pct=3.0),
              "confluence": {"direction": "bullish", "bullish_count": 3, "bearish_count": 1,
                             "total_indicators": 4}}
    assert result["trade_zone"]["setup"] == "unreachable"

    state = {"X|slow": {"confirmed_setup": "none", "pending_setup": None,
                        "pending_count": 0, "last_checked_at": "t0"}}
    fired = [process_one("X", "slow", combo_tf, result, state, f"t{i}") for i in range(1, 5)]
    assert all(a is None or a["type"] != "entry" for a in fired)

    state = {"X|slow": {"confirmed_setup": "long", "pending_setup": None,
                        "pending_count": 0, "last_checked_at": "t0"}}
    fired = [a for a in (process_one("X", "slow", combo_tf, result, state, f"t{i}")
                         for i in range(1, 5)) if a]
    assert len(fired) == 1 and fired[0]["type"] == "exit"


def test_alert_log_is_append_only(tmp_path, monkeypatch):
    """Two writes must leave the first run's line byte-identical and still
    first in the file."""
    import importlib
    monkeypatch.setenv("INDICATORS_DATA_DIR", str(tmp_path))
    import indicators.alert_watcher as aw
    importlib.reload(aw)
    try:
        aw.append_alert_log([{"sent_at": "t1", "symbol": "AAA", "alert_type": "entry"}])
        first = aw.ALERT_LOG_PATH.read_text(encoding="utf-8")
        aw.append_alert_log([{"sent_at": "t2", "symbol": "BBB", "alert_type": "exit"},
                             {"sent_at": "t2", "symbol": "CCC", "alert_type": "entry"}])
        after = aw.ALERT_LOG_PATH.read_text(encoding="utf-8")

        assert after.startswith(first), "the first run's bytes were rewritten"
        lines = [json.loads(x) for x in after.splitlines()]
        assert len(lines) == 3
        assert [x["symbol"] for x in lines] == ["AAA", "BBB", "CCC"]
    finally:
        monkeypatch.delenv("INDICATORS_DATA_DIR", raising=False)
        importlib.reload(aw)


def test_log_record_carries_numbers_not_just_text():
    """The whole point of the archive: a later review reads fields, never a
    regex over the Arabic message."""
    from indicators.alert_watcher import build_entry_message, build_log_record

    tz = trade_zone.compute("bullish", PRICE, zone_at(1.0), ATR, max_entry_distance_pct=3.0)
    result = {"trade_zone": tz, "current_price": PRICE, "as_of": "2026-09-03T00:00:00+00:00",
              "confluence": {"direction": "bullish", "bullish_count": 3, "bearish_count": 1,
                             "total_indicators": 4}}
    combo_tf = {"trend": "1h", "entry": "15m", "levels": "1d"}
    text = build_entry_message("LTCUSDT", "fast", combo_tf, result)
    rec = build_log_record("LTCUSDT", "fast", combo_tf, result,
                           {"type": "entry", "text": text}, "2026-09-03T00:00:00+00:00")

    for field in ("sent_at", "symbol", "combo", "alert_type", "entry_low", "entry_high",
                  "stop", "target", "trend_timeframe", "entry_timeframe", "levels_timeframe"):
        assert rec[field] is not None, field
    for field in ("entry_low", "entry_high", "stop", "target"):
        assert isinstance(rec[field], (int, float)), field
    assert rec["entry_low"] == tz["entry_zone"][0]
    assert rec["stop"] == tz["stop_loss"]
    assert json.loads(json.dumps(rec, ensure_ascii=False))  # archive row is serialisable


def test_trailing_fields_are_advisory_and_do_not_change_any_decision():
    """The whole contract of the trailing hint: it adds fields and changes
    nothing. Same inputs, different trail multipliers -> byte-identical
    setup, entry zone, stop, target and R:R."""
    a = trade_zone.compute("bullish", PRICE, zone_at(1.0), ATR,
                           trail_arm_atr_mult=0.5, trail_atr_mult=1.0)
    b = trade_zone.compute("bullish", PRICE, zone_at(1.0), ATR,
                           trail_arm_atr_mult=9.9, trail_atr_mult=9.9)
    for field in ("setup", "direction", "entry_zone", "stop_loss", "target",
                  "rr_ratio", "reference_level", "entry_distance_pct",
                  "risk_pct_of_price", "reward_pct_of_price"):
        assert a[field] == b[field], field


def test_trailing_fields_present_on_long_and_absent_elsewhere():
    long_tz = trade_zone.compute("bullish", PRICE, zone_at(1.0), ATR)
    assert long_tz["setup"] == "long"
    assert long_tz["trail_arm_atr_mult"] == 0.5
    assert long_tz["trail_atr_mult"] == 1.0
    assert long_tz["atr_value"] == ATR

    for tz in (trade_zone.compute("neutral", PRICE, zone_at(1.0), ATR),
               trade_zone.compute("bearish", PRICE, zone_at(1.0), ATR),
               trade_zone.compute("bullish", PRICE, zone_at(10.6), ATR)):
        assert "atr_value" not in tz, tz["setup"]


def test_trail_hint_numbers_are_arithmetically_right():
    from indicators.alert_watcher import build_trail_hint

    tz = trade_zone.compute("bullish", PRICE, zone_at(1.0), ATR)
    mid = sum(tz["entry_zone"]) / 2
    arm = mid + 0.5 * ATR
    then = arm - 1.0 * ATR
    hint = build_trail_hint(tz)
    assert f"{arm:,.4f}" in hint, hint
    assert f"{then:,.4f}" in hint, hint
    # the suggested level must actually be an IMPROVEMENT on the original
    # stop, otherwise the advice is worse than doing nothing
    assert then > tz["stop_loss"]


def test_entry_message_carries_the_hint_and_exit_message_does_not():
    from indicators.alert_watcher import build_entry_message, build_exit_message

    tz = trade_zone.compute("bullish", PRICE, zone_at(1.0), ATR)
    result = {"trade_zone": tz, "current_price": PRICE, "as_of": "2026-09-03T00:00:00+00:00",
              "confluence": {"direction": "bullish", "bullish_count": 3, "bearish_count": 1,
                             "total_indicators": 4}}
    combo_tf = {"trend": "1h", "entry": "15m", "levels": "1d"}
    assert "💡" in build_entry_message("LTCUSDT", "fast", combo_tf, result)
    assert "💡" not in build_exit_message("LTCUSDT", "fast", result)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
