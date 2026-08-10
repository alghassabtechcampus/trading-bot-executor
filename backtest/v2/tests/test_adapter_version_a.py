from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.adapters.version_ab import VersionAAdapter, legacy_strategy
from backtest.v2.models import Candle


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
TF = timedelta(minutes=5)


def parity_history() -> list[Candle]:
    candles = []
    for index in range(200):
        close = Decimal("100") + Decimal(index) * Decimal("0.25")
        candles.append(Candle(
            symbol="ETHUSDT",
            timestamp=T0 + index * TF,
            timeframe=TF,
            open=close - Decimal("0.10"),
            high=close + Decimal("1"),
            low=close - Decimal("1"),
            close=close,
            volume=Decimal("500") if index == 199 else Decimal("10"),
        ))
    return candles


class VersionAParityTests(unittest.TestCase):
    def test_adapter_matches_legacy_action_score_stops_and_metadata(self):
        history = parity_history()
        adapter = VersionAAdapter()
        legacy = legacy_strategy.evaluate_signal(adapter._legacy_window(history))
        actual = adapter.evaluate(history)
        self.assertIsNotNone(actual)
        self.assertEqual(actual.action, legacy["action"])
        self.assertEqual(actual.score, Decimal(str(legacy["score"])))
        self.assertEqual(actual.reference_price, Decimal(str(legacy["price"])))
        self.assertEqual(actual.stop_loss, Decimal(str(legacy["stop_loss"])))
        self.assertEqual(actual.take_profit, Decimal(str(legacy["take_profit"])))
        self.assertEqual(actual.metadata["up_trend"], legacy["up_trend"])
        self.assertEqual(actual.metadata["volume_ratio"], legacy["volume_ratio"])
        self.assertEqual(actual.metadata["atr_percent"], legacy["atr_percent"])
        self.assertEqual(actual.action, "BUY_NOW")

    def test_insufficient_history_matches_legacy_none(self):
        history = parity_history()[:59]
        self.assertIsNone(VersionAAdapter().evaluate(history))


if __name__ == "__main__":
    unittest.main()
