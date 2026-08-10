from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.adapters.base import AdapterSignal
from backtest.v2.config import (
    ExecutionProfile,
    FinancialAssumptions,
    PositionSizingMode,
    RunConfig,
)
from backtest.v2.engine import IntegrationEngine, IntegrationRunConfig
from backtest.v2.models import Candle, ExitReason, RejectionReason


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
TF = timedelta(minutes=5)


def assumptions() -> FinancialAssumptions:
    return FinancialAssumptions(
        entry_fee_rate=Decimal("0"), exit_fee_rate=Decimal("0"),
        entry_slippage_bps=Decimal("0"), exit_slippage_bps=Decimal("0"),
        spread_bps=Decimal("0"), stop_slippage_bps=Decimal("0"),
        take_profit_slippage_bps=Decimal("0"),
    )


def run_config(max_positions=2, capital=Decimal("1000")) -> RunConfig:
    return RunConfig(
        execution_profile=ExecutionProfile.CUSTOM,
        financial_assumptions=assumptions(),
        initial_capital=capital,
        base_currency="USDT",
        max_concurrent_positions=max_positions,
        position_sizing_mode=PositionSizingMode.FIXED_NOTIONAL,
        fixed_notional=Decimal("100"),
        risk_per_trade=None,
    )


def candle(symbol, index, *, open_="100", high="101", low="99", close="100"):
    return Candle(
        symbol=symbol, timestamp=T0 + index * TF, timeframe=TF,
        open=Decimal(open_), high=Decimal(high), low=Decimal(low),
        close=Decimal(close), volume=Decimal("10"),
    )


class ScheduledAdapter:
    strategy_name = "Version A"
    strategy_version = "test-fixture"
    max_hold_minutes = 90
    window_size = 200

    def __init__(self, signal_times):
        self.signal_times = set(signal_times)

    def evaluate(self, history):
        current = history[-1]
        if (current.symbol, current.close_time) not in self.signal_times:
            return None
        return AdapterSignal(
            action="BUY_NOW", score=Decimal("50"),
            reference_price=current.close,
            stop_loss=Decimal("95"), take_profit=Decimal("110"),
            metadata={"fixture": True},
        )


