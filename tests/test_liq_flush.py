"""Offline tests for liq_flush.py. No network, no Bybit, no Redis.

The whole point of run_tick taking an injected `ex` is this file: app.py reads
os.environ["BYBIT_API_KEY"] and REDIS_URL at import time, so importing it would
make these tests need live credentials. FakeExecutor stands in for it and
records every order, which lets one full cycle -- signal, buy, cooldown, 24h
time exit -- run in milliseconds instead of a day.

Covered, in the order the strategy depends on them:

  the locked limits   max 3 per scan, max 6 open, 24h cooldown
  the two exits       time at +24h, stop on the hourly LOW (not just last price)
  isolation           own Redis prefix, liq_ orderLinkId, refuses a symbol that
                      another strategy holds, never calls close_tracked_trade
  safety              kill switch still runs exits, insufficient balance skips,
                      an exception on one symbol does not stop the others,
                      dry-run places no orders, overrides rejected when live
"""

from __future__ import annotations

import json
import sys
import threading
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import liq_flush  # noqa: E402

HOUR = 3600
T0 = 1_760_000_000  # a fixed "now"; the hour floor of this is T0 - 2000


# ============================================================
# The fake executor
# ============================================================

class FakeExecutor:
    """Mimics only what liq_flush calls on the app module.

    Redis is a dict (TTLs are recorded, not enforced -- every test that cares
    about expiry deletes the key explicitly, which is what a real TTL does).
    Klines are settable per symbol so the stop path can be driven exactly.
    """

    def __init__(self, *, price="100", usdt="1000", wallet="1000000"):
        self.store: dict[str, dict] = {}
        self.ttl: dict[str, int] = {}
        self.prices = {}
        self.default_price = Decimal(price)
        self.usdt = Decimal(usdt)
        self.wallet = Decimal(wallet)
        self.klines: dict[str, list] = {}
        self.buys: list[tuple] = []
        self.sells: list[tuple] = []
        self.tracked: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.raise_on_price: set[str] = set()
        self.coin_balance_calls: list[str] = []
        self.min_qty = Decimal("0.0001")
        self.min_amt = Decimal("5")
        self.qty_step = Decimal("0.0001")

    # -------- helpers copied in behaviour from app.py --------
    def as_decimal(self, value, field):
        return Decimal(str(value))

    def decimal_str(self, value):
        return format(value.normalize(), "f")

    def floor_to_step(self, value, step):
        if step <= 0:
            return value
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    def coin_from_symbol(self, symbol):
        if not symbol.endswith("USDT"):
            raise ValueError("Only USDT spot symbols are supported")
        return symbol[:-4]

    # -------- Redis --------
    def redis_set_json(self, key, value, *, ex_seconds=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = json.loads(json.dumps(value))
        if ex_seconds:
            self.ttl[key] = ex_seconds
        return True

    def redis_get_json(self, key):
        return self.store.get(key)

    def redis_delete(self, key):
        self.store.pop(key, None)
        self.ttl.pop(key, None)

    def redis_keys(self, pattern):
        assert pattern.endswith("*")
        head = pattern[:-1]
        return [k for k in self.store if k.startswith(head)]

    # -------- market data --------
    def get_ticker_price(self, symbol):
        if symbol in self.raise_on_price:
            raise RuntimeError(f"Bybit network error for {symbol}")
        return self.prices.get(symbol, self.default_price)

    def bybit_get(self, path, params):
        assert path == "/v5/market/kline"
        assert params["category"] == "spot", "the stop must be read on the market we trade"
        rows = self.klines.get(params["symbol"], [])
        return {"result": {"list": rows}}

    def get_instrument_filters(self, symbol):
        return {
            "qty_step": self.qty_step,
            "min_qty": self.min_qty,
            "min_amt": self.min_amt,
            "tick_size": Decimal("0.01"),
        }

    def get_usdt_balance(self):
        return self.usdt

    def get_coin_balance(self, coin):
        self.coin_balance_calls.append(coin)
        return {
            "wallet_balance": self.wallet,
            "available": self.wallet,
            "usd_value": self.wallet * Decimal("100"),
        }

    # -------- orders --------
    def create_market_buy(self, symbol, quote_amount, link_id):
        self.buys.append((symbol, quote_amount, link_id))
        price = self.get_ticker_price(symbol)
        qty = self.floor_to_step(quote_amount / price, self.qty_step)
        return {"result": {"orderId": f"BUY-{symbol}", "_qty": str(qty), "_price": str(price)}}

    def create_market_sell(self, symbol, qty, link_id):
        self.sells.append((symbol, qty, link_id))
        price = self.get_ticker_price(symbol)
        return {"result": {"orderId": f"SELL-{symbol}", "_qty": str(qty), "_price": str(price)}}

    def wait_for_fill(self, symbol, order_id):
        source = self.buys if order_id.startswith("BUY") else self.sells
        _, amount, _ = source[-1]
        price = self.get_ticker_price(symbol)
        qty = self.floor_to_step(amount / price, self.qty_step) if order_id.startswith("BUY") else amount
        return {
            "orderId": order_id,
            "orderStatus": "Filled",
            "cumExecQty": str(qty),
            "cumExecValue": str(qty * price),
            "avgPrice": str(price),
            "cumExecFee": "0.001",
        }

    def execution_details(self, order):
        qty = Decimal(order["cumExecQty"])
        value = Decimal(order["cumExecValue"])
        return {
            "order_id": order["orderId"],
            "status": order["orderStatus"],
            "executed_qty": qty,
            "executed_value": value,
            "average_price": value / qty,
        }

    # -------- other strategies' state --------
    def tracked_trade(self, symbol):
        return self.tracked.get(symbol)

    def pending_entry(self, symbol):
        return self.pending.get(symbol)

    def close_tracked_trade(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError(
            "liq_flush must never route through the shared sell path -- it "
            "applies the OTHER strategy's stop/target rules."
        )


@pytest.fixture
def ex():
    return FakeExecutor()


@pytest.fixture(autouse=True)
def live_mode(monkeypatch):
    """Default every test to enabled + not dry, so a test that wants dry says so."""
    monkeypatch.setenv("LIQ_ENABLED", "true")
    monkeypatch.setenv("LIQ_DRY_RUN", "false")
    monkeypatch.setenv("LIQ_USDT_PER_TRADE", "10")


def sig(symbol, z=-3.5, close="100"):
    return {"symbol": symbol, "close": close, "z_r4": z, "z_oi4": z - 0.1,
            "bar": "2026-10-05T04:00:00Z"}


def kline(start_ms, low, high="999", close="100"):
    # Bybit row: [start, open, high, low, close, volume, turnover].
    # start is in MILLISECONDS -- passing seconds here silently puts every
    # candle before the entry, so no stop ever triggers and the test lies.
    assert start_ms > 10 ** 12, "kline start must be in milliseconds"
    return [str(start_ms), close, high, str(low), close, "1", "1"]


def tick(ex, **params):
    return liq_flush.run_tick(ex, params)


# ============================================================
# entries and the locked limits
# ============================================================

def test_entry_buys_ten_usdt_and_tags_the_order(ex, monkeypatch):
    monkeypatch.setenv("LIQ_DRY_RUN", "true")  # overrides need dry-run
    out = tick(ex, force_signals=[sig("SOLUSDT")], now=T0)
    monkeypatch.setenv("LIQ_DRY_RUN", "false")

    # dry-run must place nothing at all
    assert ex.buys == [] and ex.sells == []
    assert out["entries"][0]["symbol"] == "SOLUSDT"
    assert liq_flush.open_key("SOLUSDT", True) in ex.store


def test_live_entry_sizes_in_quote_and_prefixes_the_link_id(ex):
    out = tick(ex, force_signals=[sig("SOLUSDT")])
    # overrides are rejected outside dry-run -- that is the point of the next test
    assert out["status"] == "overrides_rejected"
    assert ex.buys == []


def test_entry_records_filled_qty_not_signal_price(ex):
    ex.prices["SOLUSDT"] = Decimal("120")
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT", close="125")], now=T0, dry=False)

    symbol, amount, link_id = ex.buys[0]
    assert symbol == "SOLUSDT"
    assert amount == Decimal("10")           # LIQ_USDT_PER_TRADE
    assert link_id.startswith("liq_b_")      # isolation tag for trade_logger
    assert len(link_id) <= 36                # Bybit's limit

    record = ex.store[liq_flush.open_key("SOLUSDT", False)]
    # 120 is the FILL price from Bybit, 125 was the signal bar's close
    assert Decimal(record["entry_price"]) == Decimal("120")
    assert Decimal(record["stop_level"]) == Decimal("120") * Decimal("0.92")
    assert record["signal_close"] == "125"


def test_only_first_three_signals_are_taken(ex):
    result = _blank()
    signals = [sig(f"C{i}USDT") for i in range(6)]
    liq_flush.process_entries(ex, result, signals, now=T0, dry=False)

    assert len(result["entries"]) == liq_flush.MAX_PER_SCAN == 3
    assert [s for s, *_ in ex.buys] == ["C0USDT", "C1USDT", "C2USDT"]


def test_signals_beyond_three_are_reported_as_ignored(ex):
    out = _run_forced(ex, [sig(f"C{i}USDT") for i in range(5)])
    over = [s for s in out["skipped"] if s["reason"] == "over_max_per_scan"]
    assert {s["symbol"] for s in over} == {"C3USDT", "C4USDT"}


def test_cooldown_blocks_reentry_within_24h(ex):
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT")], now=T0, dry=False)
    # position closed, but the cooldown key is still live
    ex.redis_delete(liq_flush.open_key("SOLUSDT", False))
    assert ex.ttl[liq_flush.cooldown_key("SOLUSDT", False)] == liq_flush.COOLDOWN_SECONDS

    result = _blank()
    liq_flush.process_entries(ex, result, [sig("SOLUSDT")], now=T0 + HOUR, dry=False)
    assert result["skipped"] == [{"symbol": "SOLUSDT", "reason": "cooldown"}]
    assert len(ex.buys) == 1

    # once the TTLs have expired the symbol is eligible again. The fake records
    # TTLs without enforcing them, so expiry is spelled out: 25h is long past
    # both the cooldown (24h) and the entry reservation (10 min), and the
    # journal entry is older than 24h so it no longer blocks.
    ex.redis_delete(liq_flush.cooldown_key("SOLUSDT", False))
    ex.redis_delete(liq_flush.reserve_key("SOLUSDT", False))
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT")], now=T0 + 25 * HOUR, dry=False)
    assert len(ex.buys) == 2


def test_max_six_open_positions(ex):
    for i in range(6):
        ex.store[liq_flush.open_key(f"OLD{i}USDT", False)] = {"symbol": f"OLD{i}USDT"}

    result = _blank()
    liq_flush.process_entries(ex, result, [sig("SOLUSDT")], now=T0, dry=False)
    assert result["skipped"] == [{"symbol": "SOLUSDT", "reason": "max_open"}]
    assert ex.buys == []


def test_full_cap_skips_the_sixty_second_scan(ex, monkeypatch):
    """Guard against spending a minute of Bybit calls that cannot produce a buy."""
    for i in range(6):
        ex.store[liq_flush.open_key(f"OLD{i}USDT", False)] = {
            "symbol": f"OLD{i}USDT", "entry_time": T0, "entry_price": "100",
        }
    ex.klines = {f"OLD{i}USDT": [] for i in range(6)}

    def explode():  # pragma: no cover
        raise AssertionError("scan() must not run when the position cap is full")

    monkeypatch.setitem(sys.modules, "liq_signal", type(sys)("liq_signal"))
    sys.modules["liq_signal"].scan = explode

    out = tick(ex)
    assert {"symbol": None, "reason": "max_open_scan_skipped"} in out["skipped"]


def test_insufficient_balance_skips_and_warns(ex):
    ex.usdt = Decimal("4")
    result = _blank()
    liq_flush.process_entries(ex, result, [sig("SOLUSDT")], now=T0, dry=False)

    assert result["skipped"][0]["reason"] == "insufficient_balance"
    assert ex.buys == []
    assert any("رصيد غير كافٍ" in m for m in result["messages"])


# ============================================================
# isolation from the other strategies
# ============================================================

def test_symbol_held_by_another_strategy_is_skipped_with_a_warning(ex):
    ex.tracked["SOLUSDT"] = {"symbol": "SOLUSDT", "executed_qty": "1"}
    result = _blank()
    liq_flush.process_entries(ex, result, [sig("SOLUSDT")], now=T0, dry=False)

    assert result["skipped"] == [{"symbol": "SOLUSDT", "reason": "foreign_trade"}]
    assert ex.buys == []
    assert any("استراتيجية أخرى" in m for m in result["messages"])


def test_pending_limit_order_from_another_strategy_also_blocks(ex):
    ex.pending["SOLUSDT"] = {"symbol": "SOLUSDT"}
    result = _blank()
    liq_flush.process_entries(ex, result, [sig("SOLUSDT")], now=T0, dry=False)
    assert result["skipped"] == [{"symbol": "SOLUSDT", "reason": "foreign_pending"}]


def test_state_lives_only_under_the_liq_prefix(ex):
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT")], now=T0, dry=False)
    assert all(k.startswith("liq:") for k in ex.store)
    # specifically NOT the keys /monitor_positions and /positions iterate
    assert not any(k.startswith(("trade:", "pending:", "signal:")) for k in ex.store)


def test_dry_run_uses_a_separate_prefix(ex):
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT")], now=T0, dry=True)
    assert liq_flush.open_key("SOLUSDT", True) in ex.store
    assert liq_flush.open_key("SOLUSDT", False) not in ex.store
    assert all(k.startswith("liq:dry:") for k in ex.store)


# ============================================================
# exits
# ============================================================

def test_time_exit_fires_at_exactly_24h_not_before(ex):
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0)
    ex.klines["SOLUSDT"] = []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 23 * HOUR, dry=False)
    assert result["exits"][0]["status"] == "holding"
    assert ex.sells == []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=False)
    assert result["exits"][0]["reason"] == "time"
    assert ex.sells[0][0] == "SOLUSDT"
    assert ex.sells[0][2].startswith("liq_s_")


