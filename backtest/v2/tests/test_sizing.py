from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.config import (
    ExecutionProfile,
    FinancialAssumptions,
    PositionSizingMode,
    RunConfig,
)
from backtest.v2.models import PendingOrder, PendingOrderStatus, RejectionReason, SignalSide
from backtest.v2.portfolio import PortfolioState
from backtest.v2.sizing import InstrumentConstraints, PositionSizer, SizingError


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def assumptions(**overrides) -> FinancialAssumptions:
    values = dict(
        entry_fee_rate=Decimal("0"),
        exit_fee_rate=Decimal("0"),
        entry_slippage_bps=Decimal("0"),
        exit_slippage_bps=Decimal("0"),
        spread_bps=Decimal("0"),
        stop_slippage_bps=Decimal("0"),
        take_profit_slippage_bps=Decimal("0"),
    )
    values.update(overrides)
    return FinancialAssumptions(**values)


def config(mode, *, financial=None, fixed=None, risk=None) -> RunConfig:
    return RunConfig(
        execution_profile=ExecutionProfile.CUSTOM,
        financial_assumptions=financial or assumptions(),
        initial_capital=Decimal("1000"),
        base_currency="USDT",
        max_concurrent_positions=2,
        position_sizing_mode=mode,
        fixed_notional=fixed,
        risk_per_trade=risk,
    )


def portfolio(financial=None, capital=Decimal("1000")) -> PortfolioState:
    return PortfolioState.create(
        initial_capital=capital,
        financial_assumptions=financial or assumptions(),
        timestamp=T0,
    )


def order(stop=Decimal("95"), side=SignalSide.BUY) -> PendingOrder:
    return PendingOrder(
        order_id="order-1",
        symbol="BTCUSDT",
        side=side,
        signal_time=T0,
        eligible_from=T0 + timedelta(minutes=5),
        strategy_name="test",
        strategy_version="1",
        reference_price=Decimal("100"),
        stop_loss=stop,
        take_profit=Decimal("110"),
        score=None,
        priority=None,
        metadata={},
        status=PendingOrderStatus.PENDING,
    )


class FixedNotionalTests(unittest.TestCase):
    def test_fixed_notional_excludes_fee_and_reports_cash_required(self):
        financial = assumptions(entry_fee_rate=Decimal("0.01"))
        state = portfolio(financial)
        result = PositionSizer(config(
            PositionSizingMode.FIXED_NOTIONAL,
            financial=financial,
            fixed=Decimal("100"),
        )).size(portfolio=state, order=order(None), estimated_entry_fill_price=Decimal("50"))
        self.assertEqual(result.raw_quantity, Decimal("2"))
        self.assertEqual(result.final_quantity, Decimal("2"))
        self.assertEqual(result.estimated_entry_fee, Decimal("1.00"))
        self.assertEqual(result.cash_required, Decimal("101.00"))

    def test_fixed_notional_is_cash_capped_including_entry_fee(self):
        financial = assumptions(entry_fee_rate=Decimal("0.01"))
        state = portfolio(financial, Decimal("50"))
        result = PositionSizer(config(
            PositionSizingMode.FIXED_NOTIONAL,
            financial=financial,
            fixed=Decimal("100"),
        )).size(portfolio=state, order=order(None), estimated_entry_fill_price=Decimal("10"))
        self.assertTrue(result.cash_capped)
        self.assertEqual(result.final_quantity, Decimal("50") / Decimal("10.10"))
        self.assertLessEqual(result.cash_required, state.available_cash)

    def test_fixed_notional_does_not_require_stop(self):
        result = PositionSizer(config(
            PositionSizingMode.FIXED_NOTIONAL, fixed=Decimal("100")
        )).size(portfolio=portfolio(), order=order(None), estimated_entry_fill_price=Decimal("50"))
        self.assertTrue(result.accepted)


class RiskPercentTests(unittest.TestCase):
    def test_zero_cost_risk_quantity(self):
        result = PositionSizer(config(
            PositionSizingMode.RISK_PERCENT, risk=Decimal("0.01")
        )).size(portfolio=portfolio(), order=order(), estimated_entry_fill_price=Decimal("100"))
        self.assertEqual(result.risk_budget, Decimal("10.00"))
        self.assertEqual(result.raw_quantity, Decimal("2.00"))
        self.assertEqual(result.estimated_loss_at_stop, Decimal("10.00"))

    def test_fees_spread_and_stop_slippage_reduce_quantity(self):
        financial = assumptions(
            entry_fee_rate=Decimal("0.001"),
            exit_fee_rate=Decimal("0.001"),
            spread_bps=Decimal("10"),
            stop_slippage_bps=Decimal("20"),
        )
        result = PositionSizer(config(
            PositionSizingMode.RISK_PERCENT,
            financial=financial,
            risk=Decimal("0.01"),
        )).size(portfolio=portfolio(financial), order=order(), estimated_entry_fill_price=Decimal("100.05"))
        self.assertLess(result.raw_quantity, Decimal("2"))
        self.assertLessEqual(result.estimated_loss_at_stop, result.risk_budget)

    def test_current_equity_not_initial_capital_sets_budget(self):
        state = portfolio()
        state.equity = Decimal("800")
        result = PositionSizer(config(
            PositionSizingMode.RISK_PERCENT, risk=Decimal("0.01")
        )).size(portfolio=state, order=order(), estimated_entry_fill_price=Decimal("100"))
        self.assertEqual(result.equity_used, Decimal("800"))
        self.assertEqual(result.risk_budget, Decimal("8.00"))

    def test_cash_cap_prevents_leverage(self):
        state = portfolio(capital=Decimal("400"))
        state.equity = Decimal("5000")
        result = PositionSizer(config(
            PositionSizingMode.RISK_PERCENT, risk=Decimal("0.01")
        )).size(portfolio=state, order=order(), estimated_entry_fill_price=Decimal("100"))
        self.assertEqual(result.raw_quantity, Decimal("10"))
        self.assertEqual(result.final_quantity, Decimal("4"))
        self.assertTrue(result.cash_capped)
        self.assertEqual(result.cash_required, Decimal("400"))

    def test_missing_and_invalid_stop_are_rejected(self):
        sizer = PositionSizer(config(PositionSizingMode.RISK_PERCENT, risk=Decimal("0.01")))
        missing = sizer.size(portfolio=portfolio(), order=order(None), estimated_entry_fill_price=Decimal("100"))
        invalid = sizer.size(portfolio=portfolio(), order=order(Decimal("100")), estimated_entry_fill_price=Decimal("100"))
        self.assertEqual(missing.rejection_reason, RejectionReason.MISSING_STOP)
        self.assertEqual(invalid.rejection_reason, RejectionReason.INVALID_STOP)


