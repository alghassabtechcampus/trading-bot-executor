"""Strict causal resampling for validated V2 market data."""

from __future__ import annotations

from datetime import timedelta

from .models import Candle


FIVE_MINUTES = timedelta(minutes=5)
ONE_HOUR = timedelta(hours=1)
EXPECTED_FIVE_MINUTE_BARS = 12


def resample_5m_to_1h(candles: tuple[Candle, ...] | list[Candle]) -> tuple[Candle, ...]:
    """Return only complete UTC-hour buckets containing all twelve 5m bars.

    Output timestamps are hour-open times. A consumer must treat each output bar
    as available only at ``Candle.close_time`` (the end of that hour).
    Incomplete edge or interior buckets are omitted, never synthesized.
    """

    buckets: dict[object, list[Candle]] = {}
    for candle in candles:
        if candle.timeframe != FIVE_MINUTES:
            raise ValueError("resample_5m_to_1h accepts only 5-minute candles")
        hour_start = candle.timestamp.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour_start, []).append(candle)

    output: list[Candle] = []
    for hour_start in sorted(buckets):
        group = sorted(buckets[hour_start], key=lambda c: c.timestamp)
        expected_times = [hour_start + i * FIVE_MINUTES for i in range(12)]
        actual_times = [c.timestamp for c in group]
        if len(group) != EXPECTED_FIVE_MINUTE_BARS or actual_times != expected_times:
            continue
        symbols = {c.symbol for c in group}
        if len(symbols) != 1:
            raise ValueError("cannot resample a bucket containing multiple symbols")
        output.append(Candle(
            symbol=group[0].symbol,
            timestamp=hour_start,
            timeframe=ONE_HOUR,
            open=group[0].open,
            high=max(c.high for c in group),
            low=min(c.low for c in group),
            close=group[-1].close,
            volume=sum((c.volume for c in group), start=group[0].volume * 0),
        ))
    return tuple(output)
