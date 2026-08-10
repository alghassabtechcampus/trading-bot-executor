"""Version A adapter that delegates unchanged signal logic to strategy.py."""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Sequence

from backtest import indicators as _legacy_indicators

if "indicators" not in sys.modules:
    sys.modules["indicators"] = _legacy_indicators
from backtest import strategy as _legacy_strategy  # noqa: E402

from .base import AdapterSignal
from ..models import Candle


class VersionAAdapter:
    strategy_name = "Version A"
    strategy_version = "legacy-strategy-a"
    max_hold_minutes = 90
    window_size = 200
    requires_btc_context = False

    @staticmethod
    def _legacy_candles(history: Sequence[Candle]) -> list[dict[str, float | int]]:
        return [
            {
                "timestamp": int(candle.timestamp.timestamp() * 1000),
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": float(candle.volume),
            }
            for candle in history
        ]

    @classmethod
    def _legacy_window(cls, history: Sequence[Candle]) -> list[dict[str, float | int]]:
        return cls._legacy_candles(history[-cls.window_size:])

    def evaluate(
        self,
        history: Sequence[Candle],
        *,
        btc_history: Sequence[Candle] | None = None,
    ) -> AdapterSignal | None:
        del btc_history
        raw = _legacy_strategy.evaluate_signal(self._legacy_window(history))
        if raw is None:
            return None
        metadata = {
            key: raw[key]
            for key in ("up_trend", "volume_ratio", "atr_percent")
        }
        return AdapterSignal(
            action=raw["action"],
            score=Decimal(str(raw["score"])),
            reference_price=Decimal(str(raw["price"])),
            stop_loss=(None if raw["stop_loss"] is None else Decimal(str(raw["stop_loss"]))),
            take_profit=(None if raw["take_profit"] is None else Decimal(str(raw["take_profit"]))),
            metadata=metadata,
        )


class VersionBAdapter(VersionAAdapter):
    """Version B: Version A signal gated by the unchanged Legacy BTC filter."""

    strategy_name = "Version B"
    strategy_version = "legacy-strategy-b"
    requires_btc_context = True
    btc_long_window_size = _legacy_strategy.BTC_EMA200_WINDOW

    def __init__(self) -> None:
        self._btc_filter_cache: dict[tuple[object, ...], bool] = {}

    def evaluate(
        self,
        history: Sequence[Candle],
        *,
        btc_history: Sequence[Candle] | None = None,
    ) -> AdapterSignal | None:
        raw = _legacy_strategy.evaluate_signal(self._legacy_window(history))
        if raw is None:
            return None

        current_close_time = history[-1].close_time
        aligned = bool(
            btc_history
            and btc_history[-1].symbol == "BTCUSDT"
            and btc_history[-1].close_time == current_close_time
        )
        btc_filter = False
        if raw["action"] == "BUY_NOW" and aligned:
            assert btc_history is not None
            cache_key = (
                current_close_time,
                len(btc_history),
                btc_history[-1].close,
            )
            if cache_key not in self._btc_filter_cache:
                self._btc_filter_cache[cache_key] = _legacy_strategy.evaluate_btc_filter(
                    self._legacy_window(btc_history),
                    self._legacy_candles(btc_history[-self.btc_long_window_size:]),
                )
            btc_filter = self._btc_filter_cache[cache_key]

        action = raw["action"]
        if action == "BUY_NOW" and not btc_filter:
            action = "IGNORE"
        metadata = {
            key: raw[key]
            for key in ("up_trend", "volume_ratio", "atr_percent")
        }
        metadata.update({
            "base_action": raw["action"],
            "btc_filter": btc_filter,
            "btc_timestamp_aligned": aligned,
            "btc_close_time": (
                btc_history[-1].close_time.isoformat() if aligned and btc_history else None
            ),
        })
        return AdapterSignal(
            action=action,
            score=Decimal(str(raw["score"])),
            reference_price=Decimal(str(raw["price"])),
            stop_loss=(None if raw["stop_loss"] is None else Decimal(str(raw["stop_loss"]))),
            take_profit=(None if raw["take_profit"] is None else Decimal(str(raw["take_profit"]))),
            metadata=metadata,
        )


legacy_strategy = _legacy_strategy
