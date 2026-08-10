from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.config import FinancialAssumptions, IntrabarPolicy
from backtest.v2.execution import (
    execute_intrabar_exit,
    execute_market_exit,
    execute_pending_entry,
    stop_fill,
    take_profit_fill,
)
from backtest.v2.models import (
    Candle,
    ExecutionReason,
    ExitReason,
    PendingOrder,
    PendingOrderStatus,
    RejectionReason,
    SignalSide,
)


UTC = timezone.utc
SIGNAL_TIME = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
ELIGIBLE_FROM = datetime(2026, 1, 1, 10, 5, tzinfo=UTC)
TF = timedelta(minutes=5)


def assumptions(**overrides) -> FinancialAssumptions:
    values = {
        "entry_fee_rate": Decimal("0"),
        "exit_fee_rate": Decimal("0"),
        "entry_slippage_bps": Decimal("0"),
        "exit_slippage_bps": Decimal("0"),
        "spread_bps": Decimal("0"),
        "stop_slippage_bps": Decimal("0"),
        "take_profit_slippage_bps": Decimal("0"),
    }
    values.update(overrides)
    return FinancialAssumptions(**values)


def order(**overrides) -> PendingOrder:
    values = dict(
        order_id="order-1",
        symbol="BTCUSDT",
        side=SignalSide.BUY,
        signal_time=SIGNAL_TIME,
        eligible_from=ELIGIBLE_FROM,
        strategy_name="Version A",
        strategy_version="legacy-parameters",
        reference_price=Decimal("99"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        score=Decimal("50"),
        priority=1,
        metadata={"source": "test"},
        status=PendingOrderStatus.PENDING,
        quantity=Decimal("2"),
    )
    values.update(overrides)
    return PendingOrder(**values)


def bar(
    timestamp: datetime = ELIGIBLE_FROM,
    *,
    open_price: str = "100",
    high: str = "105",
    low: str = "95",
    close: str = "102",
) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        timestamp=timestamp,
        timeframe=TF,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
    )


class PendingOrderTests(unittest.TestCase):
    def test_eligible_from_must_be_later_than_signal_time(self):
        with self.assertRaises(ValueError):
            order(eligible_from=SIGNAL_TIME)

    def test_pending_metadata_is_read_only(self):
        pending = order()
        with self.assertRaises(TypeError):
            pending.metadata["new"] = "value"


class CausalEntryTests(unittest.TestCase):
    def test_signal_at_t_executes_only_at_eligible_next_open(self):
        signal_bar = bar(
            SIGNAL_TIME,
            open_price="1",
            high="2",
            low="0.5",
            close="1.5",
        )
        next_bar = bar(ELIGIBLE_FROM, open_price="100")
        result = execute_pending_entry(order(), [signal_bar, next_bar], assumptions())
        self.assertIsNotNone(result.fill)
        self.assertEqual(result.fill.timestamp, ELIGIBLE_FROM)
        self.assertEqual(result.fill.reference_price, Decimal("100"))
        self.assertGreater(result.fill.timestamp, SIGNAL_TIME)

    def test_no_next_bar_is_deterministically_rejected(self):
        result = execute_pending_entry(order(), [bar(SIGNAL_TIME)], assumptions())
        self.assertIsNone(result.fill)
        self.assertEqual(result.rejection.reason, RejectionReason.NO_NEXT_BAR)
        self.assertEqual(result.rejection.timestamp, ELIGIBLE_FROM)

    def test_missing_quantity_is_invalid_size_not_sized_here(self):
        result = execute_pending_entry(order(quantity=None), [bar()], assumptions())
        self.assertEqual(result.rejection.reason, RejectionReason.INVALID_SIZE)


