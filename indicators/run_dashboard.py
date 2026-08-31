"""Main entry point: computes every indicator + confluence + trade zone for
all 9 pairs and writes one JSON file, structured for a frontend or a
Telegram bot to read directly. Run manually or on a schedule (cron, n8n,
or indicators/scheduler.py) to refresh the snapshot -- this script itself
only runs once per invocation, it does not loop or sleep.

Writes two things: the "latest" snapshot (overwritten every run, for
consumers that always want the current state) and a timestamped copy under
indicators/history/ (kept for an audit trail of what the dashboard showed
at each point in time) -- overwriting-only would lose that history, and
keeping only history would make "give me the current state" need a
directory scan, so both are written.

Usage: python -m indicators.run_dashboard [--out PATH] [--no-history]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indicators.config import load_config  # noqa: E402
from indicators.engine import compute_symbol  # noqa: E402
from indicators.sources import get_source  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("indicators.run_dashboard")

OUTPUT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = OUTPUT_DIR / "dashboard_snapshot.json"
HISTORY_DIR = OUTPUT_DIR / "history"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help="output JSON file path (latest snapshot)")
    parser.add_argument("--no-history", action="store_true", help="skip writing a timestamped history copy")
    args = parser.parse_args()

    config = load_config()
    source = get_source(config.market)
    symbols = source.get_available_symbols()

    print(f"Market: {config.market}  Config: trend={config.trend_timeframe} entry={config.entry_timeframe} "
          f"levels={config.levels_timeframe} lookback={config.levels_lookback_bars}")
    print(f"Symbols: {', '.join(symbols)}\n")

    pairs = {}
    for symbol in symbols:
        try:
            result = compute_symbol(symbol, config, source)
        except Exception as exc:
            logger.error(f"{symbol}: FAILED to compute ({exc})")
            pairs[symbol] = {"symbol": symbol, "error": str(exc)}
            continue

        pairs[symbol] = result
        conf = result["confluence"]
        setup = result["trade_zone"].get("setup", "none")
        stale_timeframes = [tf for tf, info in result["data_freshness"].items() if info["stale"]]
        freshness_note = f"  [STALE: {', '.join(stale_timeframes)}]" if stale_timeframes else ""
        if stale_timeframes:
            logger.warning(f"{symbol}: using stale cached data for {stale_timeframes}")
        print(f"  {symbol}: confluence={conf['summary']}  price={result['current_price']}  "
              f"setup={setup}{freshness_note}")

    generated_at = datetime.now(timezone.utc)
    snapshot = {
        "generated_at": generated_at.isoformat(),
        "config": {
            "market": config.market,
            "trend_timeframe": config.trend_timeframe,
            "entry_timeframe": config.entry_timeframe,
            "levels_timeframe": config.levels_timeframe,
        },
        "pairs": pairs,
    }

    payload = json.dumps(snapshot, indent=2)
    args.out.write_text(payload, encoding="utf-8")
    print(f"\nWrote {len(pairs)} pairs -> {args.out}")

    if not args.no_history:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        history_path = HISTORY_DIR / f"snapshot_{generated_at.strftime('%Y%m%dT%H%M%SZ')}.json"
        history_path.write_text(payload, encoding="utf-8")
        print(f"Wrote history copy -> {history_path}")


if __name__ == "__main__":
    main()