class ConstraintTests(unittest.TestCase):
    def setUp(self):
        self.sizer = PositionSizer(config(
            PositionSizingMode.FIXED_NOTIONAL, fixed=Decimal("123.7")
        ))

    def test_quantity_rounds_down(self):
        result = self.sizer.size(
            portfolio=portfolio(), order=order(None),
            estimated_entry_fill_price=Decimal("100"),
            constraints=InstrumentConstraints(qty_step=Decimal("0.01")),
        )
        self.assertEqual(result.raw_quantity, Decimal("1.237"))
        self.assertEqual(result.final_quantity, Decimal("1.23"))
        self.assertLessEqual(result.final_quantity, result.raw_quantity)

    def test_min_qty_and_min_notional_reject_separately(self):
        min_qty = self.sizer.size(
            portfolio=portfolio(), order=order(None), estimated_entry_fill_price=Decimal("100"),
            constraints=InstrumentConstraints(min_qty=Decimal("2")),
        )
        min_notional = self.sizer.size(
            portfolio=portfolio(), order=order(None), estimated_entry_fill_price=Decimal("100"),
            constraints=InstrumentConstraints(min_notional=Decimal("200")),
        )
        self.assertEqual(min_qty.rejection_reason, RejectionReason.INVALID_SIZE)
        self.assertEqual(min_notional.rejection_reason, RejectionReason.INVALID_SIZE)

    def test_max_qty_caps_then_revalidates(self):
        result = self.sizer.size(
            portfolio=portfolio(), order=order(None), estimated_entry_fill_price=Decimal("100"),
            constraints=InstrumentConstraints(max_qty=Decimal("1"), min_notional=Decimal("101")),
        )
        self.assertEqual(result.final_quantity, Decimal("1"))
        self.assertTrue(result.constraint_capped)
        self.assertEqual(result.rejection_reason, RejectionReason.INVALID_SIZE)

    def test_invalid_constraints_rejected(self):
        for kwargs in (
            {"qty_step": Decimal("0")}, {"min_qty": Decimal("-1")},
            {"max_qty": Decimal("0")}, {"min_notional": Decimal("0")},
            {"qty_step": 0.01},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(SizingError):
                InstrumentConstraints(**kwargs)


class PurityAndInvalidInputTests(unittest.TestCase):
    def test_sizing_does_not_mutate_portfolio(self):
        state = portfolio()
        before = (state.available_cash, state.equity, dict(state.positions), len(state.equity_curve))
        PositionSizer(config(PositionSizingMode.RISK_PERCENT, risk=Decimal("0.01"))).size(
            portfolio=state, order=order(), estimated_entry_fill_price=Decimal("100")
        )
        after = (state.available_cash, state.equity, dict(state.positions), len(state.equity_curve))
        self.assertEqual(after, before)

    def test_zero_equity_or_cash_rejects_without_quantity(self):
        sizer = PositionSizer(config(PositionSizingMode.RISK_PERCENT, risk=Decimal("0.01")))
        no_equity = portfolio()
        no_equity.equity = Decimal("0")
        no_cash = portfolio()
        no_cash.available_cash = Decimal("0")
        for state in (no_equity, no_cash):
            result = sizer.size(portfolio=state, order=order(), estimated_entry_fill_price=Decimal("100"))
            self.assertEqual(result.rejection_reason, RejectionReason.INVALID_SIZE)
            self.assertEqual(result.final_quantity, Decimal("0"))

    def test_float_price_and_sell_order_are_rejected(self):
        sizer = PositionSizer(config(PositionSizingMode.FIXED_NOTIONAL, fixed=Decimal("100")))
        with self.assertRaises(SizingError):
            sizer.size(portfolio=portfolio(), order=order(None), estimated_entry_fill_price=100.0)
        with self.assertRaises(SizingError):
            sizer.size(portfolio=portfolio(), order=order(None, SignalSide.SELL), estimated_entry_fill_price=Decimal("100"))

    def test_float_equity_or_cash_is_rejected(self):
        sizer = PositionSizer(config(PositionSizingMode.FIXED_NOTIONAL, fixed=Decimal("100")))
        for field in ("equity", "available_cash"):
            state = portfolio()
            setattr(state, field, 1000.0)
            with self.subTest(field=field), self.assertRaises(SizingError):
                sizer.size(
                    portfolio=state,
                    order=order(None),
                    estimated_entry_fill_price=Decimal("100"),
                )


if __name__ == "__main__":
    unittest.main()
