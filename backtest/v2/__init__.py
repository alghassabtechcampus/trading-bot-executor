"""Backtest Engine V2 core package (Phases 1 through 5)."""

from .config import (
    EndOfTestPolicy,
    ExecutionProfile,
    FinancialAssumptions,
    IntrabarPolicy,
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
    "PendingOrder",
    "PendingOrderStatus",
    "PortfolioState",
    "Position",
    "RejectionReason",
    "RunConfig",
    "SignalIntent",
    "SignalSide",
]
