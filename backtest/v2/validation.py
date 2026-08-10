"""Strict OHLCV validation used before every V2 backtest."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Iterable

from .models import Candle


class ValidationMode(str, Enum):
    STRICT = "STRICT"
    WARN = "WARN"


class DataValidationError(ValueError):
    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("; ".join(issues))


@dataclass(frozen=True, slots=True)
class DataValidationReport:
    symbol: str
    input_count: int
    output_count: int
    removed_incomplete_last_candle: bool
    duplicate_count: int
    gap_count: int
    invalid_ohlc_count: int
    invalid_volume_count: int
    misaligned_timestamp_count: int
    issues: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class ValidatedCandles:
    candles: tuple[Candle, ...]
    report: DataValidationReport


def _is_finite_decimal(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def validate_candles(
    candles: Iterable[Candle],
    *,
    symbol: str,
    expected_timeframe: timedelta,
    data_cutoff: datetime,
    alignment_origin: datetime,
    mode: ValidationMode = ValidationMode.STRICT,
) -> ValidatedCandles:
    """Validate candles and remove only a trailing incomplete candle.

    The function never sorts or deduplicates input silently. In STRICT mode any
    structural or value issue raises ``DataValidationError``.
    """

    rows = list(candles)
    input_count = len(rows)
    issues: list[str] = []
    removed_incomplete = False

    if data_cutoff.tzinfo is None or data_cutoff.utcoffset() != timedelta(0):
        raise ValueError("data_cutoff must be timezone-aware UTC")
    if alignment_origin.tzinfo is None or alignment_origin.utcoffset() != timedelta(0):
        raise ValueError("alignment_origin must be timezone-aware UTC")
    if expected_timeframe <= timedelta(0):
        raise ValueError("expected_timeframe must be positive")

    if rows and rows[-1].close_time > data_cutoff:
        rows.pop()
        removed_incomplete = True

    duplicate_count = 0
    gap_count = 0
    invalid_ohlc_count = 0
    invalid_volume_count = 0
    misaligned_count = 0
    seen: set[datetime] = set()
    previous: Candle | None = None
    interval_us = expected_timeframe // timedelta(microseconds=1)

    for index, candle in enumerate(rows):
        prefix = f"{symbol}[{index}]"
        if candle.symbol != symbol:
            issues.append(f"{prefix}: symbol mismatch ({candle.symbol})")
        if not candle.is_utc:
            issues.append(f"{prefix}: timestamp must be timezone-aware UTC")
        if candle.timeframe != expected_timeframe:
            issues.append(f"{prefix}: timeframe mismatch")
        if candle.timestamp in seen:
            duplicate_count += 1
            issues.append(f"{prefix}: duplicate timestamp {candle.timestamp.isoformat()}")
        seen.add(candle.timestamp)

        offset_us = (candle.timestamp - alignment_origin) // timedelta(microseconds=1)
        if offset_us % interval_us != 0:
            misaligned_count += 1
            issues.append(f"{prefix}: timestamp is not timeframe-aligned")

        prices = (candle.open, candle.high, candle.low, candle.close)
        valid_prices = all(_is_finite_decimal(v) and v > 0 for v in prices)
        valid_shape = (
            valid_prices
            and candle.high >= max(candle.open, candle.close)
            and candle.low <= min(candle.open, candle.close)
            and candle.high >= candle.low
        )
        if not valid_shape:
            invalid_ohlc_count += 1
            issues.append(f"{prefix}: invalid OHLC values")

        if not _is_finite_decimal(candle.volume) or candle.volume < 0:
            invalid_volume_count += 1
            issues.append(f"{prefix}: volume must be finite and non-negative")

        if previous is not None:
            delta = candle.timestamp - previous.timestamp
            if delta <= timedelta(0):
                issues.append(f"{prefix}: timestamps are not strictly increasing")
            elif delta != expected_timeframe:
                gap_count += 1
                issues.append(
                    f"{prefix}: expected {expected_timeframe}, got interval {delta}"
                )
        previous = candle

    report = DataValidationReport(
        symbol=symbol,
        input_count=input_count,
        output_count=len(rows),
        removed_incomplete_last_candle=removed_incomplete,
        duplicate_count=duplicate_count,
        gap_count=gap_count,
        invalid_ohlc_count=invalid_ohlc_count,
        invalid_volume_count=invalid_volume_count,
        misaligned_timestamp_count=misaligned_count,
        issues=tuple(issues),
    )
    if issues and mode is ValidationMode.STRICT:
        raise DataValidationError(issues)
    return ValidatedCandles(tuple(rows), report)
