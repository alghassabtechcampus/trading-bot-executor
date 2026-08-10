from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.models import Candle
from backtest.v2.validation import DataValidationError, ValidationMode, validate_candles


TF = timedelta(minutes=5)
ORIGIN = datetime(1970, 1, 1, tzinfo=timezone.utc)


def candle(minute: int, **overrides) -> Candle:
    values = dict(
        symbol="BTCUSDT",
        timestamp=datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc),
        timeframe=TF,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("10"),
    )
    values.update(overrides)
    return Candle(**values)


class ValidationTests(unittest.TestCase):
    def validate(self, rows, cutoff=None, mode=ValidationMode.STRICT):
        return validate_candles(
            rows,
            symbol="BTCUSDT",
            expected_timeframe=TF,
            data_cutoff=cutoff or datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),
            alignment_origin=ORIGIN,
            mode=mode,
        )

    def test_valid_series_passes(self):
        result = self.validate([candle(0), candle(5), candle(10)])
        self.assertTrue(result.report.is_valid)
        self.assertEqual(result.report.output_count, 3)

    def test_removes_incomplete_last_candle(self):
        result = self.validate(
            [candle(0), candle(5), candle(10)],
            cutoff=datetime(2026, 1, 1, 0, 12, tzinfo=timezone.utc),
        )
        self.assertTrue(result.report.removed_incomplete_last_candle)
        self.assertEqual([c.timestamp.minute for c in result.candles], [0, 5])

    def test_duplicate_and_gap_fail_strict_validation(self):
        with self.assertRaises(DataValidationError):
            self.validate([candle(0), candle(0), candle(10)])

    def test_invalid_ohlc_and_volume_fail(self):
        bad = candle(0, high=Decimal("98"), volume=Decimal("-1"))
        with self.assertRaises(DataValidationError):
            self.validate([bad])

    def test_warn_mode_returns_issues(self):
        result = self.validate([candle(0), candle(10)], mode=ValidationMode.WARN)
        self.assertEqual(result.report.gap_count, 1)
        self.assertFalse(result.report.is_valid)

    def test_misaligned_timestamp_fails(self):
        bad = candle(0, timestamp=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
        with self.assertRaises(DataValidationError):
            self.validate([bad])


if __name__ == "__main__":
    unittest.main()
