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
    """alert_watcher fires an entry alert on setup == "long" and nothing else,
    so an unreachable setup cannot produce one.

    The second half of this test used to assert the opposite of what it
    asserts now: a live long turning unreachable was an EXIT. That was
    measured to be wrong in practice -- the long/unreachable line is a fixed
    weekly level compared against a live price, so price crosses it several
    times a day and each crossing sent an exit the trade had not actually
    made. The pair is now one alert state (ALERT_EQUIVALENT_SETUPS), so this
    direction is silent too; see test_long_unreachable_long_is_completely_silent.
    """
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
    assert fired == []


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



# --------------------------------------------------------------------------
# "long" and "unreachable" are ONE alert state
#
# The pair is separated by a fixed weekly support level compared against a
# live price, so price crosses the line several times a day with nothing
# about the trade having changed. Replaying 30 days of real LTCUSDT 15m data
# through the engine produced 11 raw long<->unreachable flips on the slow
# combo, each of which used to become a confirmed EXIT or ENTRY message.
# --------------------------------------------------------------------------

COMBO_TF = {"trend": "1d", "entry": "4h", "levels": "1w"}
BULLISH_CONF = {"direction": "bullish", "bullish_count": 3, "bearish_count": 1,
                "total_indicators": 4}


def _result(distance_pct: float, conf: dict | None = None) -> dict:
    return {"trade_zone": trade_zone.compute("bullish", PRICE, zone_at(distance_pct), ATR,
                                             max_entry_distance_pct=3.0),
            "confluence": conf or BULLISH_CONF}


def _run(sequence, state):
    """Feeds a sequence of results through process_one and returns every alert
    that fired, in order."""
    from indicators.alert_watcher import process_one
    out = []
    for i, res in enumerate(sequence):
        a = process_one("X", "slow", COMBO_TF, res, state, f"t{i}")
        if a:
            out.append(a)
    return out


def test_long_unreachable_long_is_completely_silent():
    """The decided rule: a round trip long -> unreachable -> long fires NO
    alert of any kind, in either direction, no matter how long it dwells on
    either side or how many times it flips."""
    near, far = _result(1.0), _result(10.6)
    assert near["trade_zone"]["setup"] == "long"
    assert far["trade_zone"]["setup"] == "unreachable"

    state = {"X|slow": {"confirmed_setup": "long", "pending_setup": None,
                        "pending_count": 0, "last_checked_at": "t0"}}

    # one slow round trip, dwelling well past CONFIRMATIONS_REQUIRED on each side
    assert _run([far] * 5 + [near] * 5, state) == []
    # and rapid flapping, which is what actually happens on the live feed
    assert _run([far, near] * 10, state) == []
    # the state never left the single "long" alert state
    assert state["X|slow"]["confirmed_setup"] == "long"


def test_real_direction_changes_still_alert_exactly_as_before():
    """Only the long/unreachable pair is collapsed. A genuine loss of bullish
    confluence must still produce one EXIT, and its return one ENTRY."""
    near = _result(1.0)
    neutral = {"trade_zone": trade_zone.compute("neutral", PRICE, zone_at(1.0), ATR),
               "confluence": {"direction": "neutral", "bullish_count": 2,
                              "bearish_count": 2, "total_indicators": 4}}
    bearish = {"trade_zone": trade_zone.compute("bearish", PRICE, zone_at(1.0), ATR),
               "confluence": {"direction": "bearish", "bullish_count": 1,
                              "bearish_count": 3, "total_indicators": 4}}
    assert neutral["trade_zone"]["setup"] == "none"
    assert bearish["trade_zone"]["setup"] == "bearish_no_short"

    state = {"X|slow": {"confirmed_setup": "long", "pending_setup": None,
                        "pending_count": 0, "last_checked_at": "t0"}}
    fired = _run([neutral] * 3, state)
    assert [a["type"] for a in fired] == ["exit"]

    fired = _run([near] * 3, state)
    assert [a["type"] for a in fired] == ["entry"]

    # long -> bearish is still an exit too
    fired = _run([bearish] * 3, state)
    assert [a["type"] for a in fired] == ["exit"]


def test_becoming_bullish_while_out_of_reach_does_not_swallow_the_entry():
    """Collapsing the pair must not lose a real entry: if the setup turns
    bullish while the zone is still too far to quote, the candidate stays
    pending and the ENTRY fires on the first check where it is in reach --
    once, not once per crossing."""
    far, near = _result(10.6), _result(1.0)
    state = {"X|slow": {"confirmed_setup": "none", "pending_setup": None,
                        "pending_count": 0, "last_checked_at": "t0"}}

    assert _run([far] * 6, state) == []            # nothing quotable yet -> silence
    fired = _run([near], state)                     # in reach -> the entry it owed
    assert [a["type"] for a in fired] == ["entry"]
    assert "وقف" in fired[0]["text"] and "هدف" in fired[0]["text"]
    assert _run([far] * 4 + [near] * 4, state) == []  # and silent from then on


def test_entry_alert_is_never_built_from_an_unquotable_zone():
    """An 'unreachable' trade_zone carries no stop_loss/target/atr_value, so
    building an entry message from one would raise. No sequence may reach
    that path."""
    far = _result(10.6)
    assert "stop_loss" not in far["trade_zone"]

    for baseline in ("none", "bearish_no_short", "long", "unreachable"):
        state = {"X|slow": {"confirmed_setup": baseline, "pending_setup": None,
                            "pending_count": 0, "last_checked_at": "t0"}}
        for a in _run([far] * 8, state):
            assert a["type"] != "entry", (baseline, a)


def test_state_file_written_before_this_rule_upgrades_silently():
    """A live alert_state.json still holds literal 'unreachable' values. The
    first check after deploying must not read that as a state change."""
    near = _result(1.0)
    state = {"X|slow": {"confirmed_setup": "unreachable", "pending_setup": None,
                        "pending_count": 0, "last_checked_at": "t0"}}
    assert _run([near] * 4, state) == []
    assert state["X|slow"]["confirmed_setup"] == "long"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