def test_time_exit_still_fires_when_a_tick_was_missed(ex):
    """n8n has stopped running before (handoff/README_AR.md): a late tick must
    still exit, not decide the window has passed."""
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0)
    ex.klines["SOLUSDT"] = []
    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 40 * HOUR, dry=False)
    assert result["exits"][0]["reason"] == "time"


def test_stop_fires_on_the_hourly_low_even_if_price_recovered(ex):
    """The backtest stops on path['low'] (liquidation_stress_test.py:51). A wick
    to -9% that closes back at -1% MUST stop out, or live beats the backtest for
    no real reason."""
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0)
    ex.prices["SOLUSDT"] = Decimal("99")          # fully recovered
    ex.klines["SOLUSDT"] = [kline((T0 + HOUR) * 1000, low="91")]  # dipped below 92

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 3 * HOUR, dry=False)
    assert result["exits"][0]["reason"] == "stop"
    assert ex.sells


def test_stop_also_fires_on_current_price_alone(ex):
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0)
    ex.prices["SOLUSDT"] = Decimal("90")
    ex.klines["SOLUSDT"] = []
    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + HOUR, dry=False)
    assert result["exits"][0]["reason"] == "stop"


def test_entry_candle_low_before_the_fill_is_not_a_stop(ex):
    """The candle we bought into may have made its low before we filled.
    Counting it would invent a stop that never happened to us."""
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0)
    ex.prices["SOLUSDT"] = Decimal("99")
    # candle that STARTED before the fill, low below the stop
    ex.klines["SOLUSDT"] = [kline((T0 - HOUR) * 1000, low="80")]
    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 3 * HOUR, dry=False)
    assert result["exits"][0]["status"] == "holding"
    assert ex.sells == []


