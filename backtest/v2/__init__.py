"""Backtest Engine V2 core package (Phases 1 through 6)."""

from .config import (
    EndOfTestPolicy,
    ExecutionProfile,
    FinancialAssumptions,
    IntrabarPolicy,
    PositionSizingMode,
    RunConfig,
)
from .models import (
    Candle,
    ClosedTrade,
    ExecutionReason,
    ExecutionResult,
    ExitReason,
    Fill,
    PendingOrder,
    PendingOrderStatus,
    RejectionReason,
    SignalIntent,
    SignalSide,
)
from .portfolio import EquityEventType, EquityPoint, PortfolioState, Position
from .sizing import InstrumentConstraints, PositionSizer, SizingResult

__all__ = [
    "Candle",
    "ClosedTrade",
    "ExecutionProfile",
    "ExecutionReason",
    "ExecutionResult",
    "ExitReason",
    "EndOfTestPolicy",
    "EquityEventType",
    "EquityPoint",
    "Fill",
    "FinancialAssumptions",
    "IntrabarPolicy",
    "InstrumentConstraints",
    "PendingOrder",
    "PendingOrderStatus",
    "PortfolioState",
    "Position",
    "PositionSizer",
    "PositionSizingMode",
    "RejectionReason",
    "RunConfig",
    "SignalIntent",
    "SignalSide",
    "SizingResult",
]
