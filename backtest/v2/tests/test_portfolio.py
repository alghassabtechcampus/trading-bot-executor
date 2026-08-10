from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.config import FinancialAssumptions
from backtest.v2.models import Candle, ExecutionReason, Fill, RejectionReason, SignalSide
from backtest.v2.portfolio import EquityEventType, PortfolioError, PortfolioState


UTC = timezone.utc
T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
T2 = T1 + timedelta(minutes=5)
TF = timedelta(minutes=5)


def assumptions(**overrides) -> FinancialAssumptions:
    values = {
        "entry_fee_rate": Decimal("0.01"),
        "exit_fee_rate": Decimal("0.01"),
        "entry_slippage_bps": Decimal("0"),
        "exit_slippage_bps": Decimal("0"),
        "spread_bps": Decimal("0"),
        "stop_slippage_bps": Decimal("0"),
        "take_profit_slippage_bps": Decimal("0"),
    }
    values.update(overrides)
    return FinancialAssumptions(**values)


def fill(
    *,
    side: SignalSide,
    price: str,
    quantity: str = "1",
    timestamp: datetime = T1,
    symbol: str = "BTCUSDT",
    fee_rate: str = "0.01",
    spread_cost: str = "0",
    slippage_cost: str = "0",
    notional_override: str | None = None,
) -> Fill:
    price_value = Decimal(price)
    quantity_value = Decimal(quantity)
    notional = (
        Decimal(notional_override)
        if notional_override is not None
        else price_value * quantity_value
    )
    return Fill(
        order_id=f"{side.value.lower()}-{symbol}-{timestamp.isoformat()}",
        symbol=symbol,
        side=side,
        timestamp=timestamp,
        reference_price=price_value,
        fill_price=price_value,
        quantity=quantity_value,
        notional=notional,
        spread_cost=Decimal(spread_cost),
        slippage_cost=Decimal(slippage_cost),
        fee=notional * Decimal(fee_rate),
        execution_reason=(
            ExecutionReason.ENTRY_MARKET
            if side is SignalSide.BUY
            else ExecutionReason.EXIT_MARKET
        ),
    )


def candle(symbol: str, close: str, timestamp: datetime = T1) -> Candle:
    close_value = Decimal(close)
    return Candle(
        symbol=symbol,
        timestamp=timestamp - TF,
        timeframe=TF,
        open=close_value,
        high=close_value,
        low=close_value,
        close=close_value,
        volume=Decimal("1"),
    )


def portfolio(
    *,
    capital: str = "1000",
    configured: FinancialAssumptions | None = None,
) -> PortfolioState:
    return PortfolioState.create(
        initial_capital=Decimal(capital),
        financial_assumptions=configured or assumptions(),
        timestamp=T0,
    )


def open_btc(state: PortfolioState, entry: Fill | None = None):
    return state.open_position(
        entry or fill(side=SignalSide.BUY, price="100"),
        stop_loss=Decimal("90"),
        take_profit=Decimal("120"),
        strategy_name="Version A",
        strategy_version="legacy-parameters",
        metadata={"test": True},
    )


