from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backtest.v2.models import Candle, RejectionReason, SignalIntent, SignalSide
from backtest.v2.timeline import (
    EVENT_PHASE_ORDER,
    CausalityError,
    DuplicateProtectionState,
    DuplicateTimelineCandleError,
    EventPhase,
    build_unified_timeline,
    iter_events,
    rank_signals,
)


TF = timedelta(minutes=5)
START = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def candle(symbol: str, offset_minutes: int) -> Candle:
    base = Decimal("100") + Decimal(offset_minutes)
    return Candle(
        symbol=symbol,
        timestamp=START + timedelta(minutes=offset_minutes),
        timeframe=TF,
        open=base,
        high=base + Decimal("2"),
        low=base - Decimal("1"),
        close=base + Decimal("1"),
        volume=Decimal("10"),
    )


def signal(
    symbol: str,
    signal_time: datetime,
    *,
    priority: int | None = None,
    score: str | None = None,
    strategy: str = "test",
) -> SignalIntent:
    return SignalIntent(
        symbol=symbol,
        signal_time=signal_time,
        side=SignalSide.BUY,
        strategy_name=strategy,
        strategy_version="1",
        strategy_priority=priority,
        score=Decimal(score) if score is not None else None,
    )


class UnifiedTimelineTests(unittest.TestCase):
    def test_timestamps_are_sorted_globally_and_symbols_deterministically(self):
        timeline = build_unified_timeline({
            "ZZZ": [candle("ZZZ", 5), candle("ZZZ", 0)],
            "AAA": [candle("AAA", 0)],
        })
        self.assertEqual(
            [frame.timestamp for frame in timeline.frames],
            [START + timedelta(minutes=5), START + timedelta(minutes=10)],
        )
        self.assertEqual(
            [c.symbol for c in timeline.frames[0].candles],
            ["AAA", "ZZZ"],
        )

    def test_input_order_does_not_change_frames_or_ranked_signals(self):
        first = build_unified_timeline({
            "BBB": [candle("BBB", 0)],
            "AAA": [candle("AAA", 0)],
        })
        second = build_unified_timeline({
            "AAA": [candle("AAA", 0)],
            "BBB": [candle("BBB", 0)],
        })

        def evaluator(context):
            return signal(context.candle.symbol, context.timestamp, score="50")

        first_events = list(iter_events(first, evaluator))
        second_events = list(iter_events(second, evaluator))
        self.assertEqual(first.frames, second.frames)
        self.assertEqual(first_events, second_events)

    def test_missing_symbol_has_no_candle_no_forward_fill_and_no_signal(self):
        timeline = build_unified_timeline({
            "AAA": [candle("AAA", 0)],
            "BBB": [candle("BBB", 0), candle("BBB", 5)],
        })
        evaluated_symbols: list[tuple[datetime, str]] = []

        def evaluator(context):
            evaluated_symbols.append((context.timestamp, context.candle.symbol))
            return signal(context.candle.symbol, context.timestamp)

        events = list(iter_events(timeline, evaluator))
        second_time = START + timedelta(minutes=10)
        second_close = next(
            event for event in events
            if event.timestamp == second_time and event.phase is EventPhase.CANDLE_CLOSE
        )
        second_signals = next(
            event for event in events
            if event.timestamp == second_time and event.phase is EventPhase.SIGNAL_EVALUATION
        )
        self.assertEqual([c.symbol for c in second_close.candles], ["BBB"])
        self.assertIsNone(timeline.frames[1].candle_for("AAA"))
        self.assertEqual([s.symbol for s in second_signals.signals], ["BBB"])
        self.assertNotIn((second_time, "AAA"), evaluated_symbols)

    def test_duplicate_timestamp_for_same_symbol_is_rejected(self):
        duplicate = candle("AAA", 0)
        with self.assertRaises(DuplicateTimelineCandleError):
            build_unified_timeline({"AAA": [duplicate, duplicate]})

    def test_event_phase_order_is_explicit_and_stable(self):
        timeline = build_unified_timeline({"AAA": [candle("AAA", 0)]})
        events = list(iter_events(timeline))
        self.assertEqual(tuple(event.phase for event in events), EVENT_PHASE_ORDER)


class SignalRankingTests(unittest.TestCase):
    def test_simultaneous_signals_rank_priority_then_score_then_symbol(self):
        t = START + TF
        ranked = rank_signals([
            signal("CCC", t, priority=None, score="100"),
            signal("BBB", t, priority=5, score="10"),
            signal("AAA", t, priority=5, score="10"),
            signal("DDD", t, priority=5, score="20"),
        ])
        self.assertEqual([item.symbol for item in ranked], ["DDD", "AAA", "BBB", "CCC"])

    def test_ranking_is_independent_of_signal_input_order(self):
        t = START + TF
        signals = [
            signal("CCC", t, score="1"),
            signal("AAA", t, score="2"),
            signal("BBB", t, score="2"),
        ]
        self.assertEqual(rank_signals(signals), rank_signals(reversed(signals)))


class CausalityTests(unittest.TestCase):
    def test_context_never_contains_future_candle(self):
        timeline = build_unified_timeline({
            "AAA": [candle("AAA", 0), candle("AAA", 5)],
        })
        observations: list[tuple[datetime, tuple[datetime, ...]]] = []

        def evaluator(context):
            observations.append(
                (context.timestamp, tuple(item.close_time for item in context.history))
            )
            return None

        list(iter_events(timeline, evaluator))
        self.assertEqual(len(observations), 2)
        for current_time, available_times in observations:
            self.assertTrue(all(item <= current_time for item in available_times))
        self.assertEqual(observations[0][1], (START + TF,))

    def test_future_dated_signal_is_rejected(self):
        timeline = build_unified_timeline({"AAA": [candle("AAA", 0)]})

        def evaluator(context):
            return signal("AAA", context.timestamp + TF)

        with self.assertRaises(CausalityError):
            list(iter_events(timeline, evaluator))


class ArchitectureCompletenessTests(unittest.TestCase):
    def test_rejection_enum_is_complete(self):
        self.assertEqual(
            {item.value for item in RejectionReason},
            {
                "MAX_CONCURRENT_REACHED",
                "DUPLICATE_SYMBOL",
                "INSUFFICIENT_CASH",
                "NO_NEXT_BAR",
                "INVALID_SIZE",
                "STALE_SIGNAL",
                "INVALID_STOP",
                "MISSING_STOP",
            },
        )

    def test_duplicate_protection_covers_active_and_pending_symbols(self):
        state = DuplicateProtectionState()
        self.assertIsNone(state.register_pending_buy("BTCUSDT"))
        self.assertEqual(
            state.register_pending_buy("BTCUSDT"),
            RejectionReason.DUPLICATE_SYMBOL,
        )
        state.mark_position_active("BTCUSDT")
        self.assertEqual(
            state.buy_rejection("BTCUSDT"),
            RejectionReason.DUPLICATE_SYMBOL,
        )
        state.mark_position_closed("BTCUSDT")
        self.assertIsNone(state.buy_rejection("BTCUSDT"))


if __name__ == "__main__":
    unittest.main()
