"""Strategy D signal bridge: Bybit klines -> StrategyDAdapter -> POST /execute.

Reuses the EXACT backtest adapter, so live signals are identical to the
walk-forward. Emits at most one signal per symbol per closed UTC hour (the
adapter only fires on the hour-close bar). Schedule it every ~5 min (or hourly
just after HH:00) via cron/n8n; off-hour runs simply produce no signal.

Env:
  BOT_URL                base URL of app.py            (default http://localhost:5000)
  TRADING_WEBHOOK_SECRET must match the bot's secret   (required unless DRY_RUN)
  BYBIT_MARKET_URL       kline source                  (default https://api.bybit.com)
  SIGNAL_SYMBOLS         comma list                    (default 9 majors)
  SIGNAL_NOTIONAL_USDT   quote amount per trade        (default 100; <= bot TRADE_AMOUNT_USDT)
  DRY_RUN                1 = log only, do not POST      (default 0)

Market data is PUBLIC (no API key here). For testnet paper trading point
BYBIT_MARKET_URL at https://api-testnet.bybit.com so signal prices match the
testnet book the bot trades on.
"""
from __future__ import annotations

import argparse
import hashlib  # noqa: F401  (reserved: some deployments sign the webhook)
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests

from backtest.v2.adapters.strategy_d import StrategyDAdapter
from backtest.v2.models import Candle

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("strategy-d-runner")

BOT_URL = os.getenv("BOT_URL", "http://localhost:5000").rstrip("/")
WEBHOOK_SECRET = os.getenv("TRADING_WEBHOOK_SECRET", "")
BYBIT_MARKET_URL = os.getenv("BYBIT_MARKET_URL", "https://api.bybit.com").rstrip("/")
SYMBOLS = tuple(
    s.strip().upper()
    for s in os.getenv(
        "SIGNAL_SYMBOLS",
        "ADAUSDT,BNBUSDT,BTCUSDT,DOGEUSDT,ETHUSDT,LINKUSDT,LTCUSDT,SOLUSDT,XRPUSDT",
    ).split(",")
    if s.strip()
)
SIGNAL_NOTIONAL_USDT = os.getenv("SIGNAL_NOTIONAL_USDT", "100")
DRY_RUN = os.getenv("DRY_RUN", "0").strip() in {"1", "true", "True"}
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12"))

FIVE_MINUTES = timedelta(minutes=5)
NEED_CANDLES = StrategyDAdapter.window_size + 20  # small margin over window


def _fetch_klines(symbol: str, need: int) -> list[Candle]:
    """Fetch >= `need` closed 5m candles (public endpoint), oldest-first."""
    collected: dict[int, list[str]] = {}
    end_cursor: int | None = None
    while len(collected) < need:
        params = {"category": "spot", "symbol": symbol, "interval": "5", "limit": 1000}
        if end_cursor is not None:
            params["end"] = end_cursor
        resp = requests.get(
            f"{BYBIT_MARKET_URL}/v5/market/kline", params=params, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(f"kline error {symbol}: {payload.get('retMsg')}")
        rows = payload.get("result", {}).get("list", [])
        if not rows:
            break
        for row in rows:
            collected[int(row[0])] = row
        oldest = min(int(r[0]) for r in rows)
        if end_cursor is not None and oldest >= end_cursor:
            break
        end_cursor = oldest - 1

    now = datetime.now(timezone.utc)
    candles: list[Candle] = []
    for start_ms in sorted(collected):
        row = collected[start_ms]
        ts = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        candle = Candle(
            symbol=symbol, timestamp=ts, timeframe=FIVE_MINUTES,
            open=Decimal(row[1]), high=Decimal(row[2]), low=Decimal(row[3]),
            close=Decimal(row[4]), volume=Decimal(row[5]),
        )
        if candle.close_time <= now:  # drop the still-forming candle
            candles.append(candle)
    return candles


def _post_execute(payload: dict) -> None:
    headers = {"Content-Type": "application/json", "X-Webhook-Signature": WEBHOOK_SECRET}
    resp = requests.post(
        f"{BOT_URL}/execute", data=json.dumps(payload), headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
    status = body.get("status") if isinstance(body, dict) else resp.status_code
    logger.info("execute %s -> HTTP %s status=%s", payload["symbol"], resp.status_code, status)


def run_once() -> None:
    for symbol in SYMBOLS:
        try:
            history = _fetch_klines(symbol, NEED_CANDLES)
            if len(history) < StrategyDAdapter.window_size:
                logger.info("%s: only %d candles, need %d — skip",
                            symbol, len(history), StrategyDAdapter.window_size)
                continue
            signal = StrategyDAdapter().evaluate(history)
            if signal is None or signal.action != "BUY_NOW":
                logger.info("%s: no signal", symbol)
                continue

            hour_tag = history[-1].close_time.strftime("%Y%m%dT%H")
            payload = {
                "symbol": symbol,
                "action": "BUY",
                "price": str(signal.reference_price),
                "stopLoss": str(signal.stop_loss),
                "takeProfit": str(signal.take_profit),
                "qty": SIGNAL_NOTIONAL_USDT,
                "signal_id": f"D-{symbol}-{hour_tag}",
                "trade_id": f"D-{symbol}-{int(time.time())}",
            }
            logger.info("SIGNAL %s stretch=%s price=%s stop=%s target=%s",
                        symbol, signal.metadata.get("stretch_atr"),
                        payload["price"], payload["stopLoss"], payload["takeProfit"])
            if DRY_RUN:
                logger.info("DRY_RUN — not posting: %s", json.dumps(payload, ensure_ascii=False))
            else:
                _post_execute(payload)
        except Exception:
            logger.exception("runner failed for %s", symbol)


def main() -> int:
    parser = argparse.ArgumentParser(description="Strategy D live signal runner")
    parser.add_argument("--loop", type=int, default=0,
                        help="seconds between passes; 0 = single pass (default)")
    args = parser.parse_args()
    if not WEBHOOK_SECRET and not DRY_RUN:
        logger.warning("TRADING_WEBHOOK_SECRET is empty; /execute will 401. Set it or use DRY_RUN=1.")

    if args.loop <= 0:
        run_once()
        return 0
    while True:
        run_once()
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
