"""Core immutable domain models for Backtest Engine V2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MAX_HOLD = "MAX_HOLD"
    SIGNAL_EXIT = "SIGNAL_EXIT"
    END_OF_TEST = "END_OF_TEST"


class RejectionReason(str, Enum):
    MAX_CONCURRENT_REACHED = "MAX_CONCURRENT_REACHED"
    DUPLICATE_SYMBOL = "DUPLICATE_SYMBOL"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    NO_NEXT_BAR = "NO_NEXT_BAR"
    INVALID_SIZE = "INVALID_SIZE"
    STALE_SIGNAL = "STALE_SIGNAL"
    INVALID_STOP = "INVALID_STOP"
    MISSING_STOP = "MISSING_STOP"


class SignalSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PendingOrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ExecutionReason(str, Enum):
    ENTRY_MARKET = "ENTRY_MARKET"
    EXIT_MARKET = "EXIT_MARKET"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"


@dataclass(frozen=True, slots=True)
class PendingOrder:
    """A causal order intent that cannot execute before ``eligible_from``."""

    order_id: str
    symbol: str
    side: SignalSide
    signal_time: datetime
    eligible_from: datetime
    strategy_name: str
    strategy_version: str
    reference_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    score: Decimal | None
    priority: int | None
    metadata: Mapping[str, Any]
    status: PendingOrderStatus
    quantity: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.order_id or not self.symbol or not self.strategy_name or not self.strategy_version:
            raise ValueError("order_id, symbol, strategy_name, and strategy_version are required")
        for name, value in (("signal_time", self.signal_time), ("eligible_from", self.eligible_from)):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError(f"{name} must be timezone-aware UTC")
        if self.eligible_from <= self.signal_time:
            raise ValueError("eligible_from must be later than signal_time")
        if not isinstance(self.side, SignalSide):
            raise ValueError("side must be a SignalSide")
        if not isinstance(self.status, PendingOrderStatus):
            raise ValueError("status must be a PendingOrderStatus")
        for name in ("reference_price", "stop_loss", "take_profit", "score", "quantity"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite()
            ):
                raise ValueError(f"{name} must be a finite Decimal or None")
        if self.reference_price <= 0:
            raise ValueError("reference_price must be positive")
        if self.stop_loss is not None and self.stop_loss <= 0:
            raise ValueError("stop_loss must be positive when supplied")
        if self.take_profit is not None and self.take_profit <= 0:
            raise ValueError("take_profit must be positive when supplied")
        if self.quantity is not None and self.quantity <= 0:
            raise ValueError("quantity must be positive when supplied")
        if self.priority is not None and (
            isinstance(self.priority, bool) or not isinstance(self.priority, int)
        ):
            raise ValueError("priority must be an integer or None")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class Fill:
    """One costed execution result; monetary costs use quote currency."""

    order_id: str
    symbol: str
    side: SignalSide
    timestamp: datetime
    reference_price: Decimal
    fill_price: Decimal
    quantity: Decimal
    notional: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    fee: Decimal
    execution_reason: ExecutionReason

    def __post_init__(self) -> None:
        if not self.order_id or not self.symbol:
            raise ValueError("order_id and symbol are required")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        if not isinstance(self.side, SignalSide):
            raise ValueError("side must be a SignalSide")
        if not isinstance(self.execution_reason, ExecutionReason):
            raise ValueError("execution_reason must be an ExecutionReason")
        positive = ("reference_price", "fill_price", "quantity", "notional")
        non_negative = ("spread_cost", "slippage_cost", "fee")
        for name in positive:
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        for name in non_negative:
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a non-negative finite Decimal")


@dataclass(frozen=True, slots=True)
class ExecutionRejection:
    order_id: str
    symbol: str
    timestamp: datetime
    reason: RejectionReason


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    fill: Fill | None = None
    rejection: ExecutionRejection | None = None

    def __post_init__(self) -> None:
        if (self.fill is None) == (self.rejection is None):
            raise ValueError("exactly one of fill or rejection must be supplied")


@dataclass(frozen=True, slots=True)
class SignalIntent:
    """A strategy decision made from a candle that has already closed.

    A larger explicit ``strategy_priority`` sorts first. When priorities are
    equal or absent, a larger ``score`` sorts first. Neither field invents a
    strategy value when the originating strategy does not provide one.
    """

    symbol: str
    signal_time: datetime
    side: SignalSide
    strategy_name: str
    strategy_version: str
    strategy_priority: int | None = None
    score: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.symbol or not self.strategy_name or not self.strategy_version:
            raise ValueError("symbol, strategy_name, and strategy_version are required")
        if self.signal_time.tzinfo is None or self.signal_time.utcoffset() != timedelta(0):
            raise ValueError("signal_time must be timezone-aware UTC")
        if not isinstance(self.side, SignalSide):
            raise ValueError("side must be a SignalSide")
        if self.strategy_priority is not None and (
            isinstance(self.strategy_priority, bool)
            or not isinstance(self.strategy_priority, int)
        ):
            raise ValueError("strategy_priority must be an integer or None")
        if self.score is not None and (
            not isinstance(self.score, Decimal) or not self.score.is_finite()
        ):
            raise ValueError("score must be a finite Decimal or None")


@dataclass(frozen=True, slots=True)
class Candle:
    """A completed or potentially-completed OHLCV bar.

    ``timestamp`` is the UTC bar-open time. ``timeframe`` defines its end time.
    """

    symbol: str
    timestamp: datetime
    timeframe: timedelta
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def close_time(self) -> datetime:
        return self.timestamp + self.timeframe

    @property
    def is_utc(self) -> bool:
        return self.timestamp.tzinfo is not None and self.timestamp.utcoffset() == timedelta(0)


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """Auditable result of one completed position.

    Monetary fields are in the run base currency. ``net_return_pct``, ``mfe``,
    and ``mae`` are percentage-point values (1 means 1%), not decimal rates.
    Fees are monetary amounts already paid. Cost fields are monetary amounts.
    """

    entry_signal_time: datetime
    entry_fill_time: datetime
    exit_fill_time: datetime
    entry_reference_price: Decimal
    entry_fill_price: Decimal
    exit_reference_price: Decimal
    exit_fill_price: Decimal
    quantity: Decimal
    entry_notional: Decimal
    exit_notional: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    net_return_pct: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    holding_duration: timedelta
    exit_reason: ExitReason
    mfe: Decimal
    mae: Decimal
    intrabar_ambiguous: bool
    strategy_name: str
    strategy_version: str
    symbol: str

    def __post_init__(self) -> None:
        utc_times = (self.entry_signal_time, self.entry_fill_time, self.exit_fill_time)
        if any(t.tzinfo is None or t.utcoffset() != timedelta(0) for t in utc_times):
            raise ValueError("trade timestamps must be timezone-aware UTC")
        if self.entry_fill_time < self.entry_signal_time:
            raise ValueError("entry_fill_time cannot precede entry_signal_time")
        if self.exit_fill_time < self.entry_fill_time:
            raise ValueError("exit_fill_time cannot precede entry_fill_time")
        if self.holding_duration != self.exit_fill_time - self.entry_fill_time:
            raise ValueError("holding_duration must equal exit_fill_time - entry_fill_time")
        positive = {
            "entry_reference_price": self.entry_reference_price,
            "entry_fill_price": self.entry_fill_price,
            "exit_reference_price": self.exit_reference_price,
            "exit_fill_price": self.exit_fill_price,
            "quantity": self.quantity,
            "entry_notional": self.entry_notional,
            "exit_notional": self.exit_notional,
        }
        for name, value in positive.items():
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        non_negative = {
            "entry_fee": self.entry_fee,
            "exit_fee": self.exit_fee,
            "spread_cost": self.spread_cost,
            "slippage_cost": self.slippage_cost,
        }
        for name, value in non_negative.items():
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a non-negative finite Decimal")
        for name in ("gross_pnl", "net_pnl", "net_return_pct", "mfe", "mae"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
        if not isinstance(self.exit_reason, ExitReason):
            raise ValueError("exit_reason must be an ExitReason")
        if not self.strategy_name or not self.strategy_version or not self.symbol:
            raise ValueError("strategy_name, strategy_version, and symbol are required")


UTC = timezone.utc
