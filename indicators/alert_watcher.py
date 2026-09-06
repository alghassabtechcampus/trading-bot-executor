"""Change-only alert watcher -- COMPLETELY SEPARATE from the "Daily Report"
n8n workflow (not touched, not read, not referenced). Runs the SAME
engine.compute_symbol() used by the dashboard, across 3 timeframe
combinations x 9 symbols (27 computations), and emits a Telegram-ready
alert only when a symbol+combo's trade-zone setup actually TRANSITIONS
into or out of "long" -- never on a periodic heartbeat, never on a raw
single-check flip (see the debounce rule below).

This script computes and formats alerts. It does NOT send them and does
NOT place any trade -- see the accompanying report for how an n8n Schedule
Trigger + Execute Command + Telegram node is meant to consume its output
(indicators/pending_alerts.json), matching this project's existing pattern
of n8n orchestrating a plain Python script (signal_runner.py, dashboard's
run_dashboard.py) rather than holding logic itself.

Files (all under INDICATORS_DATA_DIR, which defaults to this directory --
point it at a mounted volume in Docker so they survive a redeploy):

  alert_log.jsonl     - APPEND-ONLY permanent record. One JSON object per
                        alert ever fired, never rewritten or truncated.
                        Every field a later performance review needs is a
                        real number in its own key (entry_low, entry_high,
                        stop, target, ...), so nothing has to be recovered
                        by regexing the Arabic message text.
  pending_alerts.json - THIS RUN's alerts only, rewritten each run. This is
                        deliberately NOT cumulative: the n8n Telegram node
                        reads it and sends whatever it finds, so making it
                        append-only would re-send the whole history every
                        cycle. alert_log.jsonl is the archive; this file
                        stays a one-shot outbox.

State: alert_state.json, one entry per "SYMBOL|combo_key":
  confirmed_setup   - the last setup value an alert was actually fired for
                       (or established as the no-alert baseline on first run)
  pending_setup     - a candidate new setup seen but not yet confirmed
  pending_count     - how many consecutive checks have seen pending_setup
  last_checked_at   - ISO timestamp of the most recent check

Debounce: a candidate setup must be observed on >=2 CONSECUTIVE checks
before it can fire an alert. A value that reverts before reaching 2 resets
pending_count to 0 -- exactly the "flips within one cycle is noise" rule.

Alert-worthy transitions (confirmed_setup before -> after, on confirmation):
  anything -> "long"           : ENTRY alert (new long opportunity)
  "long"   -> anything else    : EXIT alert (long opportunity ended)
  "none" <-> "bearish_no_short": tracked in state, no alert by default
    (see ALERT_ON_BEARISH_NO_SHORT below -- flip it if that's wanted later)
  "long" <-> "unreachable"     : NOT a transition at all. All three are one
  "long" <-> "poor_rr"           single state to this module, and an ENTRY
    alert is only ever sent while the raw setup is "long" -- see
    ALERT_EQUIVALENT_SETUPS.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from indicators.config import load_config  # noqa: E402
from indicators.engine import compute_symbol  # noqa: E402
from indicators.sources import DataSource, MarketHours, get_source  # noqa: E402

# Runtime files live in DATA_DIR, not necessarily next to the code: in Docker
# the image is replaced wholesale on every deploy, so anything written inside
# it is lost. Pointing INDICATORS_DATA_DIR at a mounted volume keeps the state
# machine's memory -- and the alert archive -- across redeploys.
DATA_DIR = Path(os.getenv("INDICATORS_DATA_DIR") or Path(__file__).resolve().parent)
STATE_PATH = DATA_DIR / "alert_state.json"
PENDING_ALERTS_PATH = DATA_DIR / "pending_alerts.json"
ALERT_LOG_PATH = DATA_DIR / "alert_log.jsonl"

COMBOS: dict[str, dict[str, str]] = {
    "سريعة":  {"trend": "1h", "entry": "15m", "levels": "1d"},
    "متوسطة": {"trend": "4h", "entry": "1h", "levels": "1d"},
    "بطيئة":  {"trend": "1d", "entry": "4h", "levels": "1w"},
}

CONFIRMATIONS_REQUIRED = 2       # consecutive checks a new setup must survive before alerting
ALERT_ON_BEARISH_NO_SHORT = False  # per the brief's default -- flip to True to also alert on that transition

# "unreachable" and "poor_rr" are the SAME bullish opportunity as "long":
# same direction, same support, same entry zone. Each is "long, but not worth
# announcing right now" for a different reason -- price is too far above the
# zone, or the nearest resistance is too close for the reward to justify the
# stop. trade_zone keeps all three apart so the DASHBOARD can explain which
# it is; the alert state machine deliberately does not.
#
# Why they are collapsed rather than treated as their own states: what
# separates them from "long" is a threshold crossing, not a change of view.
# Price walks back and forth across the reachability line several times a
# day, and R:R crosses the minimum whenever ATR breathes or a level is
# re-detected -- while the trade being described is identical throughout.
# Treating them as distinct states made each wobble a confirmed EXIT
# followed by a fresh ENTRY.
#
# In particular they are NOT mapped to "none". Mapping them there would fire
# an EXIT the moment an open trade's hypothetical re-entry scored worse,
# telling the reader an opportunity ended when their stop and target had not
# moved at all. A real loss of the bullish read still exits, because that is
# a genuine "none"/"bearish_no_short" transition and is untouched here.
ALERT_EQUIVALENT_SETUPS = {"unreachable": "long", "poor_rr": "long"}


def alert_state_of(setup: str) -> str:
    """The setup as the ALERT state machine sees it. Only alerting collapses
    these -- the dashboard, the log record and the message builders all keep
    reading the raw trade_zone["setup"]."""
    return ALERT_EQUIVALENT_SETUPS.get(setup, setup)


class _RunCache(DataSource):
    """Thin per-run memoizing wrapper: the 3 combos share timeframe values
    (e.g. levels=1d appears in two of them), so without this a 9-symbol x
    3-combo run would re-fetch the same (symbol, timeframe) OHLCV up to 3x.
    Delegates everything to the real source; caches only within one process
    run (a fresh instance is created each invocation, no stale carryover)."""

    def __init__(self, inner: DataSource) -> None:
        self._inner = inner
        self._cache: dict[tuple[str, str], "pd.DataFrame"] = {}

    def get_ohlcv(self, symbol, timeframe, start=None, end=None):
        key = (symbol, timeframe)
        if key not in self._cache:
            self._cache[key] = self._inner.get_ohlcv(symbol, timeframe, start, end)
        return self._cache[key]

    def get_available_symbols(self) -> tuple[str, ...]:
        return self._inner.get_available_symbols()

    def market_hours(self) -> MarketHours:
        return self._inner.market_hours()


def format_price(x: float) -> str:
    abs_x = abs(x)
    decimals = 2 if abs_x >= 1000 else (4 if abs_x >= 1 else 6)
    return f"{x:,.{decimals}f}"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    with STATE_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(state: dict) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)


def format_split(conf: dict) -> str:
    """Full bullish/bearish/neutral breakdown, never just the winning count.
    "2/4 صاعد" was ambiguous -- it reads as a 2-2 tie, but a tie can never
    reach an entry alert at all (tie -> confluence "neutral" -> trade_zone
    setup "none"), so it always meant 2 bullish vs at most 1 bearish, the
    rest neutral. Spelling out all three counts removes the reading that
    was never true."""
    bull, bear = conf["bullish_count"], conf["bearish_count"]
    total = conf["total_indicators"]
    return f"{bull} صاعد / {bear} هابط / {total - bull - bear} محايد (من {total})"


def append_alert_log(records: list[dict]) -> None:
    """Appends one JSON line per alert. Opened in "a" mode so a run can only
    ever add to the file -- no code path here rewrites or truncates it. JSONL
    (not a JSON array) precisely so appending never requires reading,
    re-parsing, or re-serialising what is already there: a half-written tail
    costs one line, not the archive."""
    if not records:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG_PATH.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_log_record(symbol: str, combo_name: str, combo_tf: dict, result: dict,
                     alert: dict, sent_at: str) -> dict:
    """Flattens one alert into an archive row. Every price is a number in its
    own field; `text` is kept only so the exact message a user received stays
    auditable -- never as the source the numbers are read back from."""
    tz = result["trade_zone"]
    conf = result["confluence"]
    entry_low, entry_high = (tz.get("entry_zone") or [None, None])
    return {
        "sent_at": sent_at,
        "symbol": symbol,
        "combo": combo_name,
        "alert_type": alert["type"],
        "setup": tz["setup"],
        "trend_timeframe": combo_tf["trend"],
        "entry_timeframe": combo_tf["entry"],
        "levels_timeframe": combo_tf["levels"],
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": tz.get("stop_loss"),
        "target": tz.get("target"),
        "rr_ratio": tz.get("rr_ratio"),
        "reference_level": tz.get("reference_level"),
        "entry_distance_pct": tz.get("entry_distance_pct"),
        "current_price": result["current_price"],
        "as_of": result["as_of"],
        "direction": conf["direction"],
        "bullish_count": conf["bullish_count"],
        "bearish_count": conf["bearish_count"],
        "total_indicators": conf["total_indicators"],
        "text": alert["text"],
    }


def build_trail_hint(tz: dict) -> str:
    """One advisory line for the entry message. Purely informational: nothing
    tracks whether the reader acts on it, and no alert is ever sent about it
    later. `arm` is the price at which the suggestion becomes relevant; `then`
    is what the trailing rule would give AT that moment (highest price so far
    would be `arm`, minus trail_atr_mult x ATR) -- a concrete number beats a
    formula the reader has to evaluate. Once price runs past `arm` the level
    keeps rising with the high, which is why the wording says to trail off the
    highest price seen and not to park the stop at `then` forever."""
    entry_low, entry_high = tz["entry_zone"]
    mid = (entry_low + entry_high) / 2
    atr = tz["atr_value"]
    arm = mid + tz["trail_arm_atr_mult"] * atr
    then = arm - tz["trail_atr_mult"] * atr
    return (f"💡 لو وصل {format_price(arm)}: فكّر تحرّك الوقف يدويًا لـ"
            f"(أعلى سعر وصله − {format_price(tz['trail_atr_mult'] * atr)}) ≈ {format_price(then)}")


def build_entry_message(symbol: str, combo_name: str, combo_tf: dict, result: dict) -> str:
    """Only ever called for result["trade_zone"]["setup"] == "long" -- the
    exact condition the dashboard uses to show a suggested entry zone.
    process_one() enforces that; see tests/test_alert_consistency.py."""
    conf = result["confluence"]
    tz = result["trade_zone"]
    entry_low, entry_high = tz["entry_zone"]
    return (
        f"🟢 {symbol} — فرصة شراء جديدة [نوع: {combo_name} | اتجاه {combo_tf['trend']} + دخول {combo_tf['entry']}]\n"
        f"التوافق: {format_split(conf)}\n"
        f"دخول {format_price(entry_low)}-{format_price(entry_high)} / "
        f"وقف {format_price(tz['stop_loss'])} / هدف {format_price(tz['target'])}\n"
        f"{build_trail_hint(tz)}"
    )


def build_exit_message(symbol: str, combo_name: str, result: dict) -> str:
    conf = result["confluence"]
    direction = conf["direction"]
    split = format_split(conf)
    if direction == "neutral":
        reason = f"فقد التوافق — {split}"
    elif direction == "bearish":
        reason = f"انعكس الاتجاه لهابط — {split}"
    else:
        reason = f"تغيّر التوافق — {split}"
    return f"⚪ {symbol} — انتهت فرصة الشراء [نوع: {combo_name}]\nالسبب: {reason}"


def process_one(symbol: str, combo_name: str, combo_tf: dict, result: dict, state: dict,
                 checked_at: str) -> dict | None:
    """Updates `state` in place for this (symbol, combo) key and returns an
    alert dict if (and only if) a confirmed transition just fired.

    Runs on alert_state_of(setup), not on the raw setup, so "long" and
    "unreachable" are one indistinguishable state here and no flip between
    them can produce an alert in either direction."""
    key = f"{symbol}|{combo_name}"
    raw_setup = result["trade_zone"]["setup"]
    new_setup = alert_state_of(raw_setup)
    prev = state.get(key)

    if prev is None:
        # First time ever seeing this key: establish a baseline, no alert --
        # there is nothing to have "changed" from yet.
        state[key] = {"confirmed_setup": new_setup, "pending_setup": None,
                      "pending_count": 0, "last_checked_at": checked_at}
        return None

    # Mapped on read as well as on write: a state file written before this
    # rule existed can still hold a literal "unreachable", and it must not
    # read back as a state change on the first check after the upgrade.
    confirmed = alert_state_of(prev["confirmed_setup"])
    pending = prev.get("pending_setup")
    pending = alert_state_of(pending) if pending is not None else None
    pending_count = prev.get("pending_count", 0)
    alert = None

    if new_setup == confirmed:
        pending, pending_count = None, 0  # reverted back to the established state -- noise, drop any candidate
    elif new_setup == pending:
        pending_count += 1
        # Confirming INTO "long" requires the RAW setup to be an actionable
        # long, not merely a member of the collapsed group: "unreachable"
        # carries no stop_loss or target to put in the message, and "poor_rr"
        # carries numbers we have decided are not worth acting on. Such a
        # candidate stays pending -- pending_count keeps climbing, this is not
        # a revert -- so the entry alert fires on the first check where the
        # setup is genuinely actionable, instead of the state confirming
        # silently and swallowing that entry for good.
        announceable = new_setup != "long" or raw_setup == "long"
        if pending_count >= CONFIRMATIONS_REQUIRED and announceable:
            if new_setup == "long":
                text = build_entry_message(symbol, combo_name, combo_tf, result)
                alert = {"symbol": symbol, "combo": combo_name, "type": "entry", "text": text}
            elif confirmed == "long":  # long -> (none | bearish_no_short)
                text = build_exit_message(symbol, combo_name, result)
                alert = {"symbol": symbol, "combo": combo_name, "type": "exit", "text": text}
            elif ALERT_ON_BEARISH_NO_SHORT:  # none <-> bearish_no_short, opt-in only
                alert = {"symbol": symbol, "combo": combo_name, "type": "info",
                          "text": f"ℹ️ {symbol} [نوع: {combo_name}] تغيّر إلى {new_setup}"}
            confirmed = new_setup
            pending, pending_count = None, 0
    else:
        pending, pending_count = new_setup, 1

    state[key] = {"confirmed_setup": confirmed, "pending_setup": pending,
                  "pending_count": pending_count, "last_checked_at": checked_at}
    return alert


def run_check(log=None) -> list[dict]:
    """Runs one full check (27 computations), updates alert_state.json and
    pending_alerts.json, and returns the alerts fired this run. `log`, if
    given, is called with each progress line (CLI passes print; the web
    endpoint can pass a no-op or a logger) -- the core logic is identical
    either way, so the HTTP route and the standalone script never diverge."""
    log = log or (lambda _line: None)

    base_config = load_config()
    source = _RunCache(get_source(base_config.market))
    symbols = source.get_available_symbols()
    state = load_state()
    checked_at = datetime.now(timezone.utc).isoformat()

    log(f"Alert watcher run at {checked_at}")
    log(f"Combos: {list(COMBOS.keys())}  Symbols: {symbols}")

    alerts: list[dict] = []
    log_records: list[dict] = []
    for combo_name, combo_tf in COMBOS.items():
        run_config = dataclasses.replace(base_config, trend_timeframe=combo_tf["trend"],
                                          entry_timeframe=combo_tf["entry"], levels_timeframe=combo_tf["levels"])
        for symbol in symbols:
            try:
                result = compute_symbol(symbol, run_config, source)
            except Exception as exc:
                log(f"  {symbol} [{combo_name}]: FAILED ({exc})")
                continue

            setup = result["trade_zone"]["setup"]
            alert = process_one(symbol, combo_name, combo_tf, result, state, checked_at)
            note = f" -> ALERT ({alert['type']})" if alert else ""
            log(f"  {symbol:<10} [{combo_name:<6}] setup={setup:<18}{note}")
            if alert:
                alerts.append(alert)
                log_records.append(
                    build_log_record(symbol, combo_name, combo_tf, result, alert, checked_at))

    save_state(state)
    append_alert_log(log_records)          # permanent archive -- only ever grows
    PENDING_ALERTS_PATH.write_text(json.dumps(alerts, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"\n{len(alerts)} alert(s) this run.")
    if log_records:
        log(f"Appended {len(log_records)} record(s) -> {ALERT_LOG_PATH}")
    return alerts


def main() -> None:
    alerts = run_check(log=print)
    for a in alerts:
        print("-" * 60)
        print(a["text"])
    archived = sum(1 for _ in ALERT_LOG_PATH.open(encoding="utf-8")) if ALERT_LOG_PATH.exists() else 0
    print(f"\nState saved       -> {STATE_PATH}")
    print(f"This-run outbox   -> {PENDING_ALERTS_PATH}")
    print(f"Permanent archive -> {ALERT_LOG_PATH} ({archived} record(s) total)")


if __name__ == "__main__":
    main()
