from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.adapters.strategy_d import (
    DEVELOPMENT_PARAMETERS,
    StrategyDAdapter,
    StrategyDParameters,
)
from backtest.v2.models import Candle


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
TF = timedelta(minutes=5)
EPS = Decimal("0.05")


def _history(prices_by_hour: list[Decimal]) -> list[Candle]:
    """Build 5m candles whose hourly resample closes equal prices_by_hour.

    Each hour is 12 flat 5m bars at the hour's price (tiny intrabar range), so the
    resampled 1h close is exactly the scheduled price and the last bar lands on
    minute 55 (the hour-close bar Strategy D acts on).
    """
    candles: list[Candle] = []
    for hour, price in enumerate(prices_by_hour):
        for step in range(12):
            ts = T0 + timedelta(hours=hour) + step * TF
            candles.append(Candle(
                symbol="ETHUSDT",
                timestamp=ts,
                timeframe=TF,
                open=price,
                high=price + EPS,
                low=price - EPS,
                close=price,
                volume=Decimal("100"),
            ))
    return candles


def _dislocation_prices() -> list[Decimal]:
    # 110 flat hours (mean settles at 100), then a gentle 20h decline to 95 so the
    # final close sits well below the lagging EMA relative to a modest ATR.
    flat = [Decimal("100")] * 110
    decline = [Decimal("100") - Decimal("0.25") * Decimal(i) for i in range(1, 21)]
    return flat + decline


class StrategyDAdapterTests(unittest.TestCase):
    def test_deep_dislocation_produces_buy_with_mean_target(self):
        signal = StrategyDAdapter().evaluate(_history(_dislocation_prices()))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.action, "BUY_NOW")
        self.assertLess(signal.stop_loss, signal.reference_price)
        # Mean-reversion target is the mean itself (above the depressed price).
        self.assertGreater(signal.take_profit, signal.reference_price)
        self.assertEqual(signal.take_profit, Decimal(signal.metadata["ema"]))
        self.assertGreaterEqual(
            Decimal(signal.metadata["stretch_atr"]),
            DEVELOPMENT_PARAMETERS.stretch_atr,
        )
        self.assertEqual(signal.metadata["timeframe"], "1h")

    def test_flat_market_gives_no_signal(self):
        flat = [Decimal("100")] * 130
        self.assertIsNone(StrategyDAdapter().evaluate(_history(flat)))

    def test_shallow_dip_below_stretch_threshold_is_rejected(self):
        params = StrategyDParameters(
            stretch_atr=Decimal("50"),  # unreachably deep -> never triggers
            stop_atr=Decimal("4"),
            ema_period=20,
            atr_period=14,
            min_hourly_bars=80,
        )
        self.assertIsNone(StrategyDAdapter(params).evaluate(_history(_dislocation_prices())))

    def test_signal_only_on_hour_close_bar(self):
        history = _history(_dislocation_prices())
        # Drop the final minute-55 bar: the newest bar no longer closes an hour.
        self.assertIsNone(StrategyDAdapter().evaluate(history[:-1]))

    def test_insufficient_history_returns_none(self):
        short = _history([Decimal("100")] * 100)  # 1200 bars < window_size 1500
        self.assertIsNone(StrategyDAdapter().evaluate(short))

    def test_parameters_are_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            DEVELOPMENT_PARAMETERS.stretch_atr = Decimal("1")  # type: ignore[misc]

    def test_parameters_reject_invalid_values(self):
        with self.assertRaises(ValueError):
            StrategyDParameters(
                stretch_atr=Decimal("0"), stop_atr=Decimal("4"),
                ema_period=20, atr_period=14, min_hourly_bars=80,
            )


if __name__ == "__main__":
    unittest.main()
