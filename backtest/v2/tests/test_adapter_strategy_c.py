from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.adapters.strategy_c import (
    DEVELOPMENT_THRESHOLDS,
    StrategyCAdapter,
    StrategyCThresholds,
)
from backtest.v2.models import Candle


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
TF = timedelta(minutes=5)


def trend_history(*, last_volume: Decimal = Decimal("200")) -> list[Candle]:
    candles: list[Candle] = []
    for index in range(203):
        close = Decimal("100") + Decimal(index) * Decimal("0.02")
        open_price = close - Decimal("0.01")
        candles.append(Candle(
            symbol="ETHUSDT",
            timestamp=T0 + index * TF,
            timeframe=TF,
            open=open_price,
            high=close + Decimal("0.05"),
            low=close - Decimal("0.05"),
            close=close,
            volume=last_volume if index == 202 else Decimal("100"),
        ))
    return candles


class StrategyCAdapterTests(unittest.TestCase):
    def test_trend_pullback_confirmation_stop_and_two_r_target(self):
        signal = StrategyCAdapter().evaluate(trend_history())
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.action, "BUY_NOW")
        self.assertLess(signal.stop_loss, signal.reference_price)
        risk = signal.reference_price - signal.stop_loss
        self.assertEqual(signal.take_profit, signal.reference_price + Decimal("2") * risk)
        self.assertEqual(signal.metadata["rr"], "2")
        self.assertEqual(signal.metadata["recent_swing_low"], "103.95")

    def test_ema_slope_and_trend_regime_are_required(self):
        history = trend_history()
        for index, candle in enumerate(history):
            close = Decimal("110") - Decimal(index) * Decimal("0.03")
            history[index] = Candle(
                candle.symbol, candle.timestamp, candle.timeframe,
                close + Decimal("0.01"), close + Decimal("0.05"),
                close - Decimal("0.05"), close, candle.volume,
            )
        self.assertIsNone(StrategyCAdapter().evaluate(history))

    def test_volume_confirmation_is_required(self):
        self.assertIsNone(StrategyCAdapter().evaluate(trend_history(last_volume=Decimal("100"))))

    def test_body_to_atr_filter_rejects_extended_candle(self):
        thresholds = StrategyCThresholds(
            max_atr_pct=Decimal("10"),
            near_ema20_pct=Decimal("10"),
            max_body_to_atr=Decimal("0.01"),
        )
        self.assertIsNone(StrategyCAdapter(thresholds).evaluate(trend_history()))

    def test_pullback_distance_filter_is_required(self):
        thresholds = StrategyCThresholds(
            max_atr_pct=Decimal("10"),
            near_ema20_pct=Decimal("0.00001"),
            max_body_to_atr=Decimal("10"),
        )
        self.assertIsNone(StrategyCAdapter(thresholds).evaluate(trend_history()))

    def test_thresholds_are_immutable_and_development_values_are_exact(self):
        self.assertEqual(DEVELOPMENT_THRESHOLDS.max_atr_pct, Decimal("0.3489067011519851"))
        with self.assertRaises(FrozenInstanceError):
            DEVELOPMENT_THRESHOLDS.max_atr_pct = Decimal("1")  # type: ignore[misc]

    def test_insufficient_history_prevents_future_data_access(self):
        self.assertIsNone(StrategyCAdapter().evaluate(trend_history()[:-1]))


if __name__ == "__main__":
    unittest.main()
