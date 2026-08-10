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

    @staticmethod
    def _legacy_window(history: Sequence[Candle]) -> list[dict[str, float | int]]:
        return [
            {
                "timestamp": int(candle.timestamp.timestamp() * 1000),
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": float(candle.volume),
            }
            for candle in history[-VersionAAdapter.window_size:]
        ]

    def evaluate(self, history: Sequence[Candle]) -> AdapterSignal | None:
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


legacy_strategy = _legacy_strategy