class ConfigurableCostTests(unittest.TestCase):
    def test_zero_cost_configuration_has_no_hidden_cost(self):
        result = execute_pending_entry(order(), [bar(open_price="100")], assumptions())
        fill = result.fill
        self.assertEqual(fill.fill_price, Decimal("100"))
        self.assertEqual(fill.spread_cost, Decimal("0"))
        self.assertEqual(fill.slippage_cost, Decimal("0"))
        self.assertEqual(fill.fee, Decimal("0"))

    def test_spread_increases_buy_and_reduces_sell_fill(self):
        configured = assumptions(spread_bps=Decimal("20"))
        buy = execute_pending_entry(order(), [bar(open_price="100")], configured).fill
        sell = execute_market_exit(
            order_id="exit-1",
            symbol="BTCUSDT",
            timestamp=ELIGIBLE_FROM,
            reference_price=Decimal("100"),
            quantity=Decimal("2"),
            assumptions=configured,
        )
        self.assertGreater(buy.fill_price, Decimal("100"))
        self.assertLess(sell.fill_price, Decimal("100"))

    def test_positive_slippage_worsens_buy_and_sell(self):
        zero = assumptions()
        adverse = assumptions(
            entry_slippage_bps=Decimal("10"),
            exit_slippage_bps=Decimal("10"),
        )
        buy_zero = execute_pending_entry(order(), [bar()], zero).fill
        buy_adverse = execute_pending_entry(order(), [bar()], adverse).fill
        sell_zero = execute_market_exit(
            order_id="exit-1", symbol="BTCUSDT", timestamp=ELIGIBLE_FROM,
            reference_price=Decimal("100"), quantity=Decimal("2"), assumptions=zero,
        )
        sell_adverse = execute_market_exit(
            order_id="exit-1", symbol="BTCUSDT", timestamp=ELIGIBLE_FROM,
            reference_price=Decimal("100"), quantity=Decimal("2"), assumptions=adverse,
        )
        self.assertGreater(buy_adverse.fill_price, buy_zero.fill_price)
        self.assertLess(sell_adverse.fill_price, sell_zero.fill_price)

    def test_fees_use_actual_fill_notional(self):
        configured = assumptions(
            entry_fee_rate=Decimal("0.01"),
            exit_fee_rate=Decimal("0.02"),
            spread_bps=Decimal("20"),
        )
        buy = execute_pending_entry(order(), [bar(open_price="100")], configured).fill
        sell = execute_market_exit(
            order_id="exit-1", symbol="BTCUSDT", timestamp=ELIGIBLE_FROM,
            reference_price=Decimal("110"), quantity=Decimal("2"),
            assumptions=configured,
        )
        self.assertEqual(buy.fee, buy.notional * Decimal("0.01"))
        self.assertEqual(sell.fee, sell.notional * Decimal("0.02"))
        self.assertTrue(all(isinstance(value, Decimal) for value in (
            buy.fill_price, buy.notional, buy.spread_cost, buy.slippage_cost, buy.fee,
        )))


class StopExecutionTests(unittest.TestCase):
    def test_stop_price_rejects_float_units(self):
        with self.assertRaises(ValueError):
            stop_fill(
                order_id="stop-1", symbol="BTCUSDT", candle=bar(),
                stop_loss=95.0, quantity=Decimal("2"), assumptions=assumptions(),
            )

    def test_gap_down_below_stop_uses_open_reference(self):
        fill = stop_fill(
            order_id="stop-1", symbol="BTCUSDT",
            candle=bar(open_price="90", high="92", low="88", close="89"),
            stop_loss=Decimal("95"), quantity=Decimal("2"),
            assumptions=assumptions(stop_slippage_bps=Decimal("10")),
        )
        self.assertEqual(fill.reference_price, Decimal("90"))
        self.assertLess(fill.fill_price, Decimal("90"))
        self.assertEqual(fill.execution_reason, ExecutionReason.STOP_LOSS)

    def test_intrabar_stop_touch_uses_stop_reference(self):
        fill = stop_fill(
            order_id="stop-1", symbol="BTCUSDT",
            candle=bar(open_price="100", high="103", low="94", close="98"),
            stop_loss=Decimal("95"), quantity=Decimal("2"), assumptions=assumptions(),
        )
        self.assertEqual(fill.reference_price, Decimal("95"))


class TakeProfitExecutionTests(unittest.TestCase):
    def test_gap_up_above_target_uses_open_reference(self):
        fill = take_profit_fill(
            order_id="tp-1", symbol="BTCUSDT",
            candle=bar(open_price="110", high="112", low="109", close="111"),
            take_profit=Decimal("105"), quantity=Decimal("2"),
            assumptions=assumptions(take_profit_slippage_bps=Decimal("10")),
        )
        self.assertEqual(fill.reference_price, Decimal("110"))
        self.assertLess(fill.fill_price, Decimal("110"))
        self.assertEqual(fill.execution_reason, ExecutionReason.TAKE_PROFIT)

    def test_intrabar_target_touch_uses_target_reference(self):
        fill = take_profit_fill(
            order_id="tp-1", symbol="BTCUSDT",
            candle=bar(open_price="100", high="106", low="99", close="104"),
            take_profit=Decimal("105"), quantity=Decimal("2"), assumptions=assumptions(),
        )
        self.assertEqual(fill.reference_price, Decimal("105"))


class IntrabarAmbiguityTests(unittest.TestCase):
    def test_default_policy_is_stop_first_when_both_hit(self):
        decision = execute_intrabar_exit(
            order_id="exit-1", symbol="BTCUSDT",
            candle=bar(open_price="100", high="110", low="90", close="102"),
            stop_loss=Decimal("95"), take_profit=Decimal("105"),
            quantity=Decimal("2"), assumptions=assumptions(),
        )
        self.assertEqual(decision.exit_reason, ExitReason.STOP_LOSS)
        self.assertTrue(decision.intrabar_ambiguous)
        self.assertEqual(decision.intrabar_policy, IntrabarPolicy.STOP_FIRST)
        self.assertEqual(decision.fill.reference_price, Decimal("95"))


if __name__ == "__main__":
    unittest.main()