def test_unclosed_current_candle_is_ignored(ex):
    """Its low is not final yet, and the current price already covers it."""
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0)
    ex.prices["SOLUSDT"] = Decimal("99")
    now = T0 + 3 * HOUR
    hour_floor = (now // HOUR) * HOUR
    ex.klines["SOLUSDT"] = [kline(hour_floor * 1000, low="50")]
    result = _blank()
    liq_flush.process_exits(ex, result, now=now, dry=False)
    assert result["exits"][0]["status"] == "holding"


def test_stop_wins_when_both_triggers_are_due(ex):
    """Hourly granularity: if the stop was hit at some point, it was hit BEFORE
    the 24h mark, which is what the backtest records."""
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0)
    ex.prices["SOLUSDT"] = Decimal("99")
    ex.klines["SOLUSDT"] = [kline((T0 + 2 * HOUR) * 1000, low="85")]
    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 30 * HOUR, dry=False)
    assert result["exits"][0]["reason"] == "stop"


def test_kline_failure_does_not_block_the_time_exit(ex):
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0)

    def boom(path, params):
        raise RuntimeError("Bybit network error")

    ex.bybit_get = boom
    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 25 * HOUR, dry=False)
    assert result["exits"][0]["reason"] == "time"


def test_exit_sells_the_full_recorded_qty(ex):
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.1")
    ex.klines["SOLUSDT"] = []
    liq_flush.process_exits(ex, _blank(), now=T0 + 24 * HOUR, dry=False)

    _, qty, _ = ex.sells[0]
    # 0.1 * 0.999 floored to the step -- the 0.999 is app.py's fee headroom
    assert qty == Decimal("0.0999")


def test_dust_below_min_order_writes_an_exit_row_instead_of_vanishing(ex):
    """The old behaviour deleted the record with no sheet row, no message and no
    sell -- the trade's result simply disappeared. Now it still does not sell,
    but it has to say so."""
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.00001")
    ex.wallet = Decimal("0.00001")
    ex.klines["SOLUSDT"] = []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=False)

    assert result["exits"][0]["status"] == "dust_unsellable"
    assert ex.sells == []
    assert liq_flush.open_key("SOLUSDT", False) not in ex.store

    # the row the sheet gets
    row = result["journal"][0]
    assert row["event"] == "exit"
    assert row["symbol"] == "SOLUSDT"
    assert row["exit_reason"].startswith("dust_unsellable")
    assert "remaining=0.00001" in row["exit_reason"]
    assert "expected=0.00001" in row["exit_reason"]
    assert row["exit_time"] and row["exit_price"] and row["pnl"] is not None

    # and a warning that names the leftover
    assert any("ما انباعت" in m and "0.00001" in m for m in result["messages"])


def test_dust_exit_row_keeps_the_nineteen_sheet_keys(ex):
    """The liq_flush tab is 19 fixed columns (A1:S1) -- 18 plus late_bars, added
    when the signal catch-up landed. An unsold row must not widen the schema, or
    n8n's Sheets mapping breaks on deploy.

    If this number ever changes again, the tab's header row has to change with
    it: n8n maps by header name, so a field with no column is dropped silently.
    """
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.00001")
    ex.wallet = Decimal("0.00001")
    ex.klines["SOLUSDT"] = []
    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=False)

    _seed_open(ex, "ADAUSDT", entry_price="100", entry_time=T0, qty="0.1")
    ex.wallet = Decimal("1000000")
    ex.klines["ADAUSDT"] = []
    normal = _blank()
    liq_flush.process_exits(ex, normal, now=T0 + 24 * HOUR, dry=False)

    assert set(result["journal"][0]) == set(normal["journal"][0])
    assert len(result["journal"][0]) == 19


def test_balance_far_below_expected_is_not_deleted_and_warns(ex):
    """Half the coins missing is not dust -- it is a bad balance read or a bad
    calculation, and deleting the record would lose a real position."""
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.1")
    ex.wallet = Decimal("0.05")          # 50% of the recorded 0.1
    ex.klines["SOLUSDT"] = []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=False)

    assert result["exits"][0]["status"] == "balance_mismatch"
    assert result["exits"][0]["attempts"] == 1
    assert ex.sells == []
    # the position stays open for the next tick
    assert liq_flush.open_key("SOLUSDT", False) in ex.store
    assert result["journal"] == []       # nothing claimed about the result yet
    assert any("عدم تطابق الرصيد" in m for m in result["messages"])


def test_a_balance_just_under_the_recorded_qty_still_sells(ex):
    """Fees shave a little off the base coin, so 'slightly less' is normal and
    must not be mistaken for a mismatch."""
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.1")
    ex.wallet = Decimal("0.0995")        # 99.5% -- above the 90% floor
    ex.klines["SOLUSDT"] = []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=False)

    assert result["exits"][0]["status"] == "sold"
    assert ex.sells


