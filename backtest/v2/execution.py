"""Causal order execution primitives for Backtest Engine V2 Phase 4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from .config import FinancialAssumptions, IntrabarPolicy
from .costs import buy_costed_price, sell_costed_price
from .models import (
    Candle,
    ExecutionReason,
    ExecutionRejection,
    ExecutionResult,
    ExitReason,
    Fill,
    PendingOrder,
    RejectionReason,
    SignalSide,
)


class CausalExecutionError(ValueError):
    """Raised when an attempted fill would violate order eligibility."""


@dataclass(frozen=True, slots=True)
class ExitExecution:
    fill: Fill
    exit_reason: ExitReason
    intrabar_ambiguous: bool
    intrabar_policy: IntrabarPolicy


def _require_positive_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite Decimal")


def _entry_rejection(order: PendingOrder, reason: RejectionReason) -> ExecutionResult:
    return ExecutionResult(rejection=ExecutionRejection(
        order_id=order.order_id,
        symbol=order.symbol,
        timestamp=order.eligible_from,
        reason=reason,
    ))


def execute_pending_entry(
    order: PendingOrder,
    candles: Sequence[Candle],
    assumptions: FinancialAssumptions,
) -> ExecutionResult:
    """Fill a BUY at the first available bar open on/after eligibility.

    Candles before ``eligible_from`` are never inspected for prices. No candle
    at ``signal_time`` can fill because the order model requires a strictly
    later eligibility time.
    """

    if order.side is not SignalSide.BUY:
        raise ValueError("execute_pending_entry accepts BUY orders only")
    if order.quantity is None:
        return _entry_rejection(order, RejectionReason.INVALID_SIZE)

    eligible = sorted(
        (
            candle for candle in candles
            if candle.symbol == order.symbol and candle.timestamp >= order.eligible_from
        ),
        key=lambda candle: candle.timestamp,
    )
    if not eligible:
        return _entry_rejection(order, RejectionReason.NO_NEXT_BAR)

    next_candle = eligible[0]
    if next_candle.timestamp < order.signal_time:
        raise CausalExecutionError("entry fill cannot precede signal_time")
    costed = buy_costed_price(next_candle.open, order.quantity, assumptions)
    notional = costed.fill_price * order.quantity
    fill = Fill(
        order_id=order.order_id,
        symbol=order.symbol,
        side=SignalSide.BUY,
        timestamp=next_candle.timestamp,
        reference_price=next_candle.open,
        fill_price=costed.fill_price,
        quantity=order.quantity,
        notional=notional,
        spread_cost=costed.spread_cost,
        slippage_cost=costed.slippage_cost,
        fee=notional * assumptions.entry_fee_rate,
        execution_reason=ExecutionReason.ENTRY_MARKET,
    )
    return ExecutionResult(fill=fill)


def execute_market_exit(
    *,
    order_id: str,
    symbol: str,
    timestamp: datetime,
    reference_price: Decimal,
    quantity: Decimal,
    assumptions: FinancialAssumptions,
    execution_reason: ExecutionReason = ExecutionReason.EXIT_MARKET,
    slippage_bps: Decimal | None = None,
) -> Fill:
    costed = sell_costed_price(
        reference_price,
        quantity,
        assumptions,
        slippage_bps=slippage_bps,
    )
    notional = costed.fill_price * quantity
    return Fill(
        order_id=order_id,
        symbol=symbol,
        side=SignalSide.SELL,
        timestamp=timestamp,
        reference_price=reference_price,
        fill_price=costed.fill_price,
        quantity=quantity,
        notional=notional,
        spread_cost=costed.spread_cost,
        slippage_cost=costed.slippage_cost,
        fee=notional * assumptions.exit_fee_rate,
        execution_reason=execution_reason,
    )


def stop_fill(
    *,
    order_id: str,
    symbol: str,
    candle: Candle,
    stop_loss: Decimal,
    quantity: Decimal,
    assumptions: FinancialAssumptions,
) -> Fill | None:
    """Return a gap-aware long-position market stop fill, or None."""

    _require_positive_decimal("stop_loss", stop_loss)
    _require_positive_decimal("quantity", quantity)
    if candle.open < stop_loss:
        reference = candle.open
    elif candle.low <= stop_loss:
        reference = stop_loss
    else:
        return None
    return execute_market_exit(
        order_id=order_id,
        symbol=symbol,
        timestamp=candle.close_time,
        reference_price=reference,
        quantity=quantity,
        assumptions=assumptions,
        execution_reason=ExecutionReason.STOP_LOSS,
        slippage_bps=assumptions.stop_slippage_bps,
    )


def take_profit_fill(
    *,
    order_id: str,
    symbol: str,
    candle: Candle,
    take_profit: Decimal,
    quantity: Decimal,
    assumptions: FinancialAssumptions,
) -> Fill | None:
    """Return a gap-aware long-position market take-profit fill, or None."""

    _require_positive_decimal("take_profit", take_profit)
    _require_positive_decimal("quantity", quantity)
    if candle.open > take_profit:
        reference = candle.open
    elif candle.high >= take_profit:
        reference = take_profit
    else:
        return None
    return execute_market_exit(
        order_id=order_id,
        symbol=symbol,
        timestamp=candle.close_time,
        reference_price=reference,
        quantity=quantity,
        assumptions=assumptions,
        execution_reason=ExecutionReason.TAKE_PROFIT,
        slippage_bps=assumptions.take_profit_slippage_bps,
    )


def execute_intrabar_exit(
    *,
    order_id: str,
    symbol: str,
    candle: Candle,
    stop_loss: Decimal,
    take_profit: Decimal,
    quantity: Decimal,
    assumptions: FinancialAssumptions,
    policy: IntrabarPolicy = IntrabarPolicy.STOP_FIRST,
) -> ExitExecution | None:
    """Resolve long SL/TP ambiguity according to an explicit policy."""

    if not isinstance(policy, IntrabarPolicy):
        raise ValueError("policy must be an IntrabarPolicy")
    stop = stop_fill(
        order_id=order_id,
        symbol=symbol,
        candle=candle,
        stop_loss=stop_loss,
        quantity=quantity,
        assumptions=assumptions,
    )
    target = take_profit_fill(
        order_id=order_id,
        symbol=symbol,
        candle=candle,
        take_profit=take_profit,
        quantity=quantity,
        assumptions=assumptions,
    )
    ambiguous = stop is not None and target is not None
    if ambiguous:
        selected = stop if policy is IntrabarPolicy.STOP_FIRST else target
        reason = (
            ExitReason.STOP_LOSS
            if policy is IntrabarPolicy.STOP_FIRST
            else ExitReason.TAKE_PROFIT
        )
        return ExitExecution(selected, reason, True, policy)
    if stop is not None:
        return ExitExecution(stop, ExitReason.STOP_LOSS, False, policy)
    if target is not None:
        return ExitExecution(target, ExitReason.TAKE_PROFIT, False, policy)
    return None
