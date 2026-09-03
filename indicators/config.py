"""Configuration loading for the indicators dashboard engine.

Defaults live in config.json next to this file. Every key can be
overridden at runtime with an environment variable named
DASHBOARD_<KEY_UPPERCASE> (e.g. DASHBOARD_TREND_TIMEFRAME=1d), so an
operator can change timeframes or thresholds without touching code.
This is the ONLY supported way to change behavior -- nothing in this
module or its callers should hardcode a timeframe or threshold.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

_INT_KEYS = {"levels_lookback_bars", "levels_min_touches", "atr_period", "vwap_volume_avg_bars"}
_FLOAT_KEYS = {"levels_touch_tolerance_pct", "stop_atr_mult", "target_atr_mult", "entry_zone_pct",
               "entry_max_distance_pct"}


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    market: str
    trend_timeframe: str
    entry_timeframe: str
    levels_timeframe: str
    scheduler_interval: str
    levels_lookback_bars: int
    levels_touch_tolerance_pct: float
    levels_min_touches: int
    atr_period: int
    stop_atr_mult: float
    target_atr_mult: float
    entry_zone_pct: float
    entry_max_distance_pct: float
    vwap_volume_avg_bars: int


def load_config(path: Path | None = None) -> DashboardConfig:
    with (path or CONFIG_PATH).open("r", encoding="utf-8") as handle:
        raw: dict = json.load(handle)

    for key in list(raw.keys()):
        env_name = f"DASHBOARD_{key.upper()}"
        env_val = os.getenv(env_name)
        if env_val is None:
            continue
        if key in _INT_KEYS:
            raw[key] = int(env_val)
        elif key in _FLOAT_KEYS:
            raw[key] = float(env_val)
        else:
            raw[key] = env_val

    return DashboardConfig(**raw)


__all__ = ["DashboardConfig", "load_config", "CONFIG_PATH"]
