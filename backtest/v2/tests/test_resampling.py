from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.models import Candle
from backtest.v2.resampling import FIVE_MINUTES, ONE_HOUR, resample_5m_to_1h


def bar(index: int, *, hour: int = 0) -> Candle:
    price = Decimal(100 + index)
    return Candle(
        symbol="BTCUSDT",
        timestamp=datetime(2026, 1, 1, hour, index * 5, tzinfo=timezone.utc),
        timeframe=FIVE_MINUTES,
        open=price,
        high=price + Decimal("2"),
        low=price - Decimal("1"),
        close=price + Decimal("1"),
        volume=Decimal(index + 1),
    )


class StrictResamplingTests(unittest.TestCase):
    def test_complete_hour_uses_standard_ohlcv_aggregation(self):
        output = resample_5m_to_1h([bar(i) for i in range(12)])
        self.assertEqual(len(output), 1)
        hour = output[0]
        self.assertEqual(hour.timeframe, ONE_HOUR)
        self.assertEqual(hour.open, Decimal("100"))
        self.assertEqual(hour.high, Decimal("113"))
        self.assertEqual(hour.low, Decimal("99"))
        self.assertEqual(hour.close, Decimal("112"))
        self.assertEqual(hour.volume, Decimal("78"))
        self.assertEqual(hour.close_time, datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc))

    def test_incomplete_hour_is_not_emitted(self):
        output = resample_5m_to_1h([bar(i) for i in range(11)])
        self.assertEqual(output, ())

    def test_hour_with_missing_internal_bar_is_not_emitted(self):
        rows = [bar(i) for i in range(12) if i != 6]
        self.assertEqual(resample_5m_to_1h(rows), ())

    def test_rejects_non_five_minute_input(self):
        invalid = bar(0)
        invalid = Candle(
            symbol=invalid.symbol,
            timestamp=invalid.timestamp,
            timeframe=timedelta(minutes=1),
            open=invalid.open,
            high=invalid.high,
            low=invalid.low,
            close=invalid.close,
            volume=invalid.volume,
        )
        with self.assertRaises(ValueError):
            resample_5m_to_1h([invalid])


if __name__ == "__main__":
    unittest.main()
