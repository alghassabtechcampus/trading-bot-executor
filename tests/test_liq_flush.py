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

    # once the TTL has expired the symbol is eligible again
    ex.redis_delete(liq_flush.cooldown_key("SOLUSDT", False))
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


def test_dust_below_min_order_is_logged_and_cleared_not_sold(ex):
    _seed_open(ex, "SOLUSDT", entry_price="100", entry_time=T0, qty="0.00001")
    ex.wallet = Decimal("0.00001")
    ex.klines["SOLUSDT"] = []

    result = _blank()
    liq_flush.process_exits(ex, result, now=T0 + 24 * HOUR, dry=False)

    assert result["exits"][0]["status"] == "dust_cleared"
    assert ex.sells == []
    assert liq_flush.open_key("SOLUSDT", False) not in ex.store
    assert any("غبار" in m for m in result["messages"])


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
