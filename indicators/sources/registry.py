"""Maps config's "market" string to a DataSource. Adding a new market is
exactly: implement DataSource in a new module under sources/, add one line
here, and set market to that name in config.json / DASHBOARD_MARKET. No
other file needs to change.

Future entries (not implemented yet -- see the multi-market refactor
brief): "tasi" -> a Saudi-exchange source (e.g. backed by SAHMK), "us" -> a
US-equities source. Both would need market_hours() reporting real session
times (is_24_7=False) so VWAP and similar session-anchored calculations
can eventually reset at session open instead of UTC midnight.
"""

from __future__ import annotations

from .base import DataSource
from .bybit_crypto import BybitCryptoSource
from .bybit_live import BybitLiveCryptoSource

_SOURCES: dict[str, type[DataSource]] = {
    "crypto": BybitLiveCryptoSource,          # live Bybit REST fetch (production default)
    "crypto_static": BybitCryptoSource,       # static backtest/data_15m/ cache (offline/testing)
}


def get_source(market: str) -> DataSource:
    try:
        source_cls = _SOURCES[market]
    except KeyError:
        available = ", ".join(sorted(_SOURCES))
        raise ValueError(f"unknown market {market!r}; available markets: {available}") from None
    return source_cls()


__all__ = ["get_source"]
