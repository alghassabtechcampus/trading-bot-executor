"""Portfolio and cash accounting for Backtest Engine V2 Phase 5.

Equity uses conservative estimated liquidation value:

    equity = available_cash + reserved_cash
             + sum(position liquidation values)

Each liquidation value applies configured bid-side spread, normal exit
slippage, and the configured exit fee. These are estimates only; future exit
fees are not added to ``total_fees`` until an actual SELL Fill is applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .config import FinancialAssumptions
from .costs import sell_costed_price
from .models import Candle, Fill, RejectionReason, SignalSide


ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


class PortfolioError(ValueError):
    """Raised for an invalid financial operation on the portfolio."""


class EquityEventType(str, Enum):
    INITIAL = "INITIAL"
    TIMESTAMP = "TIMESTAMP"
    ENTRY_FILL = "ENTRY_FILL"
    EXIT_FILL = "EXIT_FILL"
    END = "END"


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: Decimal
    entry_fill_price: Decimal
    entry_notional: Decimal
    entry_fee: Decimal
    entry_time: datetime
    stop_loss: Decimal | None
    take_profit: Decimal | None
    strategy_name: str
    strategy_version: str
    metadata: Mapping[str, Any]
    current_mark_price: Decimal
    unrealized_pnl: Decimal = ZERO

    def __post_init__(self) -> None:
        if not self.symbol or not self.strategy_name or not self.strategy_version:
            raise PortfolioError("position identity and strategy fields are required")
        if self.entry_time.tzinfo is None or self.entry_time.utcoffset() != timedelta(0):
            raise PortfolioError("entry_time must be timezone-aware UTC")
        for name in (
            "quantity", "entry_fill_price", "entry_notional", "current_mark_price"
        ):
            _require_positive_decimal(name, getattr(self, name))
        _require_non_negative_decimal("entry_fee", self.entry_fee)
        for name in ("stop_loss", "take_profit"):
            value = getattr(self, name)
            if value is not None:
                _require_positive_decimal(name, value)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    event_type: EquityEventType
    sequence: int
    cash: Decimal
    reserved_cash: Decimal
    positions_market_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    fees_to_date: Decimal
    slippage_to_date: Decimal
    equity: Decimal
    drawdown_value: Decimal
    drawdown_pct: Decimal
    open_positions_count: int


@dataclass(slots=True)
class PortfolioState:
    initial_capital: Decimal
    available_cash: Decimal
    reserved_cash: Decimal
    positions: dict[str, Position]
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_fees: Decimal
    total_slippage_cost: Decimal
    total_spread_cost: Decimal
    equity: Decimal
    peak_equity: Decimal
    drawdown_value: Decimal
    drawdown_pct: Decimal
    financial_assumptions: FinancialAssumptions
    equity_curve: list[EquityPoint] = field(default_factory=list)
    _sequence: int = 0

    @classmethod
    def create(
        cls,
        *,
        initial_capital: Decimal,
        financial_assumptions: FinancialAssumptions,
        timestamp: datetime,
    ) -> "PortfolioState":
        _require_positive_decimal("initial_capital", initial_capital)
        _require_utc("timestamp", timestamp)
        state = cls(
            initial_capital=initial_capital,
            available_cash=initial_capital,
            reserved_cash=ZERO,
            positions={},
            realized_pnl=ZERO,
            unrealized_pnl=ZERO,
            total_fees=ZERO,
            total_slippage_cost=ZERO,
            total_spread_cost=ZERO,
            equity=initial_capital,
            peak_equity=initial_capital,
            drawdown_value=ZERO,
            drawdown_pct=ZERO,
            financial_assumptions=financial_assumptions,
        )
        state.record_equity(timestamp, EquityEventType.INITIAL)
        return state

    def open_position(
        self,
        fill: Fill,
        *,
        stop_loss: Decimal | None,
        take_profit: Decimal | None,
        strategy_name: str,
        strategy_version: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> RejectionReason | None:
        """Apply a fully-sized BUY Fill; Phase 5 performs no sizing itself."""

        if fill.side is not SignalSide.BUY:
            raise PortfolioError("opening a position requires a BUY fill")
        self._validate_fill(fill, self.financial_assumptions.entry_fee_rate)
        if fill.symbol in self.positions:
            return RejectionReason.DUPLICATE_SYMBOL
        cash_required = fill.notional + fill.fee
        if self.available_cash < cash_required:
            return RejectionReason.INSUFFICIENT_CASH

        self.available_cash -= cash_required
        if self.available_cash < ZERO:
            raise PortfolioError("cash cannot become negative")
        self.total_fees += fill.fee
        self.total_slippage_cost += fill.slippage_cost
        self.total_spread_cost += fill.spread_cost
        self.positions[fill.symbol] = Position(
            symbol=fill.symbol,
            quantity=fill.quantity,
            entry_fill_price=fill.fill_price,
            entry_notional=fill.notional,
            entry_fee=fill.fee,
            entry_time=fill.timestamp,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            metadata=metadata or {},
            current_mark_price=fill.fill_price,
        )
        self._revalue()
        self.record_equity(fill.timestamp, EquityEventType.ENTRY_FILL)
        return None

    def close_position(self, fill: Fill) -> Decimal:
        """Apply a full-position SELL Fill and return realized trade PnL.

        Partial closes are deliberately unsupported in Phase 5.
        """

        if fill.side is not SignalSide.SELL:
            raise PortfolioError("closing a position requires a SELL fill")
        self._validate_fill(fill, self.financial_assumptions.exit_fee_rate)
        position = self.positions.get(fill.symbol)
        if position is None:
            raise PortfolioError(f"no open position for {fill.symbol}")
        if fill.quantity <= ZERO:
            raise PortfolioError("close quantity must be positive")
        if fill.quantity != position.quantity:
            relation = "exceeds" if fill.quantity > position.quantity else "is below"
            raise PortfolioError(
                f"close quantity {relation} the full open quantity; partial close unsupported"
            )

        exit_notional = fill.notional
        cash_received = exit_notional - fill.fee
        realized_trade_pnl = (
            exit_notional
            - position.entry_notional
            - position.entry_fee
            - fill.fee
        )
        self.available_cash += cash_received
        self.realized_pnl += realized_trade_pnl
        self.total_fees += fill.fee
        self.total_slippage_cost += fill.slippage_cost
        self.total_spread_cost += fill.spread_cost
        del self.positions[fill.symbol]
        self._revalue()
        self.record_equity(fill.timestamp, EquityEventType.EXIT_FILL)
        return realized_trade_pnl

    def mark_to_market(
        self,
        *,
        timestamp: datetime,
        candles: Iterable[Candle],
    ) -> EquityPoint:
        """Update marks only from candles that close at this exact timestamp.

        A missing symbol retains its last causally-known mark. No other symbol's
        price and no future candle can update it.
        """

        _require_utc("timestamp", timestamp)
        seen: set[str] = set()
        for candle in candles:
            if candle.close_time != timestamp:
                raise PortfolioError("mark candle must close at the current timestamp")
            if candle.symbol in seen:
                raise PortfolioError(f"duplicate mark candle for {candle.symbol}")
            seen.add(candle.symbol)
            position = self.positions.get(candle.symbol)
            if position is not None:
                _require_positive_decimal("candle.close", candle.close)
                position.current_mark_price = candle.close
        self._revalue()
        return self.record_equity(timestamp, EquityEventType.TIMESTAMP)

    def record_end(self, timestamp: datetime) -> EquityPoint:
        """Record final marked state; CLOSE_AT_END orchestration is deferred."""

        _require_utc("timestamp", timestamp)
        self._revalue()
        return self.record_equity(timestamp, EquityEventType.END)

    def record_equity(
        self,
        timestamp: datetime,
        event_type: EquityEventType,
    ) -> EquityPoint:
        _require_utc("timestamp", timestamp)
        if not isinstance(event_type, EquityEventType):
            raise PortfolioError("event_type must be an EquityEventType")
        self._sequence += 1
        point = EquityPoint(
            timestamp=timestamp,
            event_type=event_type,
            sequence=self._sequence,
            cash=self.available_cash,
            reserved_cash=self.reserved_cash,
            positions_market_value=self._positions_market_value(),
            unrealized_pnl=self.unrealized_pnl,
            realized_pnl=self.realized_pnl,
            fees_to_date=self.total_fees,
            slippage_to_date=self.total_slippage_cost,
            equity=self.equity,
            drawdown_value=self.drawdown_value,
            drawdown_pct=self.drawdown_pct,
            open_positions_count=len(self.positions),
        )
        self.equity_curve.append(point)
        return point

    def _liquidation_value(self, position: Position) -> Decimal:
        costed = sell_costed_price(
            position.current_mark_price,
            position.quantity,
            self.financial_assumptions,
        )
        notional = costed.fill_price * position.quantity
        estimated_exit_fee = notional * self.financial_assumptions.exit_fee_rate
        return notional - estimated_exit_fee

    def _validate_fill(self, fill: Fill, fee_rate: Decimal) -> None:
        expected_notional = fill.fill_price * fill.quantity
        if fill.notional != expected_notional:
            raise PortfolioError("fill notional must equal fill_price * quantity")
        if fill.fee != expected_notional * fee_rate:
            raise PortfolioError("fill fee does not match configured fee rate")

    def _positions_market_value(self) -> Decimal:
        return sum(
            (position.quantity * position.current_mark_price for position in self.positions.values()),
            start=ZERO,
        )

    def _revalue(self) -> None:
        liquidation_total = ZERO
        unrealized_total = ZERO
        for position in self.positions.values():
            liquidation_value = self._liquidation_value(position)
            position.unrealized_pnl = (
                liquidation_value
                - position.entry_notional
                - position.entry_fee
            )
            liquidation_total += liquidation_value
            unrealized_total += position.unrealized_pnl

        self.unrealized_pnl = unrealized_total
        self.equity = self.available_cash + self.reserved_cash + liquidation_total
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        self.drawdown_value = self.equity - self.peak_equity
        self.drawdown_pct = (
            self.drawdown_value / self.peak_equity * ONE_HUNDRED
            if self.peak_equity > ZERO
            else ZERO
        )


def _require_positive_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= ZERO:
        raise PortfolioError(f"{name} must be a positive finite Decimal")


def _require_non_negative_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < ZERO:
        raise PortfolioError(f"{name} must be a non-negative finite Decimal")


def _require_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise PortfolioError(f"{name} must be timezone-aware UTC")
