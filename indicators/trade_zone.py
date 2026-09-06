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

Reachability guard: a bullish read whose entry zone sits further than
`max_entry_distance_pct` BELOW the current price returns setup
"unreachable" instead of "long". The zone is anchored to the nearest
support on the levels timeframe, and on a slow combo (levels=1w) that
support can sit far under the market -- a real case had LTCUSDT quoting a
43.70-43.83 zone while price was 48.98, ~10.6% away, which is not an
opportunity anyone can act on, just a price the market is nowhere near.
Such a setup is reported (with the distance, so the dashboard can explain
itself) but never packaged as an actionable entry, and alert_watcher.py
never fires a buy alert on it -- it only ever alerts on setup == "long".

R:R guard: a bullish read whose reward does not justify its risk returns
setup "poor_rr" instead of "long". The target is the nearest resistance
above price, and nothing stops that resistance sitting just above the
support the entry zone is built on, while the stop is a full 1.0xATR
below it -- which quotes a "buy" whose win is smaller than its loss. Real
LTCUSDT alerts went out at R:R 0.2 and 0.6. Replaying 30 days of real
history shows this is overwhelmingly a SLOW-combo problem: 47.7% of its
long readings are under 1.5 (median 1.84, tenth percentile 0.26), against
14.6% on medium and 2.7% on fast, because levels=1w can put a weekly
resistance immediately above a weekly support while ATR(4h) keeps the
stop wide. Like "unreachable" this is reported with its numbers so the
dashboard can explain itself, and like "unreachable" alert_watcher.py
never fires a buy alert on it.

TRAILING NOTE (advisory numbers only, no behaviour attached). A "long"
also carries trail_arm_atr_mult / trail_atr_mult / atr_value. These are
NOT used by anything here: the stop, target and setup are computed
exactly as before and none of them reads these fields. They exist purely
so the dashboard and the Telegram message can print one concrete
suggestion -- "if price advances 0.5xATR, consider moving the stop to
(highest price seen - 1.0xATR)" -- with real numbers rather than leaving
the reader to do ATR arithmetic. Acting on it is manual and optional; no
state is kept anywhere about whether a stop was actually moved.