class InitialAndOpenAccountingTests(unittest.TestCase):
    def test_initial_state_and_equity_point(self):
        state = portfolio()
        self.assertEqual(state.available_cash, Decimal("1000"))
        self.assertEqual(state.reserved_cash, Decimal("0"))
        self.assertEqual(state.equity, Decimal("1000"))
        self.assertEqual(state.realized_pnl, Decimal("0"))
        self.assertEqual(len(state.equity_curve), 1)
        self.assertEqual(state.equity_curve[0].event_type, EquityEventType.INITIAL)

    def test_entry_notional_and_fee_reduce_cash(self):
        state = portfolio()
        self.assertIsNone(open_btc(state))
        self.assertEqual(state.available_cash, Decimal("899"))
        self.assertEqual(state.positions["BTCUSDT"].entry_notional, Decimal("100"))
        self.assertEqual(state.total_fees, Decimal("1"))
        self.assertGreaterEqual(state.available_cash, Decimal("0"))

    def test_entry_fee_is_part_of_cash_requirement(self):
        state = portfolio(capital="100.5")
        rejection = open_btc(state)
        self.assertEqual(rejection, RejectionReason.INSUFFICIENT_CASH)
        self.assertEqual(state.available_cash, Decimal("100.5"))
        self.assertEqual(state.total_fees, Decimal("0"))

    def test_duplicate_symbol_rejected_before_spending_cash(self):
        state = portfolio()
        open_btc(state)
        cash_before = state.available_cash
        rejection = open_btc(state)
        self.assertEqual(rejection, RejectionReason.DUPLICATE_SYMBOL)
        self.assertEqual(state.available_cash, cash_before)

    def test_simultaneous_entries_compete_for_same_cash_in_call_order(self):
        state = portfolio(capital="150")
        first = open_btc(state)
        second_fill = fill(side=SignalSide.BUY, price="100", symbol="ETHUSDT")
        second = state.open_position(
            second_fill,
            stop_loss=Decimal("90"), take_profit=Decimal("120"),
            strategy_name="Version A", strategy_version="1",
        )
        self.assertIsNone(first)
        self.assertEqual(second, RejectionReason.INSUFFICIENT_CASH)
        self.assertEqual(set(state.positions), {"BTCUSDT"})
        self.assertEqual(state.available_cash, Decimal("49"))


class CloseAccountingTests(unittest.TestCase):
    def test_manual_profitable_round_trip(self):
        state = portfolio()
        open_btc(state)
        pnl = state.close_position(fill(side=SignalSide.SELL, price="110", timestamp=T2))
        self.assertEqual(pnl, Decimal("7.90"))
        self.assertEqual(state.available_cash, Decimal("1007.90"))
        self.assertEqual(state.realized_pnl, Decimal("7.90"))
        self.assertEqual(state.equity, Decimal("1007.90"))
        self.assertEqual(state.total_fees, Decimal("2.10"))
        self.assertEqual(state.positions, {})

    def test_losing_round_trip(self):
        state = portfolio()
        open_btc(state)
        pnl = state.close_position(fill(side=SignalSide.SELL, price="90", timestamp=T2))
        self.assertEqual(pnl, Decimal("-11.90"))
        self.assertEqual(state.available_cash, Decimal("988.10"))
        self.assertEqual(state.realized_pnl, Decimal("-11.90"))

    def test_close_non_existing_position_raises(self):
        with self.assertRaisesRegex(PortfolioError, "no open position"):
            portfolio().close_position(fill(side=SignalSide.SELL, price="100"))

    def test_over_close_and_partial_close_are_rejected(self):
        state = portfolio()
        open_btc(state)
        with self.assertRaisesRegex(PortfolioError, "exceeds"):
            state.close_position(fill(side=SignalSide.SELL, price="100", quantity="2"))
        with self.assertRaisesRegex(PortfolioError, "partial close unsupported"):
            state.close_position(fill(side=SignalSide.SELL, price="100", quantity="0.5"))

    def test_non_positive_close_quantity_is_rejected_by_fill_model(self):
        with self.assertRaisesRegex(ValueError, "quantity"):
            fill(side=SignalSide.SELL, price="100", quantity="0")

    def test_invalid_fill_notional_is_rejected(self):
        state = portfolio()
        invalid = fill(
            side=SignalSide.BUY,
            price="100",
            notional_override="99",
            fee_rate="0.01",
        )
        with self.assertRaisesRegex(PortfolioError, "notional"):
            open_btc(state, invalid)


