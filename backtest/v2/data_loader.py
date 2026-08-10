"""Read-only CSV loading and strict validation for Backtest V2."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import Candle
from .validation import DataValidationReport, ValidationMode, validate_candles


FIVE_MINUTES = timedelta(minutes=5)
UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class DataLoadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LoadedSymbolData:
    symbol: str
    path: Path
    candles: tuple[Candle, ...]
    validation_report: DataValidationReport
    requested_start: datetime
    requested_end: datetime
    warmup_count: int

    @property
    def in_range_count(self) -> int:
        return sum(
            self.requested_start < candle.close_time <= self.requested_end
            for candle in self.candles
        )


def _require_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise DataLoadError(f"{name} must be timezone-aware UTC")


def _parse_timestamp(raw: str) -> datetime:
    try:
        if raw.strip().lstrip("-").isdigit():
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise DataLoadError(f"invalid timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise DataLoadError("CSV ISO timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def load_symbol_csv(
    path: str | Path,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    data_cutoff: datetime,
    warmup_candles: int = 0,
    validation_mode: ValidationMode = ValidationMode.STRICT,
) -> LoadedSymbolData:
    """Load, validate, and deterministically slice one symbol without filling gaps."""

    for name, value in (("start", start), ("end", end), ("data_cutoff", data_cutoff)):
        _require_utc(name, value)
    if start >= end:
        raise DataLoadError("start must be earlier than end")
    if isinstance(warmup_candles, bool) or warmup_candles < 0:
        raise DataLoadError("warmup_candles must be a non-negative integer")
    csv_path = Path(path)
    if not csv_path.is_file():
        raise DataLoadError(f"CSV file not found: {csv_path}")

    candles: list[Candle] = []
    try:
        with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"timestamp", "open", "high", "low", "close", "volume"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise DataLoadError(f"CSV must contain columns: {sorted(required)}")
            for row_number, row in enumerate(reader, start=2):
                try:
                    candles.append(Candle(
                        symbol=symbol,
                        timestamp=_parse_timestamp(row["timestamp"]),
                        timeframe=FIVE_MINUTES,
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=Decimal(row["volume"]),
                    ))
                except (InvalidOperation, KeyError) as exc:
                    raise DataLoadError(f"invalid numeric value at CSV row {row_number}") from exc
    except OSError as exc:
        raise DataLoadError(f"cannot read CSV: {csv_path}") from exc

    validated = validate_candles(
        candles,
        symbol=symbol,
        expected_timeframe=FIVE_MINUTES,
        data_cutoff=data_cutoff,
        alignment_origin=UTC_EPOCH,
        mode=validation_mode,
    )
    completed_through_end = [c for c in validated.candles if c.close_time <= end]
    first_in_range = next(
        (index for index, candle in enumerate(completed_through_end) if candle.close_time > start),
        len(completed_through_end),
    )
    slice_start = max(0, first_in_range - warmup_candles)
    selected = tuple(completed_through_end[slice_start:])
    actual_warmup = sum(c.close_time <= start for c in selected)
    return LoadedSymbolData(
        symbol=symbol,
        path=csv_path,
        candles=selected,
        validation_report=validated.report,
        requested_start=start,
        requested_end=end,
        warmup_count=actual_warmup,
    )


def load_market_data(
    data_dir: str | Path,
    *,
    symbols: tuple[str, ...],
    start: datetime,
    end: datetime,
    data_cutoff: datetime,
    warmup_candles: int = 0,
) -> dict[str, LoadedSymbolData]:
    if not symbols or len(set(symbols)) != len(symbols):
        raise DataLoadError("symbols must be non-empty and unique")
    directory = Path(data_dir)
    return {
        symbol: load_symbol_csv(
            directory / f"{symbol}_5m.csv",
            symbol=symbol,
            start=start,
            end=end,
            data_cutoff=data_cutoff,
            warmup_candles=warmup_candles,
        )
        for symbol in sorted(symbols)
    }
