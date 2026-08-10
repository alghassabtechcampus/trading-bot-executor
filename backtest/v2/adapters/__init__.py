"""Strategy adapters supported by Backtest V2."""

from .base import AdapterSignal, StrategyAdapter
from .version_ab import VersionAAdapter, VersionBAdapter

__all__ = ["AdapterSignal", "StrategyAdapter", "VersionAAdapter", "VersionBAdapter"]
