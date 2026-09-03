"""Guards the one invariant the alert watcher must never break: it may only
emit a "new buy opportunity" alert for a state the dashboard itself would
show as a long setup -- i.e. trade_zone.setup == "long", nothing looser.

The 2-2 tie is the case that motivated these tests: confluence calls a tie
"neutral", trade_zone turns "neutral" into setup "none", and so no entry
alert may exist for it. Test 1 proves that over EVERY possible reading of
the 4 indicators (3**4 = 81 combinations), not just a sampled few.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicators import confluence, trade_zone  # noqa: E402
from indicators.alert_watcher import process_one  # noqa: E402

INDICATORS = ("macd", "adx", "ichimoku", "vwap")
STATES = ("bullish", "bearish", "neutral")

# A plausible, self-consistent market snapshot; the numbers only need to be
# valid enough that a bullish read produces a real "long" (support below
# price, resistance above, positive ATR) -- the tests are about direction.
PRICE = 100.0
SR = {"nearest_support": 98.0, "nearest_resistance": 110.0}
ATR = 1.0
COMBO_TF = {"trend": "1h", "entry": "15m", "levels": "1d"}


def result_for(states: tuple[str, ...]) -> dict:
    indicator_states = {name: {"state": st} for name, st in zip(INDICATORS, states)}
    conf = confluence.compute(indicator_states)
    tz = trade_zone.compute(conf["direction"], PRICE, SR, ATR)
    return {"confluence": conf, "trade_zone": tz}


def all_combinations():
    return itertools.product(STATES, repeat=len(INDICATORS))


def fire_entry_alert(result: dict) -> dict | None:
    """Drives the real watcher state machine to the point where it would
    fire, if it ever would: baseline on a non-long setup, then feed the
    candidate for CONFIRMATIONS_REQUIRED consecutive checks."""
    state = {"TESTUSDT|سريعة": {"confirmed_setup": "none", "pending_setup": None,
                                "pending_count": 0, "last_checked_at": "t0"}}
    if result["trade_zone"]["setup"] == "none":
        state["TESTUSDT|سريعة"]["confirmed_setup"] = "bearish_no_short"
    alert = None
    for i in range(4):
        alert = process_one("TESTUSDT", "سريعة", COMBO_TF, result, state, f"t{i+1}")
        if alert:
            return alert
    return alert


def test_tie_never_produces_a_long_setup():
    """Every tie (bullish_count == bearish_count), including 2-2, must be
    neutral in confluence and setup "none" in trade_zone."""
    ties = 0
    for states in all_combinations():
        r = result_for(states)
        conf, tz = r["confluence"], r["trade_zone"]
        if conf["bullish_count"] != conf["bearish_count"]:
            continue
        ties += 1
        assert conf["direction"] == "neutral", (states, conf)
        assert tz["setup"] == "none", (states, tz)
    assert ties > 0
    print(f"  {ties} tied combinations, all neutral/none")


def test_two_two_ties_specifically_fire_no_entry_alert():
    """The exact reported case: 2 bullish + 2 bearish must never reach the
    user as a 'new buy opportunity'."""
    checked = 0
    for states in all_combinations():
        r = result_for(states)
        conf = r["confluence"]
        if not (conf["bullish_count"] == 2 and conf["bearish_count"] == 2):
            continue
        checked += 1
        alert = fire_entry_alert(r)
        assert alert is None or alert["type"] != "entry", (states, alert)
    assert checked > 0, "no 2-2 combination was generated -- test is vacuous"
    print(f"  {checked} true 2-2 combinations, zero entry alerts")


def test_entry_alerts_only_ever_come_from_setup_long():
    """The consistency requirement itself: across all 81 readings, an entry
    alert appears if and only if trade_zone.setup == 'long'."""
    entries, longs = 0, 0
    for states in all_combinations():
        r = result_for(states)
        is_long = r["trade_zone"]["setup"] == "long"
        alert = fire_entry_alert(r)
        is_entry = alert is not None and alert["type"] == "entry"
        assert is_entry == is_long, (states, r["trade_zone"]["setup"], alert)
        longs += is_long
        entries += is_entry
    assert longs > 0
    print(f"  {entries} entry alerts == {longs} long setups (of 81 readings)")


def test_entry_message_states_the_full_split():
    """A message must not be readable as a tie. Any entry message has to
    name the bearish and neutral counts too, not just the bullish one."""
    for states in all_combinations():
        r = result_for(states)
        if r["trade_zone"]["setup"] != "long":
            continue
        alert = fire_entry_alert(r)
        assert alert is not None and alert["type"] == "entry"
        conf = r["confluence"]
        bull, bear = conf["bullish_count"], conf["bearish_count"]
        neutral = conf["total_indicators"] - bull - bear
        text = alert["text"]
        assert f"{bull} صاعد" in text, text
        assert f"{bear} هابط" in text, text
        assert f"{neutral} محايد" in text, text
        assert bull > bear, (states, conf)  # the message can never imply a tie


def test_historical_two_of_four_alerts_were_a_real_majority():
    """Retrospective check on the shape that actually shipped: the LTCUSDT
    reading behind the real '2/4 صاعد' messages (macd+vwap bullish, ichimoku
    bearish, adx neutral, per indicators/history/*.json)."""
    r = result_for(("bullish", "neutral", "bearish", "bullish"))
    conf = r["confluence"]
    assert (conf["bullish_count"], conf["bearish_count"]) == (2, 1)
    assert conf["direction"] == "bullish"
    assert r["trade_zone"]["setup"] == "long"


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        print(f"{fn.__name__} ...")
        fn()
        print("  PASS")
    print("\nAll checks passed.")
