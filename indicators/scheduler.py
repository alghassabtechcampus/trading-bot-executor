"""Standalone periodic runner for indicators/run_dashboard.py.

This project's existing live components (app.py, signal_runner.py) are
triggered periodically by an EXTERNAL n8n Cron node calling a script or
HTTP endpoint -- n8n holds no business logic itself. run_dashboard.py
already fits that exact pattern as a single clean invocation, so the
recommended way to run this dashboard periodically is the SAME one already
used in this project: add an n8n Cron node (interval = config's
scheduler_interval, default 1h) whose action is an Execute Command node
running:

    python -m indicators.run_dashboard

pointed at this repo's working directory. No new n8n workflow logic is
needed beyond that one node, and it needs no code from this file.

This script is the explicitly-requested standalone alternative for anyone
running the dashboard without n8n: an infinite loop that calls
run_dashboard.main() every `scheduler_interval` (from config.json /
DASHBOARD_SCHEDULER_INTERVAL), sleeping in between. Stdlib only. Runs
until interrupted (Ctrl+C) or killed.

Usage: python -m indicators.scheduler
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from indicators import run_dashboard  # noqa: E402
from indicators.config import load_config  # noqa: E402
from indicators.data import timeframe_to_ms  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("indicators.scheduler")


def main() -> None:
    config = load_config()
    interval_seconds = timeframe_to_ms(config.scheduler_interval) / 1000
    logger.info(f"Starting dashboard scheduler: every {config.scheduler_interval} "
                f"({interval_seconds:.0f}s), market={config.market}. Ctrl+C to stop.")

    while True:
        cycle_start = time.monotonic()
        try:
            run_dashboard.main()
        except Exception:
            logger.exception("dashboard run failed; will retry next cycle")

        elapsed = time.monotonic() - cycle_start
        sleep_for = max(0.0, interval_seconds - elapsed)
        logger.info(f"Cycle done in {elapsed:.1f}s. Sleeping {sleep_for:.0f}s until next run.")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
