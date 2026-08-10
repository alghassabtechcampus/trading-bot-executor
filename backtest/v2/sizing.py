"""Pure, Decimal-only position sizing for Backtest Engine V2 Phase 6."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from .config import PositionSizingMode, RunConfig
from .costs import sell_costed_price
from .models import PendingOrder, RejectionReason, SignalSide
from .portfolio import PortfolioState


ZERO = Decimal("0")
ONE = Decimal("1")


class SizingError(ValueError):
    """Raised when sizing inputs or instrument constraints are malformed."""


def _positive_optional(name: str, value: Decimal | None) -> None:
    if value is not None and (
        not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO
    ):
        raise SizingError(f"{name} must be a positive finite Decimal or None")


@dataclass(frozen=True, slots=True)
class InstrumentConstraints:
    qty_step: Decimal | None = None
    min_qty: Decimal | None = None
    max_qty: Decimal | None = None
    min_notional: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("qty_step", "min_qty", "max_qty", "min_notional"):
            _positive_optional(name, getattr(self, name))
        if (
            self.min_qty is not None
            and self.max_qty is not None
            and self.min_qty > self.max_qty
        ):
            raise SizingError("min_qty cannot exceed max_qty")


@dataclass(frozen=True, slots=True)
class SizingResult:
    symbol: str
    mode: PositionSizingMode
    equity_used: Decimal
    raw_quantity: Decimal
    final_quantity: Decimal
    estimated_entry_price: Decimal
    estimated_entry_notional: Decimal
    estimated_entry_fee: Decimal
    stop_price: Decimal | None
    estimated_stop_fill_price: Decimal | None
    risk_budget: Decimal | None
    estimated_loss_at_stop: Decimal | None
    cash_required: Decimal
    cash_capped: bool
    constraint_capped: bool
    rejection_reason: RejectionReason | None

    @property
    def accepted(self) -> bool:
        return self.rejection_reason is None


class PositionSizer:
    """Calculate a long-spot quantity without mutating portfolio or order."""

    def __init__(self, config: RunConfig) -> None:
        if not isinstance(config, RunConfig):
            raise SizingError("config must be a RunConfig")
        self.config = config

    def size(
        self,
        *,
        portfolio: PortfolioState,
        order: PendingOrder,
        estimated_entry_fill_price: Decimal,
        constraints: InstrumentConstraints | None = None,
    ) -> SizingResult:
        if not isinstance(portfolio, PortfolioState):
            raise SizingError("portfolio must be a PortfolioState")
        if not isinstance(order, PendingOrder):
            raise SizingError("order must be a PendingOrder")
        if order.side is not SignalSide.BUY:
            raise SizingError("Phase 6 supports long-spot BUY sizing only")
        _positive_optional("estimated_entry_fill_price", estimated_entry_fill_price)
        selected_constraints = constraints or InstrumentConstraints()
        equity = portfolio.equity
        cash = portfolio.available_cash

        for name, value in (("portfolio.equity", equity), ("portfolio.available_cash", cash)):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise SizingError(f"{name} must be a finite Decimal")

        if equity <= ZERO or cash <= ZERO:
            return self._rejected(
                order, equity, estimated_entry_fill_price,
                RejectionReason.INVALID_SIZE,
            )

        stop_fill_price: Decimal | None = None
        risk_budget: Decimal | None = None
        loss_per_unit: Decimal | None = None
        mode = self.config.position_sizing_mode

        if mode is PositionSizingMode.FIXED_NOTIONAL:
            assert self.config.fixed_notional is not None
            raw_quantity = self.config.fixed_notional / estimated_entry_fill_price
        else:
            assert self.config.risk_per_trade is not None
            risk_budget = equity * self.config.risk_per_trade
            if order.stop_loss is None:
                return self._rejected(
                    order, equity, estimated_entry_fill_price,
                    RejectionReason.MISSING_STOP,
                    risk_budget=risk_budget,
                )
            if order.stop_loss >= estimated_entry_fill_price:
                return self._rejected(
                    order, equity, estimated_entry_fill_price,
                    RejectionReason.INVALID_STOP,
                    risk_budget=risk_budget,
                )
            stop_costed = sell_costed_price(
                order.stop_loss,
                ONE,
                self.config.financial_assumptions,
                slippage_bps=self.config.financial_assumptions.stop_slippage_bps,
            )
            stop_fill_price = stop_costed.fill_price
            loss_per_unit = (
                estimated_entry_fill_price
                - stop_fill_price
                + estimated_entry_fill_price
                * self.config.financial_assumptions.entry_fee_rate
                + stop_fill_price
                * self.config.financial_assumptions.exit_fee_rate
            )
            if loss_per_unit <= ZERO:
                return self._rejected(
                    order, equity, estimated_entry_fill_price,
                    RejectionReason.INVALID_STOP,
                    stop_fill_price=stop_fill_price,
                    risk_budget=risk_budget,
                )
            raw_quantity = risk_budget / loss_per_unit

        fee_rate = self.config.financial_assumptions.entry_fee_rate
        affordable_quantity = cash / (estimated_entry_fill_price * (ONE + fee_rate))
        final_quantity = min(raw_quantity, affordable_quantity)
        cash_capped = final_quantity < raw_quantity
        constraint_capped = False

        if selected_constraints.max_qty is not None and final_quantity > selected_constraints.max_qty:
            final_quantity = selected_constraints.max_qty
            constraint_capped = True
        if selected_constraints.qty_step is not None:
            rounded = (
                final_quantity / selected_constraints.qty_step
            ).to_integral_value(rounding=ROUND_FLOOR) * selected_constraints.qty_step
            constraint_capped = constraint_capped or rounded < final_quantity
            final_quantity = rounded

        entry_notional = final_quantity * estimated_entry_fill_price
        entry_fee = entry_notional * fee_rate
        cash_required = entry_notional + entry_fee
        estimated_loss = (
            None if loss_per_unit is None else final_quantity * loss_per_unit
        )

        invalid = (
            final_quantity <= ZERO
            or (selected_constraints.min_qty is not None and final_quantity < selected_constraints.min_qty)
            or (selected_constraints.min_notional is not None and entry_notional < selected_constraints.min_notional)
            or cash_required > cash
            or (risk_budget is not None and estimated_loss is not None and estimated_loss > risk_budget)
        )
        return SizingResult(
            symbol=order.symbol,
            mode=mode,
            equity_used=equity,
            raw_quantity=raw_quantity,
            final_quantity=final_quantity,
            estimated_entry_price=estimated_entry_fill_price,
            estimated_entry_notional=entry_notional,
            estimated_entry_fee=entry_fee,
            stop_price=order.stop_loss,
            estimated_stop_fill_price=stop_fill_price,
            risk_budget=risk_budget,
            estimated_loss_at_stop=estimated_loss,
            cash_required=cash_required,
            cash_capped=cash_capped,
            constraint_capped=constraint_capped,
            rejection_reason=RejectionReason.INVALID_SIZE if invalid else None,
        )

    def _rejected(
        self,
        order: PendingOrder,
        equity: Decimal,
        entry_price: Decimal,
        reason: RejectionReason,
        *,
        stop_fill_price: Decimal | None = None,
        risk_budget: Decimal | None = None,
    ) -> SizingResult:
        return SizingResult(
            symbol=order.symbol,
            mode=self.config.position_sizing_mode,
            equity_used=equity,
            raw_quantity=ZERO,
            final_quantity=ZERO,
            estimated_entry_price=entry_price,
            estimated_entry_notional=ZERO,
            estimated_entry_fee=ZERO,
            stop_price=order.stop_loss,
            estimated_stop_fill_price=stop_fill_price,
            risk_budget=risk_budget,
            estimated_loss_at_stop=None,
            cash_required=ZERO,
            cash_capped=False,
            constraint_capped=False,
            rejection_reason=reason,
        )
