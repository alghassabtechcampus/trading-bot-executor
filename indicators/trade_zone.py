"""Suggested entry zone / stop / target, combining confluence direction with
support/resistance levels and ATR. Display-only: this module NEVER places
an order or sizes a position, it only computes numbers for a human to look
at before deciding anything themselves.

Long-only, by design: this dashboard never suggests a short (sell) setup,
the same principle already applied to the TASI project (buy-only, no
shorting, for Shariah-compliance reasons). The three possible outcomes:

  direction == "bullish": a long setup, computed as before --
    reference level = nearest support below price (falls back to the
      current price itself if no qualifying support level exists)
    entry zone       = [reference, reference * (1 + entry_zone_pct/100)]
      (a pullback-to-support buy zone, not a single price)
    stop             = reference - stop_atr_mult  * ATR(entry_timeframe)
    target           = nearest resistance above price, else
                        reference + target_atr_mult * ATR (ATR fallback)
    R:R              = (target - reference) / (reference - stop)

  direction == "bearish": setup "bearish_no_short". NO entry/stop/target
    numbers are computed or returned -- there is nothing here for a user
    to act on as a new position. The direction information itself is
    still surfaced (a bearish reading is still useful to someone holding
    an existing long on this symbol, as a review/exit prompt), just never
    packaged as a tradeable short setup.

  direction == "neutral" (or ATR unavailable for a bullish read): "none".
    This module never invents a trade where the indicators disagree.
"""

from __future__ import annotations

BEARISH_NO_SHORT_MESSAGE = (
    "الاتجاه هابط حاليًا وفق توافق المؤشرات، لكن هذا الداشبورد لا يقترح صفقات بيع (short) "
    "لاعتبارات شرعية. لو عندك مركز شراء مفتوح على هذا الزوج، هذا الاتجاه يستحق المراجعة "
    "كإشارة خروج محتملة — وليس كفرصة دخول جديدة."
)


def compute(direction: str, current_price: float, sr: dict, atr_value: float | None,
            stop_atr_mult: float = 1.0, target_atr_mult: float = 2.0, entry_zone_pct: float = 0.3) -> dict:
    if direction == "neutral":
        return {"setup": "none", "direction": "neutral", "reason": "no clear confluence direction"}

    if direction == "bearish":
        return {"setup": "bearish_no_short", "direction": "bearish", "message": BEARISH_NO_SHORT_MESSAGE}

    # direction == "bullish" from here -- the only setup this module ever proposes as a new entry.
    if atr_value is None:
        return {"setup": "none", "direction": "bullish", "reason": "ATR unavailable"}

    reference = sr.get("nearest_support") or current_price
    used_level = sr.get("nearest_support") is not None
    entry_low, entry_high = reference, reference * (1 + entry_zone_pct / 100)
    stop = reference - stop_atr_mult * atr_value
    resistance = sr.get("nearest_resistance")
    target = resistance if resistance is not None else reference + target_atr_mult * atr_value
    risk = reference - stop
    reward = target - reference

    if risk <= 0:
        return {"setup": "none", "direction": "bullish", "reason": "invalid stop distance (<=0)"}

    rr_ratio = reward / risk if reward > 0 else None

    return {
        "setup": "long",
        "direction": "bullish",
        "reference_level": round(reference, 8),
        "reference_level_source": "support_resistance" if used_level else "current_price_fallback",
        "entry_zone": [round(entry_low, 8), round(entry_high, 8)],
        "stop_loss": round(stop, 8),
        "target": round(target, 8),
        "target_source": "support_resistance" if resistance is not None else "atr_fallback",
        "risk_pct_of_price": round(risk / current_price * 100, 4),
        "reward_pct_of_price": round(reward / current_price * 100, 4) if reward > 0 else None,
        "rr_ratio": round(rr_ratio, 2) if rr_ratio is not None else None,
    }


__all__ = ["compute", "BEARISH_NO_SHORT_MESSAGE"]
