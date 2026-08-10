"""Read-only interface between legacy signal logic and V2 orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from ..models import Candle


@dataclass(frozen=True, slots=True)
class AdapterSignal:
    action: str
    score: Decimal
    reference_price: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class StrategyAdapter(Protocol):
    strategy_name: str
    strategy_version: str
    max_hold_minutes: int
    window_size: int

    def evaluate(self, history: Sequence[Candle]) -> AdapterSignal | None: ...
