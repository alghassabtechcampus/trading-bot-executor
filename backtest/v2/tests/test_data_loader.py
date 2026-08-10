from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest.v2.data_loader import DataLoadError, load_market_data, load_symbol_csv
from backtest.v2.validation import DataValidationError


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def write_csv(path: Path, rows: list[tuple[datetime, str, str, str, str, str]]) -> None:
    lines = ["timestamp,open,high,low,close,volume"]
    lines.extend(
        f"{int(ts.timestamp() * 1000)},{open_},{high},{low},{close},{volume}"
        for ts, open_, high, low, close, volume in rows
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class DataLoaderTests(unittest.TestCase):
    def test_loads_decimals_slices_deterministically_and_keeps_warmup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ETHUSDT_5m.csv"
            rows = [
                (T0 + timedelta(minutes=5 * i), "100", "102", "99", "101", "10")
                for i in range(5)
            ]
            write_csv(path, rows)
            loaded = load_symbol_csv(
                path,
                symbol="ETHUSDT",
                start=T0 + timedelta(minutes=15),
                end=T0 + timedelta(minutes=25),
                data_cutoff=T0 + timedelta(hours=1),
                warmup_candles=2,
            )
            self.assertEqual(loaded.warmup_count, 2)
            self.assertEqual(len(loaded.candles), 4)
            self.assertEqual(loaded.in_range_count, 2)
            self.assertEqual(str(loaded.candles[0].open), "100")

    def test_removes_trailing_incomplete_candle_without_touching_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ETHUSDT_5m.csv"
            rows = [
                (T0, "100", "102", "99", "101", "10"),
                (T0 + timedelta(minutes=5), "101", "103", "100", "102", "11"),
            ]
            write_csv(path, rows)
            before = path.read_bytes()
            loaded = load_symbol_csv(
                path, symbol="ETHUSDT", start=T0,
                end=T0 + timedelta(minutes=10),
                data_cutoff=T0 + timedelta(minutes=7),
            )
            self.assertTrue(loaded.validation_report.removed_incomplete_last_candle)
            self.assertEqual(len(loaded.candles), 1)
            self.assertEqual(path.read_bytes(), before)

    def test_gap_is_rejected_not_forward_filled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ETHUSDT_5m.csv"
            write_csv(path, [
                (T0, "100", "101", "99", "100", "1"),
                (T0 + timedelta(minutes=10), "100", "101", "99", "100", "1"),
            ])
            with self.assertRaises(DataValidationError):
                load_symbol_csv(
                    path, symbol="ETHUSDT", start=T0,
                    end=T0 + timedelta(minutes=15),
                    data_cutoff=T0 + timedelta(hours=1),
                )

    def test_market_loader_requires_unique_symbols_and_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DataLoadError):
                load_market_data(
                    directory, symbols=("ETHUSDT", "ETHUSDT"), start=T0,
                    end=T0 + timedelta(hours=1), data_cutoff=T0 + timedelta(hours=2),
                )
            with self.assertRaises(DataLoadError):
                load_market_data(
                    directory, symbols=("ETHUSDT",), start=T0,
                    end=T0 + timedelta(hours=1), data_cutoff=T0 + timedelta(hours=2),
                )


if __name__ == "__main__":
    unittest.main()
