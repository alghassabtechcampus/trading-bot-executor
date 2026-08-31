"""Read-only Flask viewer for the indicators dashboard.

A SEPARATE small Flask app from app.py (the live trading executor) rather
than a new route bolted onto it: app.py holds webhook secrets and places
real orders, and this page has no business being anywhere near that
surface. It only ever READS indicators/dashboard_snapshot.json (produced
by run_dashboard.py) and, for the candlestick chart and the on-demand
timeframe recompute, calls the existing DataSource.get_ohlcv() /
engine.compute_symbol() read functions -- nothing here computes a NEW kind
of indicator or touches indicators/*.py's calculation logic; it only lets
the browser pick which timeframe those existing calculations run on.

No trade is ever placed from this page. There is no button that could.

Routes:
  GET /                       -> the dashboard page (client-side rendered)
  GET /api/snapshot           -> raw contents of dashboard_snapshot.json
                                  (always the config.json/default timeframes --
                                  this is the periodic background run's output)
  GET /api/candles/<symbol>   -> recent OHLCV for the chart, optional
                                  ?entry=<timeframe> override; cached
                                  in-process per (symbol, timeframe) for
                                  CANDLE_CACHE_TTL_SECONDS
  GET /api/compute/<symbol>   -> on-demand full recompute (same shape as a
                                  snapshot's per-symbol entry) with optional
                                  ?trend=&entry=&levels= overrides, for the
                                  frontend's timeframe dropdowns. Cached
                                  in-process per (symbol, trend, entry,
                                  levels) for COMPUTE_CACHE_TTL_SECONDS so
                                  rapid dropdown changes don't hammer Bybit.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, render_template, request, Response  # noqa: E402

from indicators.alert_watcher import run_check as run_alert_check  # noqa: E402
from indicators.config import load_config  # noqa: E402
from indicators.engine import compute_symbol  # noqa: E402
from indicators.run_dashboard import DEFAULT_OUTPUT as SNAPSHOT_PATH  # noqa: E402
from indicators.sources import get_source  # noqa: E402

app = Flask(__name__)
_alerts_logger = logging.getLogger("indicators.check_alerts")

# The 5 timeframes the frontend's dropdowns offer, and the only ones this
# endpoint will act on -- matches what BybitLiveCryptoSource can natively
# fetch (see sources/bybit_live.py's _TIMEFRAME_TO_BYBIT_INTERVAL).
ALLOWED_TIMEFRAMES = ("15m", "1h", "4h", "1d", "1w")

CANDLE_CACHE_TTL_SECONDS = 55
COMPUTE_CACHE_TTL_SECONDS = 30
_candle_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_compute_cache: dict[tuple[str, str, str, str], tuple[float, dict]] = {}


def _load_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        return {"generated_at": None, "config": {}, "pairs": {},
                "error": "no snapshot yet -- run `python -m indicators.run_dashboard` at least once"}
    with SNAPSHOT_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _validated_timeframe(param_name: str, value: str | None, fallback: str) -> str:
    if value is None:
        return fallback
    if value not in ALLOWED_TIMEFRAMES:
        raise ValueError(f"{param_name}={value!r} is not one of {ALLOWED_TIMEFRAMES}")
    return value


@app.route("/")
def index() -> str:
    return render_template("dashboard.html")


@app.route("/api/snapshot")
def api_snapshot() -> Response:
    snapshot = _load_snapshot()
    resp = jsonify(snapshot)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/candles/<symbol>")
def api_candles(symbol: str) -> Response:
    config = load_config()
    try:
        entry_timeframe = _validated_timeframe("entry", request.args.get("entry"), config.entry_timeframe)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    cache_key = (symbol, entry_timeframe)
    now = time.monotonic()
    cached = _candle_cache.get(cache_key)
    if cached and now - cached[0] < CANDLE_CACHE_TTL_SECONDS:
        resp = jsonify(cached[1])
        resp.headers["Cache-Control"] = "no-store"
        return resp

    source = get_source(config.market)
    if symbol not in source.get_available_symbols():
        return jsonify({"error": f"unknown symbol {symbol!r}"}), 404

    try:
        df = source.get_ohlcv(symbol, entry_timeframe)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    bars = df.tail(150)
    candles = [
        {
            "time": int(ts // 1000),  # lightweight-charts wants UNIX seconds
            "open": float(o), "high": float(h), "low": float(l), "close": float(c),
        }
        for ts, o, h, l, c in zip(bars["timestamp"], bars["open"], bars["high"], bars["low"], bars["close"])
    ]
    _candle_cache[cache_key] = (now, candles)

    resp = jsonify(candles)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/compute/<symbol>")
def api_compute(symbol: str) -> Response:
    base_config = load_config()
    try:
        trend = _validated_timeframe("trend", request.args.get("trend"), base_config.trend_timeframe)
        entry = _validated_timeframe("entry", request.args.get("entry"), base_config.entry_timeframe)
        levels = _validated_timeframe("levels", request.args.get("levels"), base_config.levels_timeframe)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    source = get_source(base_config.market)
    if symbol not in source.get_available_symbols():
        return jsonify({"error": f"unknown symbol {symbol!r}"}), 404

    cache_key = (symbol, trend, entry, levels)
    now = time.monotonic()
    cached = _compute_cache.get(cache_key)
    if cached and now - cached[0] < COMPUTE_CACHE_TTL_SECONDS:
        resp = jsonify(cached[1])
        resp.headers["Cache-Control"] = "no-store"
        return resp

    run_config = dataclasses.replace(base_config, trend_timeframe=trend, entry_timeframe=entry,
                                      levels_timeframe=levels)
    try:
        result = compute_symbol(symbol, run_config, source)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    _compute_cache[cache_key] = (now, result)
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/check_alerts")
def api_check_alerts() -> Response:
    """Runs one full alert_watcher check (27 computations across the 3
    timeframe combos x 9 symbols), updates alert_state.json, and returns
    whatever alerts fired this run (often an empty list -- that's the
    normal case, not an error). This is a READ+STATE-UPDATE operation, not
    a trade: it never calls anything in app.py or places an order."""
    try:
        alerts = run_alert_check(log=_alerts_logger.info)
    except Exception as exc:
        return jsonify({"error": str(exc), "alerts": []}), 502

    resp = jsonify({"checked_at": time.time(), "count": len(alerts), "alerts": alerts})
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("DASHBOARD_PORT", "5050")), debug=False)
