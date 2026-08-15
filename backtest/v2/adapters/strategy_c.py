"""Simple, causal Strategy C0 adapter for Backtest V2."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import fmean
from typing import Sequence

from backtest.indicators import calc_atr, calc_ema, calc_rsi

from .base import AdapterSignal
from ..models import Candle


@dataclass(frozen=True, slots=True)
class StrategyCThresholds:
    """Development-derived thresholds frozen before validation."""

    max_atr_pct: Decimal
    near_ema20_pct: Decimal
    max_body_to_atr: Decimal

    def __post_init__(self) -> None:
        for name in ("max_atr_pct", "near_ema20_pct", "max_body_to_atr"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")


DEVELOPMENT_THRESHOLDS = StrategyCThresholds(
    max_atr_pct=Decimal("0.3489067011519851"),  # Development P90
    near_ema20_pct=Decimal("0.22386983626627532"),  # Development P60
    max_body_to_atr=Decimal("0.8240450115027962"),  # Development P80
)


class StrategyCAdapter:
    strategy_name = "Strategy C"
    strategy_version = "c0"
    max_hold_minutes = 90
    window_size = 203
    requires_btc_context = False

    ema_slope_bars = 3
    recent_low_bars = 3
    volume_window = 20
    atr_buffer = Decimal("0.25")
    reward_risk = Decimal("2")
    max_stop_distance_pct = Decimal("2")

    def __init__(self, thresholds: StrategyCThresholds = DEVELOPMENT_THRESHOLDS) -> None:
        self.thresholds = thresholds

    def evaluate(
        self,
        history: Sequence[Candle],
        *,
        btc_history: Sequence[Candle] | None = None,
    ) -> AdapterSignal | None:
        if len(history) < self.window_size:
            return None

        window = history[-self.window_size:]
        closes = [float(c.close) for c in window]
        highs = [float(c.high) for c in window]
        lows = [float(c.low) for c in window]
        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)
        ema200 = calc_ema(closes, 200)
        ema20_past = calc_ema(closes[:-self.ema_slope_bars], 20)
        atr_raw = calc_atr(highs, lows, closes, 14)
        rsi_raw = calc_rsi(closes, 14)
        if None in (ema20, ema50, ema200, ema20_past, atr_raw, rsi_raw):
            return None

        current = window[-1]
        previous = window[-2]
        close = current.close
        atr = Decimal(str(atr_raw))
        ema20_d = Decimal(str(ema20))
        ema50_d = Decimal(str(ema50))
        ema200_d = Decimal(str(ema200))
        ema20_past_d = Decimal(str(ema20_past))
        atr_pct = atr / close * Decimal("100")
        distance = abs(close - ema20_d) / ema20_d * Decimal("100")
        body = abs(close - current.open)
        body_to_atr = body / atr
        body_pct = body / current.open * Decimal("100")
        candle_return = (close - current.open) / current.open * Decimal("100")
        prior_volumes = [float(c.volume) for c in window[-self.volume_window - 1:-1]]
        average_volume = Decimal(str(fmean(prior_volumes)))
        volume_ratio = current.volume / average_volume if average_volume > 0 else Decimal("0")
        recent_low = min(c.low for c in window[-self.recent_low_bars:])
        stop = recent_low - self.atr_buffer * atr
        stop_distance_pct = (close - stop) / close * Decimal("100")
        risk = close - stop
        target = close + self.reward_risk * risk

        trend = ema20_d > ema50_d > ema200_d and ema20_d > ema20_past_d
        setup = close > ema50_d and distance <= self.thresholds.near_ema20_pct
        confirmation = (
            close > current.open
            and close > previous.close
            and volume_ratio > Decimal("1")
        )
        safe_volatility = atr_pct <= self.thresholds.max_atr_pct
        no_chasing = body_to_atr <= self.thresholds.max_body_to_atr
        valid_stop = (
            stop > 0
            and stop < close
            and stop_distance_pct <= self.max_stop_distance_pct
        )
        if not (trend and setup and confirmation and safe_volatility and no_chasing and valid_stop):
            return None

        btc_aligned = bool(btc_history and btc_history[-1].close_time == current.close_time)
        btc_context = None
        if btc_aligned and btc_history is not None:
            btc_closes = [float(c.close) for c in btc_history[-200:]]
            btc_ema20 = calc_ema(btc_closes, 20)
            btc_ema50 = calc_ema(btc_closes, 50)
            btc_context = {
                "close": str(btc_history[-1].close),
                "ema20_above_ema50": bool(
                    btc_ema20 is not None and btc_ema50 is not None and btc_ema20 > btc_ema50
                ),
            }

        metadata = {
            "ema20": str(ema20_d),
            "ema50": str(ema50_d),
            "ema200": str(ema200_d),
            "ema20_slope": str(ema20_d - ema20_past_d),
            "atr": str(atr),
            "atr_percent": str(atr_pct),
            "distance_ema20_percent": str(distance),
            "body_to_atr": str(body_to_atr),
            "volume_ratio": str(volume_ratio),
            "rsi": str(Decimal(str(rsi_raw))),
            "recent_swing_low": str(recent_low),
            "stop_distance_percent": str(stop_distance_pct),
            "rr": str(self.reward_risk),
            "signal_candle_body_percent": str(body_pct),
            "signal_candle_return_percent": str(candle_return),
            "utc_hour": current.close_time.hour,
            "btc_context": btc_context,
            "thresholds": {
                "max_atr_pct": str(self.thresholds.max_atr_pct),
                "near_ema20_pct": str(self.thresholds.near_ema20_pct),
                "max_body_to_atr": str(self.thresholds.max_body_to_atr),
            },
        }
        return AdapterSignal(
            action="BUY_NOW",
            score=Decimal("0"),
            reference_price=close,
            stop_loss=stop,
            take_profit=target,
            metadata=metadata,
        )


__all__ = ["DEVELOPMENT_THRESHOLDS", "StrategyCAdapter", "StrategyCThresholds"]
