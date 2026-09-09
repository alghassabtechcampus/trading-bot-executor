"""The 429 backoff must stay bounded.

A full alert check makes 45 sequential fetches inside ONE HTTP request. The
backoff used to be an uncapped 2**attempt over 5 tries -- 62s of sleeping
for a single fetch -- so one rate-limited symbol could consume the whole
request budget by itself and get the gunicorn worker killed mid-request,
which the caller sees as a reset connection rather than an error it can
read. These tests pin the bound rather than the exact numbers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicators.sources import bybit_live  # noqa: E402


class _Resp:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status on {self.status_code}")

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def spy(monkeypatch):
    """Records every sleep instead of performing it, so the test is instant."""
    slept: list[float] = []
    monkeypatch.setattr(bybit_live.time, "sleep", slept.append)
    return slept


def test_a_single_fetch_cannot_sleep_longer_than_the_bound(monkeypatch, spy):
    """Always-429: total backoff must stay within MAX_RETRIES x the cap."""
    monkeypatch.setattr(bybit_live.requests, "get", lambda *a, **k: _Resp(429))
    src = bybit_live.BybitLiveCryptoSource()
    with pytest.raises(RuntimeError, match="gave up"):
        src._get_with_retry({"symbol": "BTCUSDT"})

    assert len(spy) == bybit_live.MAX_RETRIES
    assert max(spy) <= bybit_live.MAX_BACKOFF_SECONDS
    assert sum(spy) <= bybit_live.MAX_RETRIES * bybit_live.MAX_BACKOFF_SECONDS


def test_the_bound_leaves_room_for_a_full_check(monkeypatch, spy):
    """45 fetches share ONE request against gunicorn's 300s worker timeout, so
    no single fetch may claim a large slice of it. 45s is 15% of the budget --
    survivable for one pathological symbol, and the cache fallback covers the
    rest. Tighten MAX_RETRIES / MAX_BACKOFF_SECONDS / REQUEST_TIMEOUT if this
    ever fails; do not simply raise the bound."""
    # Only a FAST 429 loops: a request that times out raises straight out of
    # _get_with_retry and is caught by get_ohlcv's cache fallback. So the worst
    # a fetch can cost is the whole backoff ladder plus one timeout, not
    # MAX_RETRIES timeouts.
    ladder = sum(min(2 ** a, bybit_live.MAX_BACKOFF_SECONDS)
                 for a in range(1, bybit_live.MAX_RETRIES + 1))
    worst = ladder + bybit_live.REQUEST_TIMEOUT
    assert worst < 45, f"one fetch can occupy {worst}s -- too much of the budget"


def test_recovers_without_sleeping_once_the_429_clears(monkeypatch, spy):
    """A transient rate limit must cost one backoff, not the full ladder."""
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        return _Resp(429) if calls["n"] == 1 else _Resp(200, {"retCode": 0, "result": {"list": []}})

    monkeypatch.setattr(bybit_live.requests, "get", flaky)
    payload = bybit_live.BybitLiveCryptoSource()._get_with_retry({"symbol": "BTCUSDT"})
    assert payload["retCode"] == 0
    assert len(spy) == 1


def test_a_bybit_error_is_raised_not_retried(monkeypatch, spy):
    """retCode != 0 is a real error, not congestion -- retrying it would just
    burn the budget."""
    monkeypatch.setattr(bybit_live.requests, "get",
                        lambda *a, **k: _Resp(200, {"retCode": 10001, "retMsg": "bad symbol"}))
    with pytest.raises(RuntimeError, match="bad symbol"):
        bybit_live.BybitLiveCryptoSource()._get_with_retry({"symbol": "NOPE"})
    assert spy == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
