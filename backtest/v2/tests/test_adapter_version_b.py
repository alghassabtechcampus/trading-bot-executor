from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.adapters.version_ab import VersionBAdapter, legacy_strategy
from backtest.v2.models import Candle


TF = timedelta(minutes=5)
CURRENT_CLOSE = datetime(2026, 2, 1, tzinfo=timezone.utc)


def rising_history(symbol: str, count: int, *, end_close=CURRENT_CLOSE) -> list[Candle]:
    first_open = end_close - count * TF
    candles = []
    for index in range(count):
        close = Decimal("100") + Decimal(index) * Decimal("0.25")
        candles.append(Candle(
            symbol=symbol,
            timestamp=first_open + index * TF,
            timeframe=TF,
            open=close - Decimal("0.10"),
            high=close + Decimal("1"),
            low=close - Decimal("1"),
            close=close,
            volume=Decimal("500") if index == count - 1 else Decimal("10"),
        ))
    return candles


class VersionBParityTests(unittest.TestCase):
    def test_adapter_matches_legacy_signal_and_btc_filter(self):
        target = rising_history("ETHUSDT", 200)
        btc = rising_history("BTCUSDT", 500)
        adapter = VersionBAdapter()
        legacy_signal = legacy_strategy.evaluate_signal(adapter._legacy_window(target))
        legacy_filter = legacy_strategy.evaluate_btc_filter(
            adapter._legacy_window(btc),
            adapter._legacy_candles(btc[-adapter.btc_long_window_size:]),
        )
        actual = adapter.evaluate(target, btc_history=btc)
        self.assertIsNotNone(actual)
        expected_action = legacy_signal["action"]
        if expected_action == "BUY_NOW" and not legacy_filter:
            expected_action = "IGNORE"
        self.assertEqual(actual.action, expected_action)
        self.assertEqual(actual.score, Decimal(str(legacy_signal["score"])))
        self.assertEqual(actual.stop_loss, Decimal(str(legacy_signal["stop_loss"])))
        self.assertEqual(actual.take_profit, Decimal(str(legacy_signal["take_profit"])))
        self.assertEqual(actual.metadata["btc_filter"], legacy_filter)
        self.assertTrue(actual.metadata["btc_timestamp_aligned"])
        self.assertTrue(legacy_filter)
        self.assertEqual(actual.action, "BUY_NOW")

    def test_missing_same_timestamp_btc_is_not_forward_or_nearest_filled(self):
        target = rising_history("ETHUSDT", 200)
        btc_missing_current = rising_history("BTCUSDT", 500)[:-1]
        actual = VersionBAdapter().evaluate(target, btc_history=btc_missing_current)
        self.assertEqual(actual.action, "IGNORE")
        self.assertFalse(actual.metadata["btc_filter"])
        self.assertFalse(actual.metadata["btc_timestamp_aligned"])
        self.assertIsNone(actual.metadata["btc_close_time"])

    def test_future_btc_candle_is_never_used_for_current_signal(self):
        target = rising_history("ETHUSDT", 200)
        btc = rising_history("BTCUSDT", 500)
        future = Candle(
            symbol="BTCUSDT",
            timestamp=CURRENT_CLOSE,
            timeframe=TF,
            open=Decimal("225"), high=Decimal("226"), low=Decimal("224"),
            close=Decimal("225.5"), volume=Decimal("10"),
        )
        actual = VersionBAdapter().evaluate(target, btc_history=btc + [future])
        self.assertEqual(actual.action, "IGNORE")
        self.assertFalse(actual.metadata["btc_filter"])
        self.assertFalse(actual.metadata["btc_timestamp_aligned"])

    def test_missing_btc_context_rejects_buy_signal(self):
        actual = VersionBAdapter().evaluate(
            rising_history("ETHUSDT", 200), btc_history=None
        )
        self.assertEqual(actual.action, "IGNORE")
        self.assertFalse(actual.metadata["btc_filter"])


if __name__ == "__main__":
    unittest.main()