Why these two multipliers: backtest/analysis/exit_mechanics_study.py
replayed 10,041 signals against eight exit mechanics, and a 1.0xATR trail
armed after a 0.5xATR advance was the only one to clear the pre-committed
bar on all three combos. Honest limits of that result, worth remembering
before anyone builds on it: it is consistent in both development and
out-of-sample on the fast and medium combos but out-of-sample ONLY on the
slow one, and it does not make the system profitable -- it cuts the
average loss from -1.00R to -0.48R while also cutting the average win
from +2.57R to +0.63R. It reduces variance; it does not create an edge.
"""

from __future__ import annotations

UNREACHABLE_MESSAGE_TEMPLATE = (
    "الاتجاه صاعد وفق توافق المؤشرات، لكن أقرب دعم (ومعه نطاق الدخول المحسوب منه) يبعد "
    "{distance:.1f}% تحت السعر الحالي — أبعد من الحد المسموح ({limit:.1f}%). "
    "الفرصة دي غير قابلة للتنفيذ عمليًا عند السعر الحالي، فمفيش اقتراح دخول عليها."
)

POOR_RR_MESSAGE_TEMPLATE = (
    "الاتجاه صاعد وفق توافق المؤشرات، لكن أقرب مقاومة قريبة جدًا من نطاق الدخول: "
    "نسبة العائد للمخاطرة {rr} — أقل من الحد الأدنى ({limit:.1f}). "
    "يعني حتى لو الصفقة نجحت، الربح أصغر من المبلغ المخاطر به، فمفيش اقتراح دخول عليها."
)

BEARISH_NO_SHORT_MESSAGE = (
    "الاتجاه هابط حاليًا وفق توافق المؤشرات، لكن هذا الداشبورد لا يقترح صفقات بيع (short) "
    "لاعتبارات شرعية. لو عندك مركز شراء مفتوح على هذا الزوج، هذا الاتجاه يستحق المراجعة "
    "كإشارة خروج محتملة — وليس كفرصة دخول جديدة."
)


def compute(direction: str, current_price: float, sr: dict, atr_value: float | None,
            stop_atr_mult: float = 1.0, target_atr_mult: float = 2.0, entry_zone_pct: float = 0.3,
            max_entry_distance_pct: float = 3.0, min_rr_ratio: float = 1.5,
            trail_arm_atr_mult: float = 0.5, trail_atr_mult: float = 1.0) -> dict:
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

    # Distance measured to the TOP of the zone -- the first price a resting
    # buy order there would actually meet. Negative when price is already
    # inside or below the zone, which is reachable by definition.
    entry_distance_pct = (current_price - entry_high) / current_price * 100
    if entry_distance_pct > max_entry_distance_pct:
        return {
            "setup": "unreachable",
            "direction": "bullish",
            "reason": "entry zone too far below current price",
            "entry_zone": [round(entry_low, 8), round(entry_high, 8)],
            "entry_distance_pct": round(entry_distance_pct, 4),
            "max_entry_distance_pct": max_entry_distance_pct,
            "message": UNREACHABLE_MESSAGE_TEMPLATE.format(
                distance=entry_distance_pct, limit=max_entry_distance_pct),
        }

    stop = reference - stop_atr_mult * atr_value
    resistance = sr.get("nearest_resistance")
    target = resistance if resistance is not None else reference + target_atr_mult * atr_value
    risk = reference - stop
    reward = target - reference

    if risk <= 0:
        return {"setup": "none", "direction": "bullish", "reason": "invalid stop distance (<=0)"}

    rr_ratio = reward / risk if reward > 0 else None

    # Quality guard, the same shape as the reachability one above: a bullish
    # read whose target sits too close to the entry relative to the stop is
    # reported but never packaged as an actionable entry. rr_ratio is None
    # when the "target" is at or BELOW the entry reference (nearest
    # resistance under the support the zone is built on) -- that is the worst
    # case of all, so it fails the guard rather than slipping through as a
    # long with a blank R:R, which is what used to happen.
    if rr_ratio is None or rr_ratio < min_rr_ratio:
        return {
            "setup": "poor_rr",
            "direction": "bullish",
            "reason": "reward too small relative to risk",
            "entry_zone": [round(entry_low, 8), round(entry_high, 8)],
            "entry_distance_pct": round(entry_distance_pct, 4),
            "stop_loss": round(stop, 8),
            "target": round(target, 8),
            "rr_ratio": round(rr_ratio, 2) if rr_ratio is not None else None,
            "min_rr_ratio": min_rr_ratio,
            "message": POOR_RR_MESSAGE_TEMPLATE.format(
                rr=f"{rr_ratio:.2f}" if rr_ratio is not None else "أقل من صفر",
                limit=min_rr_ratio),
        }

    return {
        "setup": "long",
        "direction": "bullish",
        "reference_level": round(reference, 8),
        "reference_level_source": "support_resistance" if used_level else "current_price_fallback",
        "entry_zone": [round(entry_low, 8), round(entry_high, 8)],
        "entry_distance_pct": round(entry_distance_pct, 4),
        "stop_loss": round(stop, 8),
        "target": round(target, 8),
        "target_source": "support_resistance" if resistance is not None else "atr_fallback",
        "risk_pct_of_price": round(risk / current_price * 100, 4),
        "reward_pct_of_price": round(reward / current_price * 100, 4) if reward > 0 else None,
        "rr_ratio": round(rr_ratio, 2) if rr_ratio is not None else None,
        # Advisory only -- see TRAILING NOTE below. Nothing in this module or
        # in alert_watcher.py acts on these; they exist so the dashboard and
        # the Telegram message can spell out one concrete suggestion instead
        # of the reader having to do ATR arithmetic in their head.
        "trail_arm_atr_mult": trail_arm_atr_mult,
        "trail_atr_mult": trail_atr_mult,
        "atr_value": round(atr_value, 8),
    }


__all__ = ["compute", "BEARISH_NO_SHORT_MESSAGE", "UNREACHABLE_MESSAGE_TEMPLATE",
           "POOR_RR_MESSAGE_TEMPLATE"]
