"""Strategy D: simple 1h mean-reversion adapter for Backtest V2.

Rationale (see Phase 0 findings): on 5m the mean-reversion edge (~0.01-0.07%) is
smaller than the Bybit-spot round-trip fee floor (~0.20%), so every fast rule
loses. The same signal on the 1h timeframe captures a move several times larger
than the fixed cost, which is what makes it viable. This adapter therefore acts
only on completed UTC hours, buying deep dislocations below a fast mean and
exiting on mean-touch or a time stop.

Causality: the adapter consumes 5m candles, resamples the trailing window to
complete 1h bars, and emits a signal only when the current 5m bar is the final
(minute==55) bar of its hour -- i.e. the hour has just closed. Entry executes at
the next bar's open (handled by the engine). No future data is read.

Parameters are principled and frozen before validation (no percentile tuning),
to keep the surface small and reduce overfitting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Sequence

from backtest.indicators import calc_atr, calc_ema

from .base import AdapterSignal
from ..models import Candle
from ..resampling import resample_5m_to_1h


FIVE_MINUTES = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class StrategyDParameters:
    """Frozen, principled parameters (not tuned to data percentiles)."""

    stretch_atr: Decimal        # buy when close is this many ATR below the mean
    stop_atr: Decimal           # protective stop this many ATR below entry
    ema_period: int
    atr_period: int
    min_hourly_bars: int        # required complete 1h bars before evaluating

    def __post_init__(self) -> None:
        for name in ("stretch_atr", "stop_atr"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        for name in ("ema_period", "atr_period", "min_hourly_bars"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.min_hourly_bars <= self.ema_period:
            raise ValueError("min_hourly_bars must exceed ema_period")


# Validated defaults. A 3.0-ATR dislocation with a wide (4-ATR) protective stop.
# A pre-committed 4-window walk-forward (real engine, Bybit-spot maker costs) was
# positive in 3 of 4 windows at 3.0-ATR + 8h hold (+0.148%/trade), versus only
# 1 of 4 at the shallower 2.5-ATR + 4h. The wide stop lets the position ride to
# the mean-touch or the 8h time stop; tight stops truncated the reversion drift.
# CAVEAT: edge is thin and hinges on maker fills (taker was 2/4); one window (W2)
# showed fat-tail losses from the wide stop. Forward paper-trading required before
# any real capital -- do not treat this as a finished, robust edge.
DEVELOPMENT_PARAMETERS = StrategyDParameters(
    stretch_atr=Decimal("3.0"),
    stop_atr=Decimal("4"),
    ema_period=20,
    atr_period=14,
    min_hourly_bars=80,
)


class StrategyDAdapter:
    strategy_name = "Strategy D"
    strategy_version = "d1-3atr-8h"
    max_hold_minutes = 480        # 8h time stop (walk-forward: 8h beat 4h)
    window_size = 1500            # trailing 5m bars kept (~125h) for stable 1h EMA
    requires_btc_context = False

    def __init__(self, parameters: StrategyDParameters = DEVELOPMENT_PARAMETERS) -> None:
        self.parameters = parameters

    def evaluate(
        self,
        history: Sequence[Candle],
        *,
        btc_history: Sequence[Candle] | None = None,
    ) -> AdapterSignal | None:
        del btc_history
        if len(history) < self.window_size:
            return None

        current = history[-1]
        # Act only once per hour, on the bar that completes the hour bucket.
        if current.timeframe != FIVE_MINUTES or current.timestamp.minute != 55:
            return None

        window = history[-self.window_size:]
        hourly = resample_5m_to_1h(window)
        if len(hourly) < self.parameters.min_hourly_bars:
            return None

        last_hour = hourly[-1]
        # The just-closed hour must be the one the current 5m bar belongs to.
        if last_hour.timestamp != current.timestamp.replace(minute=0):
            return None
        if last_hour.close != current.close:
            return None

        closes = [float(c.close) for c in hourly]
        highs = [float(c.high) for c in hourly]
        lows = [float(c.low) for c in hourly]
        ema_raw = calc_ema(closes, self.parameters.ema_period)
        atr_raw = calc_atr(highs, lows, closes, self.parameters.atr_period)
        if ema_raw is None or atr_raw is None:
            return None

        close = last_hour.close
        ema = Decimal(str(ema_raw))
        atr = Decimal(str(atr_raw))
        if atr <= 0 or ema <= close:
            return None  # not below the mean -> no reversion setup

        stretch = (ema - close) / atr
        if stretch < self.parameters.stretch_atr:
            return None

        stop = close - self.parameters.stop_atr * atr
        if stop <= 0 or stop >= close:
            return None
        target = ema  # mean-reversion target is the mean itself
        if target <= close:
            return None

        metadata = {
            "timeframe": "1h",
            "hour_close_time": last_hour.close_time.isoformat(),
            "ema": str(ema),
            "atr": str(atr),
            "stretch_atr": str(stretch),
            "stop_atr": str(self.parameters.stop_atr),
            "target_is_mean": True,
            "reference_close": str(close),
        }
        return AdapterSignal(
            action="BUY_NOW",
            score=stretch,  # deeper dislocation ranks first when several fire
            reference_price=close,
            stop_loss=stop,
            take_profit=target,
            metadata=metadata,
        )


__all__ = ["DEVELOPMENT_PARAMETERS", "StrategyDAdapter", "StrategyDParameters"]
