from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.config import (
    ConfigurationError,
    ExecutionProfile,
    FinancialAssumptions,
    RunConfig,
)
from backtest.v2.models import ClosedTrade, ExitReason


def assumptions(**overrides):
    values = {
        "entry_fee_rate": Decimal("0.001"),
        "exit_fee_rate": Decimal("0.001"),
        "entry_slippage_bps": Decimal("2"),
        "exit_slippage_bps": Decimal("2"),
        "spread_bps": Decimal("4"),
        "stop_slippage_bps": Decimal("8"),
        "take_profit_slippage_bps": Decimal("3"),
    }
    values.update(overrides)
    return FinancialAssumptions(**values)


class ConfigurationTests(unittest.TestCase):
    def test_manifest_preserves_explicit_units_and_profile(self):
        config = RunConfig(
            execution_profile=ExecutionProfile.CUSTOM,
            financial_assumptions=assumptions(),
            initial_capital=Decimal("10000"),
            base_currency="USDT",
            max_concurrent_positions=2,
        )
        manifest = config.manifest_values()
        self.assertEqual(manifest["execution_profile"], "CUSTOM")
        self.assertEqual(manifest["financial_assumptions"]["entry_fee_rate"], "0.001")
        self.assertEqual(manifest["financial_assumptions"]["spread_bps"], "4")

    def test_rejects_float_to_prevent_unit_ambiguity(self):
        with self.assertRaises(ConfigurationError):
            assumptions(entry_fee_rate=0.001)

    def test_rejects_invalid_rate_and_bps(self):
        with self.assertRaises(ConfigurationError):
            assumptions(entry_fee_rate=Decimal("1"))
        with self.assertRaises(ConfigurationError):
            assumptions(spread_bps=Decimal("-1"))

    def test_requires_positive_capital_and_concurrency(self):
        with self.assertRaises(ConfigurationError):
            RunConfig(ExecutionProfile.CUSTOM, assumptions(), Decimal("0"), "USDT", 1)
        with self.assertRaises(ConfigurationError):
            RunConfig(ExecutionProfile.CUSTOM, assumptions(), Decimal("100"), "USDT", 0)


class ClosedTradeTests(unittest.TestCase):
    def make_trade(self, **overrides):
        signal = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        entry = signal + timedelta(minutes=5)
        exit_time = entry + timedelta(minutes=30)
        values = dict(
            entry_signal_time=signal,
            entry_fill_time=entry,
            exit_fill_time=exit_time,
            entry_reference_price=Decimal("100"),
            entry_fill_price=Decimal("100.05"),
            exit_reference_price=Decimal("102"),
            exit_fill_price=Decimal("101.95"),
            quantity=Decimal("1"),
            entry_notional=Decimal("100.05"),
            exit_notional=Decimal("101.95"),
            gross_pnl=Decimal("1.90"),
            net_pnl=Decimal("1.69"),
            net_return_pct=Decimal("1.6892"),
            entry_fee=Decimal("0.10"),
            exit_fee=Decimal("0.11"),
            spread_cost=Decimal("0.04"),
            slippage_cost=Decimal("0.06"),
            holding_duration=timedelta(minutes=30),
            exit_reason=ExitReason.TAKE_PROFIT,
            mfe=Decimal("2.2"),
            mae=Decimal("-0.4"),
            intrabar_ambiguous=False,
            strategy_name="Version A",
            strategy_version="legacy-parameters",
            symbol="BTCUSDT",
        )
        values.update(overrides)
        return ClosedTrade(**values)

    def test_contains_auditable_trade_fields(self):
        trade = self.make_trade()
        self.assertEqual(trade.exit_reason, ExitReason.TAKE_PROFIT)
        self.assertEqual(trade.holding_duration, timedelta(minutes=30))
        self.assertEqual(trade.entry_signal_time.minute, 0)
        self.assertEqual(trade.entry_fill_time.minute, 5)

    def test_rejects_fill_before_signal(self):
        signal = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            self.make_trade(entry_fill_time=signal - timedelta(minutes=5))

    def test_rejects_inconsistent_holding_duration(self):
        with self.assertRaises(ValueError):
            self.make_trade(holding_duration=timedelta(minutes=25))


if __name__ == "__main__":
    unittest.main()
