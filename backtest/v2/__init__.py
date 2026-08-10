"""Backtest Engine V2 core package (Phases 1 through 3)."""

from .config import ExecutionProfile, FinancialAssumptions, RunConfig
from .models import (
    Candle,
    ClosedTrade,
    ExitReason,
    RejectionReason,
    SignalIntent,
    SignalSide,
)

__all__ = [
    "Candle",
    "ClosedTrade",
    "ExecutionProfile",
    "ExitReason",
    "FinancialAssumptions",
    "RejectionReason",
    "RunConfig",
    "SignalIntent",
    "SignalSide",
]