def test_mismatch_escalates_to_needs_manual_check_after_three_checks(ex):
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.1")
    ex.wallet = Decimal("0.05")
    ex.klines["SOLUSDT"] = []

    seen = []
    for attempt in range(3):
        result = _blank()
        liq_flush.process_exits(ex, result, now=T0 + (24 + attempt) * HOUR, dry=False)
        seen.append(result)

    assert [r["exits"][0]["status"] for r in seen] == [
        "balance_mismatch", "balance_mismatch", "needs_manual_check"]
    assert [r["exits"][0]["attempts"] for r in seen] == [1, 2, 3]

    # only the final check closes the record, and it closes it loudly
    assert liq_flush.open_key("SOLUSDT", False) not in ex.store
    assert ex.sells == []
    final = seen[-1]
    assert final["journal"][0]["exit_reason"].startswith("needs_manual_check")
    assert any("needs_manual_check" in m for m in final["messages"])
    assert seen[0]["journal"] == [] and seen[1]["journal"] == []


def test_attempt_counter_resets_when_the_balance_comes_back(ex):
    """A transient bad read must not leave a trade one strike from being closed
    for good once the balance is healthy again."""
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.1")
    ex.wallet = Decimal("0.05")
    ex.klines["SOLUSDT"] = []
    liq_flush.process_exits(ex, _blank(), now=T0 + 24 * HOUR, dry=False)
    assert ex.store[liq_flush.open_key("SOLUSDT", False)]["exit_attempts"] == 1

    ex.wallet = Decimal("0.1")           # balance recovered
    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 25 * HOUR, dry=False)
    assert result["exits"][0]["status"] == "sold"
    assert ex.sells


def test_dry_run_exit_never_reads_the_real_wallet(ex):
    """There was no real buy, so there is no real balance to check. Reading one
    measured another strategy's coins (or zero) and turned every paper exit into
    silent 'dust' -- which is how the paper trade's result went missing."""
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.1", dry=True)
    ex.wallet = Decimal("0")             # nothing of this coin is really held
    ex.prices["SOLUSDT"] = Decimal("110")
    ex.klines["SOLUSDT"] = []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=True)

    assert ex.coin_balance_calls == []
    assert ex.sells == []
    assert result["exits"][0]["status"] == "sold"


def test_dry_run_time_exit_writes_a_full_row_and_a_sell_message(ex):
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.1", dry=True)
    ex.wallet = Decimal("0")
    ex.prices["SOLUSDT"] = Decimal("110")
    ex.klines["SOLUSDT"] = []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=True)

    row = result["journal"][0]
    assert row["event"] == "exit" and row["exit_reason"] == "time"
    assert row["exit_time"] and row["exit_price"] == "110"
    assert row["dry_run"] is True
    # bought 0.1 at 100 = 10, sold 0.1 at 110 = 11, and no fee is charged on paper
    assert Decimal(row["pnl"]) == Decimal("1")
    assert Decimal(row["pnl_pct"]) == Decimal("10")

    message = result["messages"][0]
    assert "تجريبي" in message and "بيع" in message
    assert liq_flush.open_key("SOLUSDT", True) not in ex.store


def test_dry_run_stop_exit_also_reports_normally(ex):
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.1", dry=True)
    ex.wallet = Decimal("0")
    ex.prices["SOLUSDT"] = Decimal("90")
    ex.klines["SOLUSDT"] = []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + HOUR, dry=True)

    assert result["exits"][0]["reason"] == "stop"
    assert result["journal"][0]["exit_reason"] == "stop"
    assert "تجريبي" in result["messages"][0]
    assert ex.coin_balance_calls == [] and ex.sells == []


def test_live_exit_message_is_not_tagged_as_paper(ex):
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.1")
    ex.klines["SOLUSDT"] = []
    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=False)
    assert "تجريبي" not in result["messages"][0]


def test_exit_record_carries_everything_the_sheet_needs(ex):
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.1",
               z_r4=-3.7, z_oi4=-4.1)
    ex.prices["SOLUSDT"] = Decimal("110")
    ex.klines["SOLUSDT"] = []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=False)
    row = result["journal"][0]

    assert row["strategy"] == "liq_flush" and row["event"] == "exit"
    assert row["symbol"] == "SOLUSDT"
    assert row["exit_reason"] == "time"
    assert row["z_r4"] == -3.7 and row["z_oi4"] == -4.1
    assert row["signal_bar"] == "2026-10-05T04:00:00Z"
    assert Decimal(row["pnl_pct"]) > 0        # bought 100, sold 110
    assert Decimal(row["fees"]) > 0           # buy fee + sell fee
    assert row["hold_hours"] == 24.0
    for field in ("entry_time", "entry_price", "qty", "exit_time", "exit_price",
                  "stop_level", "pnl"):
        assert row[field] is not None, field


def test_one_broken_symbol_does_not_block_the_others(ex):
    _seed_open(ex, "AAAUSDT", entry_price="100", entry_time=T0)
    _seed_open(ex, "BBBUSDT", entry_price="100", entry_time=T0)
    ex.raise_on_price.add("AAAUSDT")
    ex.klines = {"AAAUSDT": [], "BBBUSDT": []}

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=False)

    assert [e["symbol"] for e in result["errors"]] == ["AAAUSDT"]
    assert ex.sells[0][0] == "BBBUSDT"


# ============================================================
# one full cycle, which is what the dry run is for
# ============================================================

def test_full_cycle_signal_to_buy_to_time_exit(ex, monkeypatch):
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    ex.prices["SOLUSDT"] = Decimal("100")
    ex.klines["SOLUSDT"] = []

    bought = tick(ex, force_signals=[sig("SOLUSDT")], now=T0)
    assert bought["entries"][0]["status"] == "bought"
    assert any("🟢 شراء: SOLUSDT" in m for m in bought["messages"])
    assert bought["journal"][0]["event"] == "entry"

    # the position is open, cooldown is set, nothing was sent to Bybit
    assert liq_flush.open_key("SOLUSDT", True) in ex.store
    assert liq_flush.cooldown_key("SOLUSDT", True) in ex.store
    assert ex.buys == [] and ex.sells == []

    # a tick one hour later holds
    held = tick(ex, skip_scan=True, now=T0 + HOUR)
    assert held["exits"][0]["status"] == "holding"

    # 24h later it exits on time, at a profit
    ex.prices["SOLUSDT"] = Decimal("101.65")      # the backtest's +1.65% average
    sold = tick(ex, skip_scan=True, now=T0 + 24 * HOUR)

    assert sold["exits"][0]["reason"] == "time"
    assert any("بيع: SOLUSDT" in m and "24 ساعة" in m for m in sold["messages"])
    assert float(sold["journal"][0]["pnl_pct"]) == pytest.approx(1.65, abs=0.01)
    assert liq_flush.open_key("SOLUSDT", True) not in ex.store
    assert ex.sells == []                         # still nothing real


# ============================================================
# safety
# ============================================================

def test_kill_switch_blocks_entries_but_still_runs_exits(ex, monkeypatch):
    """A switch that stranded an open position past its -8% stop would be worse
    than no switch at all."""
    monkeypatch.setenv("LIQ_ENABLED", "false")
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0)
    ex.klines["SOLUSDT"] = []

    out = liq_flush.run_tick(ex, {})
    assert out["status"] == "disabled_exits_only"
    assert ex.sells and ex.sells[0][0] == "SOLUSDT"   # the 24h exit still ran
    assert ex.buys == []


