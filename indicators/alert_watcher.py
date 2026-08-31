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

State: indicators/alert_state.json, one entry per "SYMBOL|combo_key":
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
"""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from indicators.config import load_config  # noqa: E402
from indicators.engine import compute_symbol  # noqa: E402
from indicators.sources import DataSource, MarketHours, get_source  # noqa: E402

STATE_PATH = Path(__file__).resolve().parent / "alert_state.json"
PENDING_ALERTS_PATH = Path(__file__).resolve().parent / "pending_alerts.json"

COMBOS: dict[str, dict[str, str]] = {
    "سريعة":  {"trend": "1h", "entry": "15m", "levels": "1d"},
    "متوسطة": {"trend": "4h", "entry": "1h", "levels": "1d"},
    "بطيئة":  {"trend": "1d", "entry": "4h", "levels": "1w"},
}

CONFIRMATIONS_REQUIRED = 2       # consecutive checks a new setup must survive before alerting
ALERT_ON_BEARISH_NO_SHORT = False  # per the brief's default -- flip to True to also alert on that transition


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


def build_entry_message(symbol: str, combo_name: str, combo_tf: dict, result: dict) -> str:
    conf = result["confluence"]
    tz = result["trade_zone"]
    total = conf["total_indicators"]
    bull = conf["bullish_count"]
    entry_low, entry_high = tz["entry_zone"]
    return (
        f"🟢 {symbol} — فرصة شراء جديدة [نوع: {combo_name} | اتجاه {combo_tf['trend']} + دخول {combo_tf['entry']}]\n"
        f"توافق {bull}/{total} صاعد\n"
        f"دخول {format_price(entry_low)}-{format_price(entry_high)} / "
        f"وقف {format_price(tz['stop_loss'])} / هدف {format_price(tz['target'])}"
    )


def build_exit_message(symbol: str, combo_name: str, result: dict) -> str:
    conf = result["confluence"]
    total = conf["total_indicators"]
    bull, bear = conf["bullish_count"], conf["bearish_count"]
    direction = conf["direction"]
    if direction == "neutral":
        reason = f"فقد التوافق (بقى {bull}/{total})"
    elif direction == "bearish":
        reason = f"انعكس الاتجاه لهابط ({bear}/{total})"
    else:
        reason = f"تغيّر التوافق ({bull}/{total} صاعد)"
    return f"⚪ {symbol} — انتهت فرصة الشراء [نوع: {combo_name}]\nالسبب: {reason}"


def process_one(symbol: str, combo_name: str, combo_tf: dict, result: dict, state: dict,
                 checked_at: str) -> dict | None:
    """Updates `state` in place for this (symbol, combo) key and returns an
    alert dict if (and only if) a confirmed transition just fired."""
    key = f"{symbol}|{combo_name}"
    new_setup = result["trade_zone"]["setup"]
    prev = state.get(key)

    if prev is None:
        # First time ever seeing this key: establish a baseline, no alert --
        # there is nothing to have "changed" from yet.
        state[key] = {"confirmed_setup": new_setup, "pending_setup": None,
                      "pending_count": 0, "last_checked_at": checked_at}
        return None

    confirmed = prev["confirmed_setup"]
    pending = prev.get("pending_setup")
    pending_count = prev.get("pending_count", 0)
    alert = None

    if new_setup == confirmed:
        pending, pending_count = None, 0  # reverted back to the established state -- noise, drop any candidate
    elif new_setup == pending:
        pending_count += 1
        if pending_count >= CONFIRMATIONS_REQUIRED:
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

    save_state(state)
    PENDING_ALERTS_PATH.write_text(json.dumps(alerts, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"\n{len(alerts)} alert(s) this run.")
    return alerts


def main() -> None:
    alerts = run_check(log=print)
    for a in alerts:
        print("-" * 60)
        print(a["text"])
    print(f"\nState saved -> {STATE_PATH}")
    print(f"Alerts written -> {PENDING_ALERTS_PATH}")


if __name__ == "__main__":
    main()
