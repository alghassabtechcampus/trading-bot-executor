"""Explicit command-line smoke runner for Strategy A on Backtest V2."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .adapters import VersionAAdapter
from .config import (
    EndOfTestPolicy,
    ExecutionProfile,
    FinancialAssumptions,
    PositionSizingMode,
    RunConfig,
)
from .data_loader import load_market_data
from .engine import IntegrationEngine, IntegrationRunConfig


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps must include UTC offset")
    utc = parsed.astimezone(timezone.utc)
    if utc.utcoffset().total_seconds() != 0:
        raise argparse.ArgumentTypeError("timestamps must resolve to UTC")
    return utc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Backtest V2 Strategy A")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--symbol", action="append", dest="symbols", required=True)
    parser.add_argument("--start", type=_utc, required=True)
    parser.add_argument("--end", type=_utc, required=True)
    parser.add_argument("--data-cutoff", type=_utc, required=True)
    parser.add_argument("--initial-capital", type=Decimal, required=True)
    parser.add_argument("--fixed-notional", type=Decimal, required=True)
    parser.add_argument("--max-concurrent-positions", type=int, required=True)
    parser.add_argument("--entry-fee-rate", type=Decimal, required=True)
    parser.add_argument("--exit-fee-rate", type=Decimal, required=True)
    parser.add_argument("--entry-slippage-bps", type=Decimal, required=True)
    parser.add_argument("--exit-slippage-bps", type=Decimal, required=True)
    parser.add_argument("--spread-bps", type=Decimal, required=True)
    parser.add_argument("--stop-slippage-bps", type=Decimal, required=True)
    parser.add_argument("--take-profit-slippage-bps", type=Decimal, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    assumptions = FinancialAssumptions(
        entry_fee_rate=args.entry_fee_rate,
        exit_fee_rate=args.exit_fee_rate,
        entry_slippage_bps=args.entry_slippage_bps,
        exit_slippage_bps=args.exit_slippage_bps,
        spread_bps=args.spread_bps,
        stop_slippage_bps=args.stop_slippage_bps,
        take_profit_slippage_bps=args.take_profit_slippage_bps,
    )
    run = RunConfig(
        execution_profile=ExecutionProfile.CUSTOM,
        financial_assumptions=assumptions,
        initial_capital=args.initial_capital,
        base_currency="USDT",
        max_concurrent_positions=args.max_concurrent_positions,
        position_sizing_mode=PositionSizingMode.FIXED_NOTIONAL,
        fixed_notional=args.fixed_notional,
        risk_per_trade=None,
        end_of_test_policy=EndOfTestPolicy.CLOSE_AT_END,
    )
    symbols = tuple(args.symbols)
    loaded = load_market_data(
        args.data_dir,
        symbols=symbols,
        start=args.start,
        end=args.end,
        data_cutoff=args.data_cutoff,
        warmup_candles=VersionAAdapter.window_size - 1,
    )
    integration = IntegrationRunConfig(
        run=run, symbols=symbols, start=args.start, end=args.end
    )
    started = time.perf_counter()
    result = IntegrationEngine(integration, VersionAAdapter()).run({
        symbol: item.candles for symbol, item in loaded.items()
    })
    runtime = time.perf_counter() - started
    summary = result.summary
    output = {
        "strategy": "Version A",
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "symbols": list(symbols),
        "financial_assumptions": run.manifest_values()["financial_assumptions"],
        "initial_capital": str(run.initial_capital),
        "fixed_notional": str(run.fixed_notional),
        "candles_processed": result.candles_processed,
        "signals": result.signals,
        "orders": result.orders,
        "fills": result.fills,
        "closed_trades": summary.total_trades,
        "rejections": len(result.rejections),
        "initial_equity": str(summary.initial_equity),
        "final_equity": str(summary.final_equity),
        "net_return_pct": str(summary.net_return_pct),
        "exit_reason_counts": {
            reason.value: count for reason, count in summary.exit_reason_counts.items()
        },
        "rejection_counts": {
            reason.value: count for reason, count in summary.rejection_counts.items()
        },
        "validation": {
            symbol: {
                "input_count": item.validation_report.input_count,
                "validated_count": item.validation_report.output_count,
                "selected_count": len(item.candles),
                "warmup_count": item.warmup_count,
                "in_range_count": item.in_range_count,
                "removed_incomplete_last_candle": item.validation_report.removed_incomplete_last_candle,
                "issues": list(item.validation_report.issues),
            }
            for symbol, item in loaded.items()
        },
        "runtime_seconds": runtime,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