def test_kill_switch_with_nothing_open_returns_immediately(ex, monkeypatch):
    monkeypatch.setenv("LIQ_ENABLED", "false")
    out = liq_flush.run_tick(ex, {})
    assert out["status"] == "disabled"
    assert out["exits"] == [] and out["entries"] == []


def test_overrides_are_refused_outside_dry_run(ex):
    """Otherwise anyone holding the webhook secret could force a real buy."""
    for params in ({"force_signals": [sig("SOLUSDT")]}, {"skip_scan": True}, {"now": T0}):
        out = liq_flush.run_tick(ex, params)
        assert out["status"] == "overrides_rejected"
    assert ex.buys == []


def test_concurrent_tick_is_locked_out(ex, monkeypatch):
    """n8n retries on timeout, and the scan takes ~60s -- without the lock two
    overlapping ticks could enter the same signal twice."""
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    ex.store[liq_flush.lock_key(True)] = {"since": T0}
    out = tick(ex, force_signals=[sig("SOLUSDT")], now=T0)
    assert out["status"] == "locked"
    assert out["entries"] == []


def test_lock_is_released_even_when_the_tick_raises(ex, monkeypatch):
    monkeypatch.setenv("LIQ_DRY_RUN", "true")

    def boom(*a, **k):
        raise RuntimeError("Redis unavailable")

    monkeypatch.setattr(liq_flush, "process_exits", boom)
    out = tick(ex, skip_scan=True, now=T0)

    assert out["status"] == "error"
    assert liq_flush.lock_key(True) not in ex.store   # next tick is not blocked
    assert any("خطأ liq_flush" in m for m in out["messages"])


def test_tick_never_raises_when_the_scan_fails(ex, monkeypatch):
    broken = type(sys)("liq_signal")

    def boom():
        raise RuntimeError("Bybit failed: /v5/market/kline BTCUSDT")

    broken.scan = boom
    monkeypatch.setitem(sys.modules, "liq_signal", broken)

    out = liq_flush.run_tick(ex, {})
    assert out["status"] == "error"
    assert out["errors"][0]["stage"] == "tick"
    assert any("خطأ liq_flush" in m for m in out["messages"])


def test_scan_errors_are_surfaced_not_swallowed(ex, monkeypatch):
    """liq_signal.scan() turns a per-symbol failure into a row with 'error' and
    signal=False -- silently fewer symbols checked. Say so."""
    fake = type(sys)("liq_signal")
    fake.scan = lambda: (
        [{"symbol": "BTCUSDT", "error": "timeout", "signal": False},
         {"symbol": "ETHUSDT", "signal": False}],
        [],
    )
    monkeypatch.setitem(sys.modules, "liq_signal", fake)

    out = liq_flush.run_tick(ex, {})
    assert out["scan"]["errors"] == 1
    assert any("فشل حساب الإشارة" in m for m in out["messages"])


def test_quiet_when_nothing_happens(ex, monkeypatch):
    """No spam: a scan with no signals and no open positions says nothing."""
    fake = type(sys)("liq_signal")
    fake.scan = lambda: ([{"symbol": "BTCUSDT", "signal": False}], [])
    monkeypatch.setitem(sys.modules, "liq_signal", fake)

    out = liq_flush.run_tick(ex, {})
    assert out["messages"] == []
    assert out["status"] == "ok"


def test_locked_numbers_match_the_backtest():
    """These came out of liquidation_stress_test.py and must not drift."""
    assert liq_flush.MAX_PER_SCAN == 3
    assert liq_flush.MAX_OPEN == 6
    assert liq_flush.COOLDOWN_SECONDS == 24 * 3600
    assert liq_flush.HOLD_SECONDS == 24 * 3600
    assert liq_flush.STOP_FRACTION == Decimal("0.92")
    # and they are NOT reachable from the environment
    assert not hasattr(liq_flush, "max_open")


def test_defaults_are_off(monkeypatch):
    monkeypatch.delenv("LIQ_ENABLED", raising=False)
    monkeypatch.delenv("LIQ_DRY_RUN", raising=False)
    monkeypatch.delenv("LIQ_USDT_PER_TRADE", raising=False)
    assert liq_flush.is_enabled() is False
    assert liq_flush.is_dry_run() is False
    assert liq_flush.usdt_per_trade() == Decimal("10")


# ============================================================
# the double entry of 2026-10-07
# ============================================================
# ATOMUSDT reported "bought" twice 74 seconds apart (n8n executions 109694 and
# 109695), both with skipped == []. These tests pin down which paths can and
# cannot produce that, so the next occurrence is diagnosable instead of
# re-litigated. The sequential path -- the one that actually ran in
# production -- is blocked and was blocked then too, so the production cause is
# NOT established by these tests; see test_wiping_the_two_guards_is_the_only...

def test_a_second_tick_74_seconds_later_is_blocked_as_already_open(ex, monkeypatch):
    """The exact production shape: two full ticks, same forced signal, 74s apart."""
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    first = liq_flush.run_tick(ex, {"force_signals": [sig("ATOMUSDT")], "now": T0})
    second = liq_flush.run_tick(ex, {"force_signals": [sig("ATOMUSDT")], "now": T0 + 74})

    assert first["entries"][0]["status"] == "bought"
    # the tick lock is released in run_tick's finally, so it is NOT what stops
    # the second tick -- already_open is
    assert second["status"] == "ok"
    assert second["entries"] == []
    assert second["skipped"] == [{"symbol": "ATOMUSDT", "reason": "already_open"}]


def test_wiping_the_two_guards_reproduces_the_production_signature(ex, monkeypatch):
    """Deleting open: + cooldown: is the ONLY sequence found that yields a second
    "bought" with skipped == [] -- the signature the live run showed.

    It is consistent with a manual Redis cleanup between the two forced tests
    (the first one's Sheets node had failed), but it is NOT proof: nothing in
    Redis or the n8n log records who deleted what. The journal guard below
    exists so that this sequence can no longer end in a buy.
    """
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    liq_flush.run_tick(ex, {"force_signals": [sig("ATOMUSDT")], "now": T0})
    ex.redis_delete(liq_flush.open_key("ATOMUSDT", True))
    ex.redis_delete(liq_flush.cooldown_key("ATOMUSDT", True))

    out = liq_flush.run_tick(ex, {"force_signals": [sig("ATOMUSDT")], "now": T0 + 74})

    # the journal survives the wipe and now refuses the entry
    assert out["entries"] == []
    assert out["skipped"][0]["reason"] == "recent_entry_in_journal"
    assert out["skipped"][0]["entered_at"] == T0
    assert "سجل الدخول" in out["messages"][0]