class IntegrationFlowTests(unittest.TestCase):
    def test_simultaneous_signals_next_open_tp_sl_and_max_concurrent(self):
        symbols = ("AAA", "BBB", "CCC")
        signal_time = T0 + TF
        adapter = ScheduledAdapter({(symbol, signal_time) for symbol in symbols})
        data = {
            "AAA": [candle("AAA", 0), candle("AAA", 1, high="111", low="99", close="108")],
            "BBB": [candle("BBB", 0), candle("BBB", 1, high="101", low="94", close="96")],
            "CCC": [candle("CCC", 0), candle("CCC", 1)],
        }
        result = IntegrationEngine(
            IntegrationRunConfig(
                run=run_config(max_positions=2), symbols=symbols,
                start=T0, end=T0 + 2 * TF,
            ), adapter,
        ).run(data)
        self.assertEqual(result.signals, 3)
        self.assertEqual(result.orders, 2)
        self.assertEqual(result.fills, 4)
        self.assertEqual(result.summary.total_trades, 2)
        self.assertEqual(
            {trade.exit_reason for trade in result.trades},
            {ExitReason.TAKE_PROFIT, ExitReason.STOP_LOSS},
        )
        self.assertEqual(
            result.summary.rejection_counts[RejectionReason.MAX_CONCURRENT_REACHED], 1
        )
        self.assertTrue(all(trade.entry_fill_time == signal_time for trade in result.trades))

    def test_same_entry_bar_both_levels_uses_stop_first(self):
        signal_time = T0 + TF
        data = {"AAA": [
            candle("AAA", 0),
            candle("AAA", 1, high="111", low="94", close="100"),
        ]}
        result = IntegrationEngine(
            IntegrationRunConfig(
                run=run_config(1), symbols=("AAA",), start=T0, end=T0 + 2 * TF,
            ),
            ScheduledAdapter({("AAA", signal_time)}),
        ).run(data)
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, ExitReason.STOP_LOSS)
        self.assertTrue(trade.intrabar_ambiguous)
        self.assertEqual(trade.holding_duration, TF)
        self.assertEqual(trade.mfe, Decimal("0"))
        self.assertEqual(trade.mae, Decimal("-5.00"))

    def test_max_hold_starts_at_actual_fill_time(self):
        signal_time = T0 + TF
        data = {"AAA": [candle("AAA", index) for index in range(19)]}
        result = IntegrationEngine(
            IntegrationRunConfig(
                run=run_config(1), symbols=("AAA",),
                start=T0, end=T0 + 19 * TF,
            ),
            ScheduledAdapter({("AAA", signal_time)}),
        ).run(data)
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, ExitReason.MAX_HOLD)
        self.assertEqual(trade.entry_fill_time, signal_time)
        self.assertEqual(trade.holding_duration, timedelta(minutes=90))

    def test_last_bar_signal_is_rejected_no_next_bar(self):
        signal_time = T0 + TF
        result = IntegrationEngine(
            IntegrationRunConfig(
                run=run_config(1), symbols=("AAA",),
                start=T0, end=signal_time,
            ),
            ScheduledAdapter({("AAA", signal_time)}),
        ).run({"AAA": [candle("AAA", 0)]})
        self.assertEqual(result.orders, 1)
        self.assertEqual(result.summary.rejection_counts[RejectionReason.NO_NEXT_BAR], 1)
        self.assertEqual(result.fills, 0)

    def test_open_position_rejects_repeated_signal_as_duplicate(self):
        first_signal = T0 + TF
        second_signal = T0 + 2 * TF
        data = {"AAA": [candle("AAA", index) for index in range(4)]}
        result = IntegrationEngine(
            IntegrationRunConfig(
                run=run_config(2), symbols=("AAA",), start=T0, end=T0 + 4 * TF,
            ),
            ScheduledAdapter({("AAA", first_signal), ("AAA", second_signal)}),
        ).run(data)
        self.assertEqual(result.summary.rejection_counts[RejectionReason.DUPLICATE_SYMBOL], 1)
        self.assertEqual(result.trades[0].exit_reason, ExitReason.END_OF_TEST)

    def test_gap_at_entry_can_be_rejected_for_insufficient_cash(self):
        signal_time = T0 + TF
        data = {"AAA": [
            candle("AAA", 0),
            candle("AAA", 1, open_="200", high="201", low="199", close="200"),
        ]}
        result = IntegrationEngine(
            IntegrationRunConfig(
                run=run_config(1, capital=Decimal("100")),
                symbols=("AAA",), start=T0, end=T0 + 2 * TF,
            ),
            ScheduledAdapter({("AAA", signal_time)}),
        ).run(data)
        self.assertEqual(result.summary.rejection_counts[RejectionReason.INSUFFICIENT_CASH], 1)
        self.assertEqual(result.summary.total_trades, 0)
        self.assertEqual(result.fills, 0)

    def test_open_position_is_closed_at_end_of_test(self):
        signal_time = T0 + TF
        data = {"AAA": [candle("AAA", 0), candle("AAA", 1)]}
        result = IntegrationEngine(
            IntegrationRunConfig(
                run=run_config(1), symbols=("AAA",), start=T0, end=T0 + 2 * TF,
            ),
            ScheduledAdapter({("AAA", signal_time)}),
        ).run(data)
        self.assertEqual(result.summary.total_trades, 1)
        self.assertEqual(result.trades[0].exit_reason, ExitReason.END_OF_TEST)
        self.assertEqual(result.fills, 2)
        self.assertEqual(result.summary.final_equity, Decimal("1000"))


if __name__ == "__main__":
    unittest.main()
