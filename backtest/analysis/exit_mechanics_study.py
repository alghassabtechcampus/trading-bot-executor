"""Do flexible exits beat the fixed stop/target the alert system quotes?

Reuses the EXACT signal generator from stop_width_study.py (already validated
bar-for-bar against indicators/*.py), so every variant below sees an identical
signal set and the only thing that changes is how the position is managed
AFTER the fill. That is the whole point: any difference in the numbers is
attributable to the exit mechanic and nothing else.

VARIANTS
  fixed          baseline -- the deployed behaviour: stop = ref - 1.0*ATR,
                 target = nearest resistance (or ref + 2*ATR), neither moves.
  be_0.5atr      stop -> breakeven once price has advanced 0.5*ATR
  be_1.0atr      stop -> breakeven once price has advanced 1.0*ATR
  trail_1.0atr   after a 0.5*ATR advance, stop trails 1.0*ATR under the high
  trail_1.5atr   after a 0.5*ATR advance, stop trails 1.5*ATR under the high
  ratchet_50     once 50% of the way to target, lock half the reached move
  ratchet_70     once 70% of the way to target, lock half the reached move
  partial_50     sell half at 50% of the way to target, rest runs to target
                 with the stop at breakeven
  partial_70     same, at 70%

INTRABAR CONVENTION (matters more here than in the stop study, because these
mechanics react to the path, not just its endpoints). OHLC does not say
whether the high or the low came first, so every ambiguity is resolved
AGAINST the trade:
  - the stop is tested before the target and before any profit-taking level,
    so a bar that spans both is a loss;
  - a stop raised as a result of bar k only takes effect from bar k+1, so a
    bar can never be used to protect against its own low.
This under-states the flexible variants slightly. That is deliberate: a
mechanic that only wins under optimistic path assumptions is not a finding.

COSTS
  0.20% round trip. Splitting the exit does NOT add cost -- Bybit spot fees
  are proportional, so 0.10% on each of two halves is the same 0.10% total as
  one full exit.

DECISION BAR (pre-committed, same shape as the stop study, applied PER COMBO
-- the stop study's pooling was wrong and is not repeated):
  a variant replaces `fixed` for a combo only if, FOR THAT COMBO,
    (a) OOS average R per signal is higher than `fixed`, AND
    (b) it is higher in >= 3 of that combo's 4 walk-forward windows.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backtest.analysis.stop_width_study import (  # noqa: E402
    COMBOS, DATA, SYMBOLS, FILL_WINDOW_BARS, MAX_HOLD_BARS,
    ROUND_TRIP_COST_PCT, collect_signals, validate_vectorisation,
)

BASE_STOP_MULT = 1.0        # the deployed stop; baseline for every comparison
PARTIAL_FRACTION = 0.5      # how much of the position a partial exit sells
RATCHET_LOCK_FRACTION = 0.5  # how much of the reached move a ratchet locks in

# name -> dict of knobs read by run_variant()
VARIANTS: dict[str, dict] = {
    "fixed":        {},
    "be_0.5atr":    {"arm_atr": 0.5, "breakeven": True},
    "be_1.0atr":    {"arm_atr": 1.0, "breakeven": True},
    "trail_1.0atr": {"arm_atr": 0.5, "trail_atr": 1.0},
    "trail_1.5atr": {"arm_atr": 0.5, "trail_atr": 1.5},
    "ratchet_50":   {"ratchet_at": 0.50},
    "ratchet_70":   {"ratchet_at": 0.70},
    "partial_50":   {"partial_at": 0.50},
    "partial_70":   {"partial_at": 0.70},
}


def run_variant(sig: dict, high: np.ndarray, low: np.ndarray, close: np.ndarray,
                cfg: dict) -> dict:
    """One signal, one exit mechanic. R is always measured against the
    ORIGINAL risk (mid - initial stop) so every variant is on one scale."""
    mid = (sig["entry_low"] + sig["entry_high"]) / 2
    atr = sig["atr"]
    stop0 = sig["reference"] - BASE_STOP_MULT * atr
    target = sig["target"]
    risk = mid - stop0
    if risk <= 0 or target <= mid:
        return {"outcome": "invalid", "R": None, "net_pct": None}

    start = sig["bar"] + 1
    fill = None
    for k in range(start, min(start + FILL_WINDOW_BARS, len(low))):
        if low[k] <= mid:
            fill = k
            break
    if fill is None:
        return {"outcome": "never_entered", "R": 0.0, "net_pct": 0.0, "exit_bars": 0}

    reward = target - mid
    arm_level = mid + cfg.get("arm_atr", 0) * atr
    partial_level = mid + cfg["partial_at"] * reward if "partial_at" in cfg else None
    ratchet_level = mid + cfg["ratchet_at"] * reward if "ratchet_at" in cfg else None

    stop = stop0
    remaining = 1.0
    realised_R = 0.0
    realised_pct = 0.0
    best_high = mid
    took_partial = False
    outcome = None

    last_bar = min(fill + MAX_HOLD_BARS, len(low)) - 1
    for k in range(fill, last_bar + 1):
        # 1. stop first -- a bar spanning stop and target is scored a loss
        if low[k] <= stop:
            realised_R += remaining * (stop - mid) / risk
            realised_pct += remaining * (stop - mid) / mid * 100
            outcome = "hit_stop" if stop <= stop0 else ("stopped_at_be" if abs(stop - mid) < 1e-12
                                                        else "stopped_in_profit")
            remaining = 0.0
            break

        # 2. scale out at the intermediate level, if this variant has one
        if partial_level is not None and not took_partial and high[k] >= partial_level:
            realised_R += PARTIAL_FRACTION * (partial_level - mid) / risk
            realised_pct += PARTIAL_FRACTION * (partial_level - mid) / mid * 100
            remaining -= PARTIAL_FRACTION
            took_partial = True
            stop = max(stop, mid)          # rest of the position rides at breakeven

        # 3. target
        if high[k] >= target:
            realised_R += remaining * (target - mid) / risk
            realised_pct += remaining * (target - mid) / mid * 100
            outcome = "hit_target" if not took_partial else "partial_then_target"
            remaining = 0.0
            break

        # 4. path-dependent stop updates, effective from the NEXT bar
        best_high = max(best_high, high[k])
        if cfg.get("breakeven") and best_high >= arm_level:
            stop = max(stop, mid)
        if "trail_atr" in cfg and best_high >= arm_level:
            stop = max(stop, best_high - cfg["trail_atr"] * atr)
        if ratchet_level is not None and best_high >= ratchet_level:
            stop = max(stop, mid + RATCHET_LOCK_FRACTION * (best_high - mid))

    if outcome is None:                     # ran out of holding time
        exit_price = float(close[last_bar])
        realised_R += remaining * (exit_price - mid) / risk
        realised_pct += remaining * (exit_price - mid) / mid * 100
        outcome = "time_exit"

    return {"outcome": outcome, "R": realised_R,
            "net_pct": realised_pct - ROUND_TRIP_COST_PCT,
            "took_partial": took_partial, "risk_pct": risk / mid * 100}


def main() -> None:
    rng = np.random.default_rng(20260903)
    print("Validating the vectorised indicators against indicators/*.py ...")
    validate_vectorisation(rng, checks=60)

    rows = []
    for combo in COMBOS:
        for symbol in SYMBOLS:
            entry_df = pd.read_parquet(DATA / f"{symbol}_{COMBOS[combo]['entry']}.parquet")
            high = entry_df["high"].to_numpy()
            low = entry_df["low"].to_numpy()
            close = entry_df["close"].to_numpy()
            sigs = collect_signals(symbol, combo)
            for sig in sigs:
                keep = {k: sig[k] for k in ("symbol", "combo", "bar", "time", "timestamp",
                                            "entry_distance_pct")}
                for name, cfg in VARIANTS.items():
                    rows.append({**keep, "variant": name,
                                 **run_variant(sig, high, low, close, cfg)})
            print(f"  {combo:<7} {symbol:<9} {len(sigs):>5} signals", flush=True)

    df = pd.DataFrame(rows)
    out = Path(__file__).resolve().parent / "exit_mechanics_results.parquet"
    df.to_parquet(out, index=False)
    n_sig = len(df) // len(VARIANTS)
    print(f"\n{len(df)} rows = {n_sig} signals x {len(VARIANTS)} variants")
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
