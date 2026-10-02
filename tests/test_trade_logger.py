"""Offline tests for trade_logger.py's resume/dedupe pair. No network, no Bybit.

Covers only the two coupled correctness properties:

  C1  a fill the API reveals LATE (execTime before the previous window's end,
      visible only after it) is still captured, because each run steps back by
      OVERLAP_MS instead of resuming exactly where it stopped.

  C2  that re-read never duplicates a row, even once the archive passes
      SEEN_IDS_MAX fills -- which requires the remembered execIds to be pruned
      in TIME order, not text order.

They are tested together as well as apart, because C1 without C2 is worse than
neither: deliberately re-reading recent fills with unreliable dedupe appends
them again every cycle.

Bybit is replaced by FakeBybit, which mimics the parts of
GET /v5/execution/list this code depends on: inclusive startTime/endTime
filtering on execTime, newest-first ordering, and nextPageCursor pagination.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_trade_logger(tmp_path, monkeypatch):
    """Import trade_logger.py fresh, pointed at a temp data dir.

    Imported by path rather than name: the module reads its paths from the
    environment at import time, and each test needs its own archive.
    """
    monkeypatch.setenv("TRADE_LOGGER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BYBIT_API_KEY", "test-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "test-secret")
    monkeypatch.delenv("TRADE_LOGGER_OVERLAP_SECONDS", raising=False)

    spec = importlib.util.spec_from_file_location(
        "trade_logger_under_test", REPO_ROOT / "trade_logger.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClock:
    """Stands in for the `time` module so tests control now_ms() exactly."""

    def __init__(self, start: float):
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        # Pagination's inter-page pause: advance rather than actually wait.
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBybit:
    """Minimal GET /v5/execution/list stand-in."""

    def __init__(self, fills=None, page_limit_override=None):
        self.fills = list(fills or [])
        self.calls = 0
        self.page_limit_override = page_limit_override

    def signed_get(self, path, params):
        assert path == "/v5/execution/list", path
        assert params["category"] == "spot"
        self.calls += 1

        start = int(params["startTime"])
        end = int(params["endTime"])
        limit = self.page_limit_override or int(params["limit"])

        # startTime/endTime are inclusive on execTime, newest first -- as Bybit does.
        matching = [f for f in self.fills if start <= int(f["execTime"]) <= end]
        matching.sort(key=lambda f: int(f["execTime"]), reverse=True)

        offset = int(params.get("cursor") or 0)
        page = matching[offset:offset + limit]
        next_offset = offset + len(page)
        next_cursor = str(next_offset) if next_offset < len(matching) else ""

        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"list": page, "nextPageCursor": next_cursor},
        }


def make_fill(exec_id: str, exec_time_ms: int, symbol: str = "BTCUSDT") -> dict:
    return {
        "execId": exec_id,
        "orderId": f"order-{exec_id}",
        "orderLinkId": f"sig-{exec_id}",
        "symbol": symbol,
        "side": "Buy",
        "execType": "Trade",
        "orderType": "Limit",
        "execPrice": "50000",
        "execQty": "0.001",
        "execValue": "50",
        "execFee": "0.05",
        "feeCurrency": "USDT",
        "feeRate": "0.001",
        "isMaker": True,
        "execTime": str(exec_time_ms),
    }


def read_ids(log_path) -> list[str]:
    path = Path(log_path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line)["exec_id"] for line in handle if line.strip()]


# ----------------------------------------------------------------------------
# C1: the overlap catches a late-appearing fill
# ----------------------------------------------------------------------------

def test_late_appearing_fill_is_captured_by_overlap(tmp_path, monkeypatch):
    tl = load_trade_logger(tmp_path, monkeypatch)
    clock = FakeClock(start=1_700_000_000.0)
    monkeypatch.setattr(tl, "time", clock)
    api = FakeBybit()
    monkeypatch.setattr(tl, "signed_get", api.signed_get)

    # Cycle 1: the account has no visible fills yet.
    state = tl.load_state()
    assert tl.run_once(state) == 0
    first_end = state["last_end_ms"]
    assert read_ids(tl.LOG_PATH) == []

    # A fill stamped 30s BEFORE that window closed, which the API only reveals
    # now. Resuming exactly at last_end_ms would never request it again.
    late = make_fill("exec-late", first_end - 30_000)
    api.fills.append(late)
    assert int(late["execTime"]) < first_end

    clock.advance(300)
    assert tl.run_once(state) == 1
    assert read_ids(tl.LOG_PATH) == ["exec-late"]


def test_late_fill_would_be_lost_without_overlap(tmp_path, monkeypatch):
    """The control for the test above: with the overlap disabled the same fill is
    silently lost. This is the C1 bug, pinned so a regression is visible."""
    tl = load_trade_logger(tmp_path, monkeypatch)
    clock = FakeClock(start=1_700_000_000.0)
    monkeypatch.setattr(tl, "time", clock)
    monkeypatch.setattr(tl, "OVERLAP_MS", 0)
    api = FakeBybit()
    monkeypatch.setattr(tl, "signed_get", api.signed_get)

    state = tl.load_state()
    tl.run_once(state)
    first_end = state["last_end_ms"]

    api.fills.append(make_fill("exec-late", first_end - 30_000))
    clock.advance(300)

    assert tl.run_once(state) == 0
    assert read_ids(tl.LOG_PATH) == []


def test_overlap_floor_cannot_be_disabled(tmp_path, monkeypatch):
    """A zero or negative override must not silently reintroduce the gap."""
    monkeypatch.setenv("TRADE_LOGGER_OVERLAP_SECONDS", "0")
    monkeypatch.setenv("TRADE_LOGGER_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "trade_logger_floor", REPO_ROOT / "trade_logger.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.OVERLAP_MS == 60_000


# ----------------------------------------------------------------------------
# C2: no duplicates, including past the remembered-id cap
# ----------------------------------------------------------------------------

def test_overlap_does_not_duplicate_rows(tmp_path, monkeypatch):
    """The plain C1+C2 interaction: re-reading a window appends nothing twice."""
    tl = load_trade_logger(tmp_path, monkeypatch)
    clock = FakeClock(start=1_700_000_000.0)
    monkeypatch.setattr(tl, "time", clock)
    now_ms = int(clock.time() * 1000)
    api = FakeBybit([make_fill(f"exec-{i}", now_ms - i * 1000) for i in range(5)])
    monkeypatch.setattr(tl, "signed_get", api.signed_get)

    state = tl.load_state()
    assert tl.run_once(state) == 5

    # Three more cycles, no new fills. Each re-reads the overlap window, which
    # contains all five, and must append none of them again.
    for _ in range(3):
        clock.advance(300)
        assert tl.run_once(state) == 0

    assert len(read_ids(tl.LOG_PATH)) == 5


def test_no_duplicates_after_thousands_of_fills(tmp_path, monkeypatch):
    """Past SEEN_IDS_MAX, the remembered ids must still be the most RECENT ones.

    The execIds here are deliberately adversarial: id text order is the exact
    REVERSE of time order, so the old sorted(seen)[-10000:] pruning would have
    retained the oldest ids and dropped every recent one -- then re-appended
    them on the next overlap read.
    """
    tl = load_trade_logger(tmp_path, monkeypatch)
    clock = FakeClock(start=1_700_000_000.0)
    monkeypatch.setattr(tl, "time", clock)
    # The page guard is not what is under test here; 12k fills at 100/page needs
    # more than the production MAX_PAGES of 50.
    monkeypatch.setattr(tl, "MAX_PAGES", 500)

    total = 12_000
    assert total > tl.SEEN_IDS_MAX
    now_ms = int(clock.time() * 1000)
    # Fill i is i seconds old, so i ascends as time DESCENDS -- which makes the
    # id text order the exact reverse of time order. sorted(ids)[-10000:] then
    # keeps the 10k OLDEST and discards every recent id.
    fills = [make_fill(f"exec-{i:06d}", now_ms - i * 1000) for i in range(total)]
    api = FakeBybit(fills)
    monkeypatch.setattr(tl, "signed_get", api.signed_get)

    state = tl.load_state()
    assert tl.run_once(state) == total

    remembered = state["seen_exec_ids"]
    assert len(remembered) == tl.SEEN_IDS_MAX

    # The remembered window must be the NEWEST ids, in time order.
    newest_ids = [
        f["execId"]
        for f in sorted(fills, key=lambda f: int(f["execTime"]))[-tl.SEEN_IDS_MAX:]
    ]
    assert remembered == newest_ids

    # And this is what the old text-order pruning would have kept instead --
    # none of the recent ids the next overlap read depends on.
    legacy_kept = sorted(f["execId"] for f in fills)[-tl.SEEN_IDS_MAX:]
    recent_ids = {f["execId"] for f in fills[:200]}  # the 200 newest
    assert not (recent_ids & set(legacy_kept)), "adversarial ids not adversarial"

    # Now the real assertion: further cycles re-read the overlap and add nothing.
    for _ in range(3):
        clock.advance(300)
        assert tl.run_once(state) == 0

    logged = read_ids(tl.LOG_PATH)
    assert len(logged) == total
    assert len(set(logged)) == total


def test_restart_preserves_dedupe_order(tmp_path, monkeypatch):
    """A container restart reloads the deque from disk with its time order intact,
    so the overlap after a restart still dedupes."""
    tl = load_trade_logger(tmp_path, monkeypatch)
    clock = FakeClock(start=1_700_000_000.0)
    monkeypatch.setattr(tl, "time", clock)
    now_ms = int(clock.time() * 1000)
    api = FakeBybit([make_fill(f"exec-{i}", now_ms - i * 1000) for i in range(10)])
    monkeypatch.setattr(tl, "signed_get", api.signed_get)

    state = tl.load_state()
    assert tl.run_once(state) == 10

    # Simulate the restart: drop all in-memory state, reload from the state file.
    clock.advance(300)
    reloaded = tl.load_state()
    assert reloaded["last_end_ms"] == state["last_end_ms"]
    assert tl.run_once(reloaded) == 0
    assert len(read_ids(tl.LOG_PATH)) == 10


def test_fills_are_written_in_time_order(tmp_path, monkeypatch):
    """Rows land oldest-first even though Bybit pages newest-first -- this is what
    makes the deque's eviction order correct across page boundaries."""
    tl = load_trade_logger(tmp_path, monkeypatch)
    clock = FakeClock(start=1_700_000_000.0)
    monkeypatch.setattr(tl, "time", clock)
    now_ms = int(clock.time() * 1000)
    # 250 fills over 3 pages at the production page size of 100.
    api = FakeBybit([make_fill(f"exec-{i:04d}", now_ms - i * 1000) for i in range(250)])
    monkeypatch.setattr(tl, "signed_get", api.signed_get)

    state = tl.load_state()
    assert tl.run_once(state) == 250

    with Path(tl.LOG_PATH).open(encoding="utf-8") as handle:
        times = [json.loads(line)["exec_time"] for line in handle if line.strip()]
    assert times == sorted(times)
    assert state["seen_exec_ids"][-1] == "exec-0000"  # newest fill, remembered last


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