class MarkToMarketAndEquityTests(unittest.TestCase):
    def test_equity_uses_estimated_liquidation_value(self):
        configured = assumptions(
            exit_fee_rate=Decimal("0.01"),
            spread_bps=Decimal("20"),
            exit_slippage_bps=Decimal("10"),
        )
        state = portfolio(configured=configured)
        open_btc(state)
        state.mark_to_market(timestamp=T2, candles=[candle("BTCUSDT", "110", T2)])

        half_spread_price = Decimal("110") * Decimal("0.999")
        estimated_fill = half_spread_price * Decimal("0.999")
        liquidation = estimated_fill * Decimal("0.99")
        self.assertEqual(state.equity, Decimal("899") + liquidation)
        self.assertEqual(
            state.unrealized_pnl,
            liquidation - Decimal("100") - Decimal("1"),
        )

    def test_missing_symbol_keeps_last_historical_mark_without_cross_symbol_fill(self):
        state = portfolio()
        open_btc(state)
        state.mark_to_market(timestamp=T2, candles=[candle("ETHUSDT", "999", T2)])
        self.assertEqual(state.positions["BTCUSDT"].current_mark_price, Decimal("100"))

    def test_future_or_wrong_timestamp_mark_is_rejected(self):
        state = portfolio()
        open_btc(state)
        future = candle("BTCUSDT", "110", T2 + TF)
        with self.assertRaisesRegex(PortfolioError, "current timestamp"):
            state.mark_to_market(timestamp=T2, candles=[future])

    def test_drawdown_and_recovery_derive_from_equity_curve(self):
        configured = assumptions(entry_fee_rate=Decimal("0"), exit_fee_rate=Decimal("0"))
        state = portfolio(configured=configured)
        open_btc(state, fill(side=SignalSide.BUY, price="100", fee_rate="0"))
        high = state.mark_to_market(timestamp=T2, candles=[candle("BTCUSDT", "110", T2)])
        self.assertEqual(high.equity, Decimal("1010"))
        self.assertEqual(high.drawdown_value, Decimal("0"))

        t3 = T2 + TF
        low = state.mark_to_market(timestamp=t3, candles=[candle("BTCUSDT", "90", t3)])
        self.assertEqual(low.drawdown_value, Decimal("-20"))
        self.assertEqual(low.drawdown_pct, Decimal("-20") / Decimal("1010") * Decimal("100"))

        t4 = t3 + TF
        recovered = state.mark_to_market(
            timestamp=t4, candles=[candle("BTCUSDT", "110", t4)]
        )
        self.assertEqual(recovered.drawdown_value, Decimal("0"))
        self.assertEqual(recovered.drawdown_pct, Decimal("0"))

    def test_same_timestamp_points_have_deterministic_sequences(self):
        state = portfolio()
        open_btc(state)
        state.mark_to_market(timestamp=T1, candles=[candle("BTCUSDT", "100", T1)])
        points = [point for point in state.equity_curve if point.timestamp == T1]
        self.assertEqual([point.sequence for point in points], sorted(point.sequence for point in points))
        self.assertEqual(
            [point.event_type for point in points],
            [EquityEventType.ENTRY_FILL, EquityEventType.TIMESTAMP],
        )


class CostAccumulationTests(unittest.TestCase):
    def test_actual_fill_costs_accumulate_separately(self):
        state = portfolio()
        entry = fill(
            side=SignalSide.BUY, price="100",
            spread_cost="0.2", slippage_cost="0.3",
        )
        open_btc(state, entry)
        exit_fill = fill(
            side=SignalSide.SELL, price="110", timestamp=T2,
            spread_cost="0.4", slippage_cost="0.5",
        )
        state.close_position(exit_fill)
        self.assertEqual(state.total_fees, Decimal("2.10"))
        self.assertEqual(state.total_spread_cost, Decimal("0.6"))
        self.assertEqual(state.total_slippage_cost, Decimal("0.8"))

    def test_end_point_records_marked_state_without_forced_close(self):
        state = portfolio()
        open_btc(state)
        point = state.record_end(T2)
        self.assertEqual(point.event_type, EquityEventType.END)
        self.assertEqual(point.open_positions_count, 1)


if __name__ == "__main__":
    unittest.main()
