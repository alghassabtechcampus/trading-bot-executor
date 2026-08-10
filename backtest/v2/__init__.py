"""Backtest Engine V2 core package (Phases 1 through 3)."""

from .config import ExecutionProfile, FinancialAssumptions, IntrabarPolicy, RunConfig
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

__all__ = [
    "Candle",
    "ClosedTrade",
    "ExecutionProfile",
    "ExecutionReason",
    "ExecutionResult",
    "ExitReason",
    "Fill",
    "FinancialAssumptions",
    "IntrabarPolicy",
    "PendingOrder",
    "PendingOrderStatus",
    "RejectionReason",
    "RunConfig",
    "SignalIntent",
    "SignalSide",
]
