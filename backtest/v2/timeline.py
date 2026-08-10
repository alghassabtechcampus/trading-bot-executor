"""Deterministic, causal multi-symbol timeline for Backtest Engine V2.

Phase 3 intentionally contains no fills, execution prices, or portfolio
accounting. It establishes only event order, signal evaluation/ranking, and
the duplicate-protection state needed by later phases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Callable, Iterable, Iterator, Mapping, Sequence

from .models import Candle, RejectionReason, SignalIntent


class TimelineError(ValueError):
    """Base error for invalid timeline inputs or causal violations."""


class DuplicateTimelineCandleError(TimelineError):
    """Raised when one symbol has multiple candles available at one time."""


class CausalityError(TimelineError):
    """Raised when a signal claims a time other than its closed candle time."""


class EventPhase(str, Enum):
    MARK_UPDATE = "MARK_UPDATE"
    EXIT_CHECK = "EXIT_CHECK"
    CANDLE_CLOSE = "CANDLE_CLOSE"
    SIGNAL_EVALUATION = "SIGNAL_EVALUATION"
    SIGNAL_RANKING = "SIGNAL_RANKING"
    PENDING_ORDER_CREATION = "PENDING_ORDER_CREATION"


EVENT_PHASE_ORDER = (
    EventPhase.MARK_UPDATE,
    EventPhase.EXIT_CHECK,
    EventPhase.CANDLE_CLOSE,
    EventPhase.SIGNAL_EVALUATION,
    EventPhase.SIGNAL_RANKING,
    EventPhase.PENDING_ORDER_CREATION,
)


@dataclass(frozen=True, slots=True)
class TimelineFrame:
    """Candles that become available at exactly one UTC close timestamp."""

    timestamp: datetime
    candles: tuple[Candle, ...]

    def candle_for(self, symbol: str) -> Candle | None:
        """Return only an actually present candle; never forward-fill."""

        return next((c for c in self.candles if c.symbol == symbol), None)


@dataclass(frozen=True, slots=True)
class UnifiedTimeline:
    frames: tuple[TimelineFrame, ...]

    def __iter__(self) -> Iterator[TimelineFrame]:
        return iter(self.frames)


@dataclass(frozen=True, slots=True)
class SignalContext:
    """Read-only context containing no candle later than ``timestamp``."""

    timestamp: datetime
    candle: Candle
    history: tuple[Candle, ...]

    def __post_init__(self) -> None:
        if self.candle.close_time != self.timestamp:
            raise CausalityError("current candle must close at context timestamp")
        if not self.history or self.history[-1] != self.candle:
            raise CausalityError("history must end with the current closed candle")
        if any(c.close_time > self.timestamp for c in self.history):
            raise CausalityError("signal history contains a future candle")


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    timestamp: datetime
    phase: EventPhase
    candles: tuple[Candle, ...]
    signals: tuple[SignalIntent, ...] = ()


SignalEvaluator = Callable[[SignalContext], SignalIntent | Iterable[SignalIntent] | None]


@dataclass(slots=True)
class DuplicateProtectionState:
    """Minimal Phase-3 state; it performs no cash or fill accounting."""

    active_position_symbols: set[str] = field(default_factory=set)
    pending_buy_symbols: set[str] = field(default_factory=set)

    def buy_rejection(self, symbol: str) -> RejectionReason | None:
        if symbol in self.active_position_symbols or symbol in self.pending_buy_symbols:
            return RejectionReason.DUPLICATE_SYMBOL
        return None

    def register_pending_buy(self, symbol: str) -> RejectionReason | None:
        rejection = self.buy_rejection(symbol)
        if rejection is None:
            self.pending_buy_symbols.add(symbol)
        return rejection

    def clear_pending_buy(self, symbol: str) -> None:
        self.pending_buy_symbols.discard(symbol)

    def mark_position_active(self, symbol: str) -> None:
        self.pending_buy_symbols.discard(symbol)
        self.active_position_symbols.add(symbol)

    def mark_position_closed(self, symbol: str) -> None:
        self.active_position_symbols.discard(symbol)


def build_unified_timeline(
    candles_by_symbol: Mapping[str, Sequence[Candle]],
) -> UnifiedTimeline:
    """Build globally sorted frames keyed by candle close time.

    Input order is irrelevant. Missing symbols remain missing at a timestamp;
    no price or candle is copied from an earlier time or another symbol.
    """

    grouped: dict[datetime, dict[str, Candle]] = {}
    for symbol in sorted(candles_by_symbol):
        for candle in candles_by_symbol[symbol]:
            if candle.symbol != symbol:
                raise TimelineError(
                    f"mapping key {symbol} does not match candle symbol {candle.symbol}"
                )
            available_at = candle.close_time
            by_symbol = grouped.setdefault(available_at, {})
            if symbol in by_symbol:
                raise DuplicateTimelineCandleError(
                    f"duplicate candle for {symbol} at {available_at.isoformat()}"
                )
            by_symbol[symbol] = candle

    frames = tuple(
        TimelineFrame(
            timestamp=timestamp,
            candles=tuple(by_symbol[symbol] for symbol in sorted(by_symbol)),
        )
        for timestamp, by_symbol in sorted(grouped.items())
    )
    return UnifiedTimeline(frames)


def rank_signals(signals: Iterable[SignalIntent]) -> tuple[SignalIntent, ...]:
    """Rank signals deterministically: priority, score, symbol, strategy."""

    def key(signal: SignalIntent) -> tuple[object, ...]:
        has_priority = signal.strategy_priority is not None
        has_score = signal.score is not None
        return (
            0 if has_priority else 1,
            -(signal.strategy_priority or 0),
            0 if has_score else 1,
            -(signal.score if signal.score is not None else Decimal(0)),
            signal.symbol,
            signal.strategy_name,
            signal.strategy_version,
            signal.side.value,
        )

    return tuple(sorted(signals, key=key))


def _normalize_signals(
    result: SignalIntent | Iterable[SignalIntent] | None,
) -> tuple[SignalIntent, ...]:
    if result is None:
        return ()
    if isinstance(result, SignalIntent):
        return (result,)
    return tuple(result)


def iter_events(
    timeline: UnifiedTimeline,
    signal_evaluator: SignalEvaluator | None = None,
) -> Iterator[TimelineEvent]:
    """Yield the six Phase-3 event stages for every close timestamp.

    The evaluator is invoked only for symbols with a candle in the current
    frame. Its history is immutable, symbol-local, and ends at that candle.
    Signals are validated and ranked but never converted into executable orders
    or fills in Phase 3.
    """

    histories: dict[str, list[Candle]] = {}
    for frame in timeline:
        yield TimelineEvent(frame.timestamp, EventPhase.MARK_UPDATE, frame.candles)
        yield TimelineEvent(frame.timestamp, EventPhase.EXIT_CHECK, frame.candles)

        for candle in frame.candles:
            histories.setdefault(candle.symbol, []).append(candle)
        yield TimelineEvent(frame.timestamp, EventPhase.CANDLE_CLOSE, frame.candles)

        evaluated: list[SignalIntent] = []
        if signal_evaluator is not None:
            for candle in frame.candles:
                context = SignalContext(
                    timestamp=frame.timestamp,
                    candle=candle,
                    history=tuple(histories[candle.symbol]),
                )
                for signal in _normalize_signals(signal_evaluator(context)):
                    if signal.symbol != candle.symbol:
                        raise CausalityError("signal symbol must match its evaluated candle")
                    if signal.signal_time != frame.timestamp:
                        raise CausalityError(
                            "signal_time must equal the current closed candle time"
                        )
                    evaluated.append(signal)

        evaluated_tuple = tuple(evaluated)
        yield TimelineEvent(
            frame.timestamp,
            EventPhase.SIGNAL_EVALUATION,
            frame.candles,
            evaluated_tuple,
        )
        ranked = rank_signals(evaluated_tuple)
        yield TimelineEvent(
            frame.timestamp,
            EventPhase.SIGNAL_RANKING,
            frame.candles,
            ranked,
        )
        yield TimelineEvent(
            frame.timestamp,
            EventPhase.PENDING_ORDER_CREATION,
            frame.candles,
            ranked,
        )