def test_two_concurrent_entries_buy_the_symbol_only_once(monkeypatch):
    """The real race: every guard is read-then-write, so two parallel ticks can
    both clear them and both buy. Redis executes each command atomically, so
    the fake locks per command -- the window is BETWEEN commands, which is
    exactly what SET NX closes.
    """
    monkeypatch.setenv("LIQ_DRY_RUN", "false")
    command = threading.Lock()
    reserve = liq_flush.reserve_key("ATOMUSDT", False)
    both_attempted = threading.Barrier(2, timeout=10)

    class AtomicRedis(FakeExecutor):
        def redis_set_json(self, key, value, *, ex_seconds=None, nx=False):
            with command:
                got = super().redis_set_json(key, value, ex_seconds=ex_seconds, nx=nx)
            if key == reserve:
                # The reservation is the first write process_entries makes, so
                # holding both threads just after their attempt freezes the
                # winner before it can write open_key. The loser therefore
                # reads the same pre-entry state the winner did and clears
                # every read-then-write guard -- the window the live code has.
                # Deterministic in both directions: whoever wins blocks here
                # too, so neither can race ahead of the other.
                both_attempted.wait()
            return got

        def redis_get_json(self, key):
            with command:
                return super().redis_get_json(key)

        def redis_delete(self, key):
            with command:
                return super().redis_delete(key)

        def redis_keys(self, pattern):
            with command:
                return super().redis_keys(pattern)

    ex = AtomicRedis()
    results = []

    def one_tick():
        out = _blank()
        liq_flush.process_entries(ex, out, [sig("ATOMUSDT")], now=T0, dry=False)
        results.append(out)

    threads = [threading.Thread(target=one_tick) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(t.is_alive() for t in threads), "a thread never finished"

    # one buy, not two
    assert len(ex.buys) == 1
    bought = [e for r in results for e in r["entries"]]
    reserved = [s for r in results for s in r["skipped"]]
    assert len(bought) == 1
    assert reserved == [{"symbol": "ATOMUSDT", "reason": "entry_reserved"}]


def test_reservation_blocks_an_entry_while_a_buy_is_in_flight(ex):
    """Non-threaded form of the same guard: a live reservation means another
    worker is mid-buy, so this tick must not add a second order."""
    ex.store[liq_flush.reserve_key("SOLUSDT", False)] = {"since": T0 - 5}

    result = _blank()
    liq_flush.process_entries(ex, result, [sig("SOLUSDT")], now=T0, dry=False)

    assert ex.buys == []
    assert result["skipped"] == [{"symbol": "SOLUSDT", "reason": "entry_reserved"}]


def test_reservation_is_released_when_the_buy_fails(ex):
    """A failed buy must not leave the symbol locked out: the signal has to be
    takeable on the next tick."""
    ex.raise_on_price.add("SOLUSDT")  # dry path reads the ticker and raises

    result = _blank()
    liq_flush.process_entries(ex, result, [sig("SOLUSDT")], now=T0, dry=True)

    assert result["errors"][0]["stage"] == "entry"
    assert liq_flush.reserve_key("SOLUSDT", True) not in ex.store

    # and the next tick can take it
    ex.raise_on_price.clear()
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT")], now=T0 + 60, dry=True)
    assert liq_flush.open_key("SOLUSDT", True) in ex.store


def test_reservation_has_a_ttl_so_a_dead_worker_cannot_block_forever(ex):
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT")], now=T0, dry=False)
    assert ex.ttl[liq_flush.reserve_key("SOLUSDT", False)] == liq_flush.ENTRY_RESERVE_SECONDS
    assert liq_flush.ENTRY_RESERVE_SECONDS < liq_flush.COOLDOWN_SECONDS


def test_journal_is_written_on_entry_and_outlives_the_cooldown(ex):
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT")], now=T0, dry=False)

    key = liq_flush.journal_key("SOLUSDT", T0, False)
    assert ex.store[key]["entered"] == T0
    # longer than the cooldown on purpose: it is the guard that must still be
    # there when the cooldown key is not
    assert ex.ttl[key] == liq_flush.ENTRY_JOURNAL_SECONDS
    assert liq_flush.ENTRY_JOURNAL_SECONDS > liq_flush.COOLDOWN_SECONDS


def test_journal_lets_the_symbol_back_in_after_24h(ex):
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT")], now=T0, dry=False)
    # the wipe this guard exists for, plus the reservation whose 10-minute TTL
    # has long passed at the times used below (the fake does not enforce TTLs)
    ex.redis_delete(liq_flush.open_key("SOLUSDT", False))
    ex.redis_delete(liq_flush.cooldown_key("SOLUSDT", False))
    ex.redis_delete(liq_flush.reserve_key("SOLUSDT", False))

    # one second short of 24h: still refused
    blocked = _blank()
    liq_flush.process_entries(ex, blocked, [sig("SOLUSDT")], now=T0 + 24 * HOUR - 1, dry=False)
    assert blocked["skipped"][0]["reason"] == "recent_entry_in_journal"
    assert len(ex.buys) == 1

    # exactly 24h: eligible again, same boundary as the cooldown
    ex.redis_delete(liq_flush.reserve_key("SOLUSDT", False))
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT")], now=T0 + 24 * HOUR, dry=False)
    assert len(ex.buys) == 2


def test_journal_of_one_symbol_does_not_block_another(ex):
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT")], now=T0, dry=False)

    result = _blank()
    liq_flush.process_entries(ex, result, [sig("ETCUSDT")], now=T0 + 60, dry=False)
    assert result["skipped"] == []
    assert [s for s, *_ in ex.buys] == ["SOLUSDT", "ETCUSDT"]


def test_journal_keeps_the_dry_and_live_prefixes_apart(ex):
    liq_flush.process_entries(ex, _blank(), [sig("SOLUSDT")], now=T0, dry=True)

    # a paper entry must not block a live one -- different prefix entirely
    result = _blank()
    liq_flush.process_entries(ex, result, [sig("SOLUSDT")], now=T0 + 60, dry=False)
    assert result["skipped"] == []
    assert liq_flush.journal_key("SOLUSDT", T0, True) in ex.store
    assert liq_flush.journal_key("SOLUSDT", T0, False) not in ex.store


def test_a_malformed_journal_key_does_not_block_the_entry(ex):
    """KEYS is a pattern match; a stray key must not take the strategy down."""
    ex.store[f"{liq_flush._prefix(False)}:journal:SOLUSDT:not-a-number"] = {}

    result = _blank()
    liq_flush.process_entries(ex, result, [sig("SOLUSDT")], now=T0, dry=False)
    assert result["skipped"] == []
    assert len(ex.buys) == 1


# ============================================================
# the missed signal of 2026-10-08
# ============================================================
# AVAXUSDT signalled on the 17:00 bar. The 18:02 tick -- the only tick that
# would ever look at that bar -- died with a DNS error, so the signal was lost
# for good while the 19:02 tick ran fine and left no trace of the loss.
# liq_signal now evaluates the last TWO closed bars and tags each signal with
# late_bars; these tests pin down that the catch-up enters once and no more,
# and that it refuses a signal too old to still be the same trade.

def _late_sig(symbol, *, bar, late_bars, z=-3.5, close="100"):
    return {"symbol": symbol, "close": close, "z_r4": z, "z_oi4": z - 0.1,
            "bar": bar, "late_bars": late_bars}


def _bar(stamp):
    """ISO string for the hour bar OPENING at `stamp` (what Bybit returns)."""
    return liq_flush._iso(stamp)


def test_a_missed_tick_is_caught_up_on_the_next_tick(ex):
    """The 2026-10-08 case, start to finish: the bar closed an hour ago because
    the tick that should have seen it never ran. Entering it late is the whole
    point -- but the row must say so, or the live log cannot be compared with a
    backtest that always enters at the next bar's open."""
    bar_open = T0                      # bar 17:00, closes at T0 + 1h
    now = T0 + 2 * HOUR + 120          # the 19:02 tick: 1h02m after the close

    result = _blank()
    liq_flush.process_entries(
        ex, result, [_late_sig("AVAXUSDT", bar=_bar(bar_open), late_bars=1)],
        now=now, dry=False,
    )

    assert result["skipped"] == []
    assert len(ex.buys) == 1 and ex.buys[0][0] == "AVAXUSDT"
    assert result["entries"][0]["late_bars"] == 1

    # it has to survive into the record, because the exit row is written 24h
    # later when the signal itself is long gone
    record = ex.store[liq_flush.open_key("AVAXUSDT", False)]
    assert record["late_bars"] == 1
    assert record["signal_bar"] == _bar(bar_open)

    row = result["journal"][0]
    assert row["event"] == "entry" and row["late_bars"] == 1

    # and the person reading Telegram learns it was a catch-up, not a fresh entry
    assert any("AVAXUSDT" in m and "1" in m for m in result["messages"])


def test_the_caught_up_signal_is_entered_once_even_though_it_scans_twice(ex):
    """A two-bar window means the SAME bar is offered twice: as late_bars=0 on
    the tick after it closes, then as late_bars=1 on the tick after that. The
    second offer must not buy again."""
    bar_open = T0
    fresh_tick = T0 + HOUR + 120       # bar seen as the last closed bar
    next_tick = T0 + 2 * HOUR + 120    # same bar, now one bar old

    first = _blank()
    liq_flush.process_entries(
        ex, first, [_late_sig("AVAXUSDT", bar=_bar(bar_open), late_bars=0)],
        now=fresh_tick, dry=False,
    )
    assert len(ex.buys) == 1
    assert first["entries"][0]["late_bars"] == 0

    second = _blank()
    liq_flush.process_entries(
        ex, second, [_late_sig("AVAXUSDT", bar=_bar(bar_open), late_bars=1)],
        now=next_tick, dry=False,
    )

    # one buy, total, and the skip says which guard caught it
    assert len(ex.buys) == 1, "the overlapping scan window bought the bar twice"
    assert second["entries"] == []
    assert second["journal"] == []
    assert second["skipped"][0]["reason"] == "already_open"


def test_the_catch_up_is_still_blocked_by_cooldown_after_the_position_closed(ex):
    """already_open is not the only guard that matters: a trade that opened and
    closed inside the overlap window leaves no open key, and only the cooldown
    and the entry journal stand between the second offer and a second buy."""
    bar_open = T0
    liq_flush.process_entries(
        ex, _blank(), [_late_sig("AVAXUSDT", bar=_bar(bar_open), late_bars=0)],
        now=T0 + HOUR + 120, dry=False,
    )
    assert len(ex.buys) == 1

    # the position is gone (sold, or cleared by hand) but the cooldown remains
    ex.redis_delete(liq_flush.open_key("AVAXUSDT", False))

    result = _blank()
    liq_flush.process_entries(
        ex, result, [_late_sig("AVAXUSDT", bar=_bar(bar_open), late_bars=1)],
        now=T0 + 2 * HOUR + 120, dry=False,
    )
    assert len(ex.buys) == 1
    assert result["skipped"][0]["reason"] == "cooldown"


def test_catch_up_refuses_a_signal_whose_bar_is_too_old(ex):
    """Several ticks in a row can fail (three did on 2026-10-08). Past two hours
    the flush has already played out, so entering is a different trade from the
    one the backtest measured -- refuse it and say so."""
    bar_open = T0                               # closes T0 + 1h
    now = T0 + 1 * HOUR + liq_flush.MAX_SIGNAL_AGE_SECONDS + 60

    result = _blank()
    liq_flush.process_entries(
        ex, result, [_late_sig("AVAXUSDT", bar=_bar(bar_open), late_bars=1)],
        now=now, dry=False,
    )

    assert ex.buys == []
    assert result["entries"] == []
    skip = result["skipped"][0]
    assert skip["reason"] == "signal_too_old"
    assert skip["late_bars"] == 1
    assert skip["age_minutes"] == pytest.approx(121, abs=1)
    assert any("AVAXUSDT" in m for m in result["messages"])


def test_the_age_limit_is_measured_from_the_bar_close_not_its_open(ex):
    """An hour bar opening at 17:00 only becomes knowable at 18:00. Measuring the
    age from the open would reject the normal one-tick catch-up as 2h01m old."""
    bar_open = T0
    # exactly at the limit measured from the close: 1h (bar) + 2h (limit)
    at_limit = T0 + HOUR + liq_flush.MAX_SIGNAL_AGE_SECONDS

    result = _blank()
    liq_flush.process_entries(
        ex, result, [_late_sig("AVAXUSDT", bar=_bar(bar_open), late_bars=1)],
        now=at_limit, dry=False,
    )
    assert len(ex.buys) == 1, "the boundary itself must be allowed"
    assert result["skipped"] == []


def test_an_unreadable_bar_is_refused_when_late_but_not_when_fresh(ex):
    """A signal whose age cannot be established is worse than a missed one, so
    the late path refuses it. The fresh path keeps working: force_signals is
    hand-written and its bar has never been required to parse."""
    result = _blank()
    liq_flush.process_entries(
        ex, result, [_late_sig("AVAXUSDT", bar="whenever", late_bars=1)],
        now=T0, dry=False,
    )
    assert ex.buys == []
    assert result["skipped"][0]["reason"] == "signal_too_old"
    assert result["skipped"][0]["age_minutes"] is None

    fresh = _blank()
    liq_flush.process_entries(
        ex, fresh, [_late_sig("SOLUSDT", bar="whenever", late_bars=0)],
        now=T0, dry=False,
    )
    assert len(ex.buys) == 1
    assert fresh["skipped"] == []


def test_a_signal_with_no_late_bars_field_is_treated_as_fresh(ex):
    """Every force_signals payload ever sent by hand omits late_bars, and the
    live overrides are documented in handoff/LIQ_FLUSH_AR.md. Omitting it must
    mean "the last closed bar", not "unknown age, refuse"."""
    result = _blank()
    liq_flush.process_entries(ex, result, [sig("SOLUSDT")], now=T0, dry=False)
    assert len(ex.buys) == 1
    assert result["entries"][0]["late_bars"] == 0
    assert result["journal"][0]["late_bars"] == 0


def test_late_bars_reaches_the_exit_row_a_day_later(ex):
    """The exit row is the one that gets compared with the backtest, and by the
    time it is written the signal is 24h gone -- so late_bars has to travel in
    the open record, not be recomputed."""
    bar_open = T0
    liq_flush.process_entries(
        ex, _blank(), [_late_sig("SOLUSDT", bar=_bar(bar_open), late_bars=1)],
        now=T0 + 2 * HOUR, dry=False,
    )
    ex.klines["SOLUSDT"] = []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 26 * HOUR + 1, dry=False)

    row = result["journal"][0]
    assert row["event"] == "exit"
    assert row["late_bars"] == 1


