"""Confluence: how many of the directional indicators agree.

Takes the {"state": "bullish"|"bearish"|"neutral", ...} dict each indicator
module returns and counts agreement. "neutral" readings count toward the
total but not toward either side (an indicator that isn't taking a side
shouldn't inflate whichever direction happens to have more votes).
`direction` is whichever side has strictly more votes; a tie (including
0-0) is reported as "neutral" -- confluence should never manufacture a
direction the indicators don't actually support.
"""

from __future__ import annotations


def compute(indicator_states: dict[str, dict]) -> dict:
    total = len(indicator_states)
    bullish = [name for name, r in indicator_states.items() if r.get("state") == "bullish"]
    bearish = [name for name, r in indicator_states.items() if r.get("state") == "bearish"]

    if len(bullish) > len(bearish):
        direction = "bullish"
    elif len(bearish) > len(bullish):
        direction = "bearish"
    else:
        direction = "neutral"

    return {
        "direction": direction,
        "bullish_count": len(bullish),
        "bearish_count": len(bearish),
        "total_indicators": total,
        "bullish_indicators": bullish,
        "bearish_indicators": bearish,
        "summary": f"{max(len(bullish), len(bearish))}/{total} {direction}" if direction != "neutral"
                   else f"{len(bullish)}-{len(bearish)} split of {total} (neutral)",
    }


__all__ = ["compute"]
