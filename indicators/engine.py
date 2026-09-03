"""Per-symbol orchestration: loads each configured timeframe once, runs
every indicator, computes confluence and the suggested trade zone, and
assembles the result into one JSON-ready dict. Pure calculation -- this
module never places, sizes, or touches an actual order.

Depends only on the DataSource interface (indicators/sources/base.py), not
on any concrete source -- swapping `config.market` to a future TASI or US
source changes zero lines here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import adx, atr, confluence, ichimoku, macd, support_resistance, trade_zone, vwap
from .config import DashboardConfig
from .sources import DataSource


def _freshness(df) -> dict:
    """Reads the source/fetched_at/stale sidecar a live DataSource attaches
    via df.attrs (see BybitLiveCryptoSource); defaults describe a source
    that doesn't set these (e.g. the static offline source)."""
    return {
        "source": df.attrs.get("source", "unknown"),
        "fetched_at": df.attrs.get("fetched_at"),
        "stale": df.attrs.get("stale", False),
    }


def compute_symbol(symbol: str, config: DashboardConfig, source: DataSource) -> dict:
    trend_df = source.get_ohlcv(symbol, config.trend_timeframe)
    entry_df = source.get_ohlcv(symbol, config.entry_timeframe)
    levels_df = source.get_ohlcv(symbol, config.levels_timeframe)

    current_price = float(entry_df["close"].iloc[-1])
    as_of = entry_df["time"].iloc[-1]

    macd_result = macd.compute(trend_df)
    adx_result = adx.compute(trend_df)
    ichimoku_result = ichimoku.compute(trend_df)
    vwap_result = vwap.compute(entry_df, volume_avg_bars=config.vwap_volume_avg_bars)
    sr_result = support_resistance.compute(
        levels_df, current_price,
        lookback_bars=config.levels_lookback_bars,
        touch_tolerance_pct=config.levels_touch_tolerance_pct,
        min_touches=config.levels_min_touches,
    )

    indicator_states = {
        "macd": macd_result, "adx": adx_result, "ichimoku": ichimoku_result, "vwap": vwap_result,
    }
    confluence_result = confluence.compute(indicator_states)

    entry_atr = atr.compute(entry_df, period=config.atr_period)
    trade_zone_result = trade_zone.compute(
        confluence_result["direction"], current_price, sr_result, entry_atr,
        stop_atr_mult=config.stop_atr_mult, target_atr_mult=config.target_atr_mult,
        entry_zone_pct=config.entry_zone_pct,
        max_entry_distance_pct=config.entry_max_distance_pct,
    )

    return {
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current_price": round(current_price, 8),
        "timeframes": {
            "trend": config.trend_timeframe,
            "entry": config.entry_timeframe,
            "levels": config.levels_timeframe,
        },
        "data_freshness": {
            "trend": _freshness(trend_df),
            "entry": _freshness(entry_df),
            "levels": _freshness(levels_df),
        },
        "indicators": {
            "macd": {"timeframe": config.trend_timeframe, **macd_result},
            "adx": {"timeframe": config.trend_timeframe, **adx_result},
            "ichimoku": {"timeframe": config.trend_timeframe, **ichimoku_result},
            "vwap": {"timeframe": config.entry_timeframe, **vwap_result},
        },
        "support_resistance": {"timeframe": config.levels_timeframe, **sr_result},
        "atr": {"timeframe": config.entry_timeframe, "period": config.atr_period,
                "value": round(entry_atr, 8) if entry_atr is not None else None},
        "confluence": confluence_result,
        "trade_zone": trade_zone_result,
    }


__all__ = ["compute_symbol"]