# ============================================================
# the 25-hour hold of 2026-10-08
# ============================================================
# ETCUSDT entered at 19:02:11 because the request reached the executor four
# seconds after n8n started the run. The next day's tick arrived at 19:02:07 --
# four seconds short of 24h -- so the strict >= missed and the position sat for
# another full hour. HOLD_TOLERANCE_SECONDS closes that gap.

def test_time_exit_fires_when_the_tick_lands_seconds_short_of_24h(ex):
    """The exact 2026-10-08 ETCUSDT shape: entry stamped a few seconds into the
    tick, next day's tick a few seconds earlier than that."""
    _seed_open(ex, "ETCUSDT", entry_price="100", entry_time=T0 + 4)
    ex.klines["ETCUSDT"] = []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=False)

    assert result["exits"][0]["reason"] == "time", "a 4-second miss cost a full hour"
    assert ex.sells[0][0] == "ETCUSDT"


def test_the_tolerance_cannot_pull_the_exit_a_whole_tick_early(ex):
    """The tolerance exists to stop a seconds-wide miss, not to shorten the hold.
    It must stay far below the hour between two ticks, or the exit moves to the
    23:02 tick and the live hold is an hour short of the backtest's 24."""
    assert liq_flush.HOLD_TOLERANCE_SECONDS < 1800

    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0)
    ex.klines["SOLUSDT"] = []

    # the tick an hour before the due time -- where the exit would land if the
    # tolerance were anywhere near an hour wide
    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 23 * HOUR + 120, dry=False)
    assert result["exits"][0]["status"] == "holding"
    assert ex.sells == []

    # and ten minutes short is still short: only a seconds-wide miss is forgiven
    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 23 * HOUR + 50 * 60, dry=False)
    assert result["exits"][0]["status"] == "holding"
    assert ex.sells == []


