"""End-to-end Strategy A integration runner for Backtest Engine V2."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .adapters.base import AdapterSignal, StrategyAdapter
from .config import EndOfTestPolicy, RunConfig
from .costs import buy_costed_price
from .execution import execute_intrabar_exit, execute_market_exit, execute_pending_entry
from .models import (
    Candle,
    ClosedTrade,
    ExecutionReason,
    ExitReason,
    PendingOrder,
    PendingOrderStatus,
    RejectionReason,
    SignalIntent,
    SignalSide,
)
from .portfolio import PortfolioState, Position
from .sizing import InstrumentConstraints, PositionSizer
from .timeline import build_unified_timeline, rank_signals


ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


@dataclass(frozen=True, slots=True)
class IntegrationRunConfig:
    run: RunConfig
    symbols: tuple[str, ...]
    start: datetime
    end: datetime
    instrument_constraints: Mapping[str, InstrumentConstraints] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be non-empty and unique")
        for name, value in (("start", self.start), ("end", self.end)):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError(f"{name} must be timezone-aware UTC")
        if self.start >= self.end:
            raise ValueError("start must be earlier than end")
        if self.run.end_of_test_policy is not EndOfTestPolicy.CLOSE_AT_END:
            raise ValueError("the first integration runner requires CLOSE_AT_END")
        unknown = set(self.instrument_constraints) - set(self.symbols)
        if unknown:
            raise ValueError(f"constraints supplied for unknown symbols: {sorted(unknown)}")
        object.__setattr__(
            self, "instrument_constraints", MappingProxyType(dict(self.instrument_constraints))
        )


@dataclass(frozen=True, slots=True)
class SignalRejection:
    timestamp: datetime
    symbol: str
    signal_time: datetime
    reason: RejectionReason
    score: Decimal | None
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class BasicRunSummary:
    initial_equity: Decimal
    final_equity: Decimal
    net_profit: Decimal
    net_return_pct: Decimal
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal
    total_fees: Decimal
    total_slippage_cost: Decimal
    total_spread_cost: Decimal
    max_drawdown_pct: Decimal
    exit_reason_counts: Mapping[ExitReason, int]
    rejection_counts: Mapping[RejectionReason, int]


@dataclass(frozen=True, slots=True)
class BacktestRunResult:
    trades: tuple[ClosedTrade, ...]
    rejections: tuple[SignalRejection, ...]
    summary: BasicRunSummary
    candles_processed: int
    signals: int
    orders: int
    fills: int


@dataclass(slots=True)
class _TradeTracker:
    signal_time: datetime
    entry_reference_price: Decimal
    entry_fill_price: Decimal
    entry_spread_cost: Decimal
    entry_slippage_cost: Decimal
    score: Decimal | None
    metadata: Mapping[str, Any]
    best_price: Decimal
    worst_price: Decimal


class IntegrationEngine:
    def __init__(self, config: IntegrationRunConfig, adapter: StrategyAdapter) -> None:
        self.config = config
        self.adapter = adapter

    def run(self, candles_by_symbol: Mapping[str, Sequence[Candle]]) -> BacktestRunResult:
        if set(candles_by_symbol) != set(self.config.symbols):
            raise ValueError("candles_by_symbol must exactly match configured symbols")
        timeline = build_unified_timeline(candles_by_symbol)
        portfolio = PortfolioState.create(
            initial_capital=self.config.run.initial_capital,
            financial_assumptions=self.config.run.financial_assumptions,
            timestamp=self.config.start,
        )
        sizer = PositionSizer(self.config.run)
        histories: dict[str, list[Candle]] = {symbol: [] for symbol in self.config.symbols}
        pending: dict[str, PendingOrder] = {}
        trackers: dict[str, _TradeTracker] = {}
        trades: list[ClosedTrade] = []
        rejections: list[SignalRejection] = []
        latest_candle: dict[str, Candle] = {}
        signal_count = order_count = fill_count = candles_processed = 0
        order_sequence = 0

        for frame in timeline:
            if frame.timestamp > self.config.end:
                break
            for candle in frame.candles:
                histories[candle.symbol].append(candle)
                latest_candle[candle.symbol] = candle
            if frame.timestamp <= self.config.start:
                continue
            candles_processed += len(frame.candles)
            frame_by_symbol = {candle.symbol: candle for candle in frame.candles}

            # Orders created at the prior close execute at this bar's open.
            for symbol in sorted(tuple(pending)):
                candle = frame_by_symbol.get(symbol)
                if candle is None:
                    continue
                order = pending.pop(symbol)
                execution = execute_pending_entry(
                    order, (candle,), self.config.run.financial_assumptions
                )
                if execution.rejection is not None:
                    self._reject(rejections, order, execution.rejection.reason, frame.timestamp)
                    continue
                fill = execution.fill
                assert fill is not None
                rejection = portfolio.open_position(
                    fill,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    strategy_name=order.strategy_name,
                    strategy_version=order.strategy_version,
                    metadata=order.metadata,
                )
                if rejection is not None:
                    self._reject(rejections, order, rejection, fill.timestamp)
                    continue
                fill_count += 1
                trackers[symbol] = _TradeTracker(
                    signal_time=order.signal_time,
                    entry_reference_price=fill.reference_price,
                    entry_fill_price=fill.fill_price,
                    entry_spread_cost=fill.spread_cost,
                    entry_slippage_cost=fill.slippage_cost,
                    score=order.score,
                    metadata=order.metadata,
                    best_price=fill.fill_price,
                    worst_price=fill.fill_price,
                )

            # Existing positions, including entries at this bar's open.
            for symbol in sorted(tuple(portfolio.positions)):
                candle = frame_by_symbol.get(symbol)
                if candle is None:
                    continue
                position = portfolio.positions[symbol]
                tracker = trackers[symbol]
                exit_execution = execute_intrabar_exit(
                    order_id=f"exit-{symbol}-{int(frame.timestamp.timestamp())}",
                    symbol=symbol,
                    candle=candle,
                    stop_loss=position.stop_loss,
                    take_profit=position.take_profit,
                    quantity=position.quantity,
                    assumptions=self.config.run.financial_assumptions,
                    policy=self.config.run.intrabar_policy,
                )
                if exit_execution is not None:
                    if exit_execution.exit_reason is ExitReason.STOP_LOSS:
                        tracker.worst_price = min(
                            tracker.worst_price, exit_execution.fill.reference_price
                        )
                    else:
                        tracker.best_price = max(
                            tracker.best_price, exit_execution.fill.reference_price
                        )
                    self._close_trade(
                        portfolio, position, tracker, exit_execution.fill,
                        exit_execution.exit_reason, exit_execution.intrabar_ambiguous, trades,
                    )
                    fill_count += 1
                    del trackers[symbol]
                    continue

                tracker.best_price = max(tracker.best_price, candle.high)
                tracker.worst_price = min(tracker.worst_price, candle.low)
                if candle.close_time - position.entry_time >= timedelta(
                    minutes=self.adapter.max_hold_minutes
                ):
                    fill = execute_market_exit(
                        order_id=f"max-hold-{symbol}-{int(frame.timestamp.timestamp())}",
                        symbol=symbol,
                        timestamp=candle.close_time,
                        reference_price=candle.close,
                        quantity=position.quantity,
                        assumptions=self.config.run.financial_assumptions,
                    )
                    self._close_trade(
                        portfolio, position, tracker, fill,
                        ExitReason.MAX_HOLD, False, trades,
                    )
                    fill_count += 1
                    del trackers[symbol]

            portfolio.mark_to_market(timestamp=frame.timestamp, candles=frame.candles)

            candidates: list[tuple[SignalIntent, AdapterSignal]] = []
            for candle in frame.candles:
                evaluated = self.adapter.evaluate(histories[candle.symbol])
                if evaluated is None or evaluated.action != "BUY_NOW":
                    continue
                signal_count += 1
                intent = SignalIntent(
                    symbol=candle.symbol,
                    signal_time=frame.timestamp,
                    side=SignalSide.BUY,
                    strategy_name=self.adapter.strategy_name,
                    strategy_version=self.adapter.strategy_version,
                    score=evaluated.score,
                )
                candidates.append((intent, evaluated))

            by_symbol = {intent.symbol: (intent, signal) for intent, signal in candidates}
            for intent in rank_signals(intent for intent, _ in candidates):
                signal = by_symbol[intent.symbol][1]
                metadata = dict(signal.metadata)
                metadata["action"] = signal.action
                metadata["score"] = str(signal.score)
                if intent.symbol in portfolio.positions or intent.symbol in pending:
                    self._reject_intent(
                        rejections, intent, RejectionReason.DUPLICATE_SYMBOL, metadata
                    )
                    continue
                if signal.stop_loss is None:
                    self._reject_intent(
                        rejections, intent, RejectionReason.MISSING_STOP, metadata
                    )
                    continue
                if signal.stop_loss >= signal.reference_price:
                    self._reject_intent(
                        rejections, intent, RejectionReason.INVALID_STOP, metadata
                    )
                    continue
                if len(portfolio.positions) + len(pending) >= self.config.run.max_concurrent_positions:
                    self._reject_intent(
                        rejections, intent, RejectionReason.MAX_CONCURRENT_REACHED, metadata
                    )
                    continue
                unit_cost = buy_costed_price(
                    signal.reference_price, Decimal("1"),
                    self.config.run.financial_assumptions,
                )
                sizing_order = PendingOrder(
                    order_id="sizing-only",
                    symbol=intent.symbol,
                    side=SignalSide.BUY,
                    signal_time=frame.timestamp,
                    eligible_from=frame.timestamp,
                    strategy_name=intent.strategy_name,
                    strategy_version=intent.strategy_version,
                    reference_price=signal.reference_price,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    score=signal.score,
                    priority=intent.strategy_priority,
                    metadata=metadata,
                    status=PendingOrderStatus.PENDING,
                )
                sizing = sizer.size(
                    portfolio=portfolio,
                    order=sizing_order,
                    estimated_entry_fill_price=unit_cost.fill_price,
                    constraints=self.config.instrument_constraints.get(intent.symbol),
                )
                if sizing.rejection_reason is not None:
                    self._reject_intent(
                        rejections, intent, sizing.rejection_reason, metadata
                    )
                    continue
                order_sequence += 1
                pending[intent.symbol] = PendingOrder(
                    order_id=f"entry-{order_sequence:08d}",
                    symbol=intent.symbol,
                    side=SignalSide.BUY,
                    signal_time=frame.timestamp,
                    eligible_from=frame.timestamp,
                    strategy_name=intent.strategy_name,
                    strategy_version=intent.strategy_version,
                    reference_price=signal.reference_price,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    score=signal.score,
                    priority=intent.strategy_priority,
                    metadata=metadata,
                    status=PendingOrderStatus.PENDING,
                    quantity=sizing.final_quantity,
                )
                order_count += 1

        for order in sorted(pending.values(), key=lambda item: (item.symbol, item.order_id)):
            self._reject(rejections, order, RejectionReason.NO_NEXT_BAR, self.config.end)

        for symbol in sorted(tuple(portfolio.positions)):
            position = portfolio.positions[symbol]
            candle = latest_candle[symbol]
            fill = execute_market_exit(
                order_id=f"end-{symbol}",
                symbol=symbol,
                timestamp=min(candle.close_time, self.config.end),
                reference_price=candle.close,
                quantity=position.quantity,
                assumptions=self.config.run.financial_assumptions,
            )
            self._close_trade(
                portfolio, position, trackers[symbol], fill,
                ExitReason.END_OF_TEST, False, trades,
            )
            fill_count += 1
        portfolio.record_end(self.config.end)

        summary = self._summary(portfolio, trades, rejections)
        return BacktestRunResult(
            trades=tuple(trades),
            rejections=tuple(rejections),
            summary=summary,
            candles_processed=candles_processed,
            signals=signal_count,
            orders=order_count,
            fills=fill_count,
        )

    @staticmethod
    def _reject(
        target: list[SignalRejection], order: PendingOrder,
        reason: RejectionReason, timestamp: datetime,
    ) -> None:
        target.append(SignalRejection(
            timestamp=timestamp,
            symbol=order.symbol,
            signal_time=order.signal_time,
            reason=reason,
            score=order.score,
            metadata=order.metadata,
        ))

    @staticmethod
    def _reject_intent(
        target: list[SignalRejection], intent: SignalIntent,
        reason: RejectionReason, metadata: Mapping[str, Any],
    ) -> None:
        target.append(SignalRejection(
            timestamp=intent.signal_time,
            symbol=intent.symbol,
            signal_time=intent.signal_time,
            reason=reason,
            score=intent.score,
            metadata=metadata,
        ))

    @staticmethod
    def _close_trade(
        portfolio: PortfolioState,
        position: Position,
        tracker: _TradeTracker,
        fill,
        reason: ExitReason,
        ambiguous: bool,
        trades: list[ClosedTrade],
    ) -> None:
        portfolio.close_position(fill)
        gross = fill.notional - position.entry_notional
        net = gross - position.entry_fee - fill.fee
        trades.append(ClosedTrade(
            entry_signal_time=tracker.signal_time,
            entry_fill_time=position.entry_time,
            exit_fill_time=fill.timestamp,
            entry_reference_price=tracker.entry_reference_price,
            entry_fill_price=position.entry_fill_price,
            exit_reference_price=fill.reference_price,
            exit_fill_price=fill.fill_price,
            quantity=position.quantity,
            entry_notional=position.entry_notional,
            exit_notional=fill.notional,
            gross_pnl=gross,
            net_pnl=net,
            net_return_pct=net / position.entry_notional * ONE_HUNDRED,
            entry_fee=position.entry_fee,
            exit_fee=fill.fee,
            spread_cost=tracker.entry_spread_cost + fill.spread_cost,
            slippage_cost=tracker.entry_slippage_cost + fill.slippage_cost,
            holding_duration=fill.timestamp - position.entry_time,
            exit_reason=reason,
            mfe=(tracker.best_price - position.entry_fill_price)
                / position.entry_fill_price * ONE_HUNDRED,
            mae=(tracker.worst_price - position.entry_fill_price)
                / position.entry_fill_price * ONE_HUNDRED,
            intrabar_ambiguous=ambiguous,
            strategy_name=position.strategy_name,
            strategy_version=position.strategy_version,
            symbol=position.symbol,
        ))

    def _summary(
        self,
        portfolio: PortfolioState,
        trades: Sequence[ClosedTrade],
        rejections: Sequence[SignalRejection],
    ) -> BasicRunSummary:
        final = portfolio.equity
        initial = self.config.run.initial_capital
        wins = sum(trade.net_pnl > ZERO for trade in trades)
        losses = len(trades) - wins
        exit_counts = Counter(trade.exit_reason for trade in trades)
        rejection_counts = Counter(item.reason for item in rejections)
        max_drawdown = abs(min(
            (point.drawdown_pct for point in portfolio.equity_curve),
            default=ZERO,
        ))
        return BasicRunSummary(
            initial_equity=initial,
            final_equity=final,
            net_profit=final - initial,
            net_return_pct=(final - initial) / initial * ONE_HUNDRED,
            total_trades=len(trades),
            winning_trades=wins,
            losing_trades=losses,
            win_rate=(Decimal(wins) / Decimal(len(trades)) * ONE_HUNDRED if trades else ZERO),
            total_fees=portfolio.total_fees,
            total_slippage_cost=portfolio.total_slippage_cost,
            total_spread_cost=portfolio.total_spread_cost,
            max_drawdown_pct=max_drawdown,
            exit_reason_counts=MappingProxyType(dict(exit_counts)),
            rejection_counts=MappingProxyType(dict(rejection_counts)),
        )
