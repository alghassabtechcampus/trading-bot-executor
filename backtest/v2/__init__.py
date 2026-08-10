"""Backtest Engine V2 core package (Phases 1 and 2)."""

from .config import ExecutionProfile, FinancialAssumptions, RunConfig
from .models import Candle, ClosedTrade, ExitReason

__all__ = [
    "Candle",
    "ClosedTrade",
    "ExecutionProfile",
    "ExitReason",
    "FinancialAssumptions",
    "RunConfig",
]