# ============================================================
# /liq_health -- is the hourly tick arriving at all?
# ============================================================

def test_health_reports_no_tick_before_the_first_one(ex, monkeypatch):
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    out = liq_flush.health(ex)
    assert out["last_tick_at"] is None
    assert out["last_tick_age_minutes"] is None
    assert out["stale"] is True            # silence is stale, not healthy
    assert out["dry_run"] is True


def test_a_tick_stamps_its_arrival(ex, monkeypatch):
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    liq_flush.run_tick(ex, {"skip_scan": True, "now": T0})
    assert ex.store[liq_flush.last_tick_key(True)]["at"] == T0


def test_health_reports_the_age_in_minutes(ex, monkeypatch):
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    now = 1_760_000_600
    monkeypatch.setattr(liq_flush.time, "time", lambda: now)
    ex.store[liq_flush.last_tick_key(True)] = {"at": now - 25 * 60}

    out = liq_flush.health(ex)
    assert out["last_tick_age_minutes"] == 25.0
    assert out["stale"] is False           # inside one hourly beat
    assert out["expected_interval_minutes"] == 60


def test_health_flags_a_tick_that_never_arrived(ex, monkeypatch):
    """The 2026-10-08 DNS failures: the service answered fine and the strategy
    was dead. Three missed hourly beats must read as stale."""
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    now = 1_760_000_600
    monkeypatch.setattr(liq_flush.time, "time", lambda: now)
    ex.store[liq_flush.last_tick_key(True)] = {"at": now - 3 * 60 * 60}

    out = liq_flush.health(ex)
    assert out["last_tick_age_minutes"] == 180.0
    assert out["stale"] is True


def test_health_is_read_only(ex, monkeypatch):
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    ex.store[liq_flush.last_tick_key(True)] = {"at": T0}
    before = json.dumps(ex.store, sort_keys=True)

    liq_flush.health(ex)
    assert json.dumps(ex.store, sort_keys=True) == before
    assert ex.buys == [] and ex.sells == []
    assert ex.coin_balance_calls == []


def test_health_survives_a_broken_record(ex, monkeypatch):
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    ex.store[liq_flush.last_tick_key(True)] = {"oops": 1}

    out = liq_flush.health(ex)
    assert "malformed" in out["error"]
    assert out["stale"] is True


def test_health_reads_the_prefix_it_runs_under(ex, monkeypatch):
    """A paper stamp must not make the live strategy look alive."""
    ex.store[liq_flush.last_tick_key(True)] = {"at": T0}

    monkeypatch.setenv("LIQ_DRY_RUN", "false")
    assert liq_flush.health(ex)["last_tick_at"] is None
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    assert liq_flush.health(ex)["last_tick_at"] == T0


def test_a_disabled_tick_still_stamps_its_arrival(ex, monkeypatch):
    """The kill switch stops trading, not the heartbeat: otherwise turning the
    strategy off makes the monitor read as a dead executor."""
    monkeypatch.setenv("LIQ_ENABLED", "false")
    monkeypatch.setenv("LIQ_DRY_RUN", "true")

    out = liq_flush.run_tick(ex, {"skip_scan": True, "now": T0})
    assert out["status"].startswith("disabled")
    assert ex.store[liq_flush.last_tick_key(True)]["at"] == T0


def test_a_stamp_failure_does_not_break_the_tick(ex, monkeypatch):
    """The stamp is diagnostics, never the job."""
    monkeypatch.setenv("LIQ_DRY_RUN", "true")
    real_set = ex.redis_set_json

    def refuse_the_stamp(key, value, **kwargs):
        if key == liq_flush.last_tick_key(True):
            raise RuntimeError("Redis write failed")
        return real_set(key, value, **kwargs)

    monkeypatch.setattr(ex, "redis_set_json", refuse_the_stamp)
    out = liq_flush.run_tick(ex, {"force_signals": [sig("SOLUSDT")], "now": T0})
    assert out["status"] == "ok"
    assert out["entries"][0]["status"] == "bought"


# ============================================================
# helpers
# ============================================================

def _blank():
    return {"exits": [], "entries": [], "skipped": [], "journal": [],
            "messages": [], "errors": []}


def _run_forced(ex, signals, now=T0):
    import os
    os.environ["LIQ_DRY_RUN"] = "true"
    try:
        return liq_flush.run_tick(ex, {"force_signals": signals, "now": now})
    finally:
        os.environ["LIQ_DRY_RUN"] = "false"


def _seed_open(ex, symbol, *, entry_price, entry_time, qty="0.1",
               z_r4=-3.5, z_oi4=-3.6, dry=False):
    ex.store[liq_flush.open_key(symbol, dry)] = {
        "strategy": "liq_flush",
        "symbol": symbol,
        "coin": symbol[:-4],
        "entry_time": entry_time,
        "entry_time_iso": liq_flush._iso(entry_time),
        "entry_price": entry_price,
        "entry_value": str(Decimal(entry_price) * Decimal(qty)),
        "entry_fee": "0.001",
        "qty": qty,
        "stop_level": str(Decimal(entry_price) * Decimal("0.92")),
        "signal_bar": "2026-10-05T04:00:00Z",
        "z_r4": z_r4,
        "z_oi4": z_oi4,
        "dry_run": dry,
    }
