"""Walk-forward study: does widening the trade_zone stop from 1.0xATR to
1.5x or 2.0x actually improve outcomes for the indicators/ alert system?

METHOD
  Replays the REAL alert pipeline bar by bar on Bybit history for all 9
  symbols x all 3 combos. At each entry-timeframe bar it rebuilds exactly
  what the live system would have computed at that moment -- macd/adx/
  ichimoku on the trend timeframe, vwap + ATR on the entry timeframe,
  support/resistance on the levels timeframe -- feeds them through the real
  confluence.compute() and trade_zone.compute(), and applies the same
  2-consecutive-checks debounce alert_watcher.py uses. Only closed bars are
  ever visible: no value at bar t depends on anything after t.

  The stop multiplier changes ONLY the stop price. Entry zone and target are
  untouched by it, so all three variants see an IDENTICAL signal set and the
  comparison is like-for-like.

COSTS
  0.20% round trip (0.10% maker in + 0.10% maker out) -- the structural Bybit
  spot floor established in Phase 0 of this project; fees are the same for
  maker and taker there, so no order type escapes it.

PRE-COMMITTED DECISION BAR (written before the study was run, per the
walk-forward lesson recorded earlier in this project: pooled numbers hid
regime dependence once already, so a single aggregate is not allowed to
decide anything). A wider stop replaces 1.0xATR only if BOTH hold on the
OUT-OF-SAMPLE segment:
    (a) higher average R per signal (unfilled signals counted as 0R), AND
    (b) higher average R in at least 3 of the 4 walk-forward windows.
Anything less -> keep 1.0xATR. A better pooled number alone is NOT enough.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from indicators import confluence, trade_zone  # noqa: E402
from indicators.support_resistance import _cluster  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data_stopstudy"
SYMBOLS = ["ADAUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT",
           "LINKUSDT", "LTCUSDT", "SOLUSDT", "XRPUSDT"]
COMBOS = {
    "fast":   {"trend": "1h", "entry": "15m", "levels": "1d"},
    "medium": {"trend": "4h", "entry": "1h",  "levels": "1d"},
    "slow":   {"trend": "1d", "entry": "4h",  "levels": "1w"},
}
TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}
STOP_MULTS = [1.0, 1.5, 2.0]
CONFIRMATIONS_REQUIRED = 2
ROUND_TRIP_COST_PCT = 0.20
FILL_WINDOW_BARS = 48      # a resting limit that never fills within this many bars is abandoned
MAX_HOLD_BARS = 240        # time-exit at market if neither stop nor target is reached
LEVELS_LOOKBACK = 20
LEVELS_TOL_PCT = 0.5
LEVELS_MIN_TOUCHES = 2
ATR_PERIOD = 14
ENTRY_ZONE_PCT = 0.3
TARGET_ATR_MULT = 2.0


# ---------------------------------------------------------------- indicators
# Each of these reproduces the corresponding indicators/*.py module as a
# SERIES. Every operation below is prefix-dependent (ewm(adjust=False),
# rolling, groupby-cumsum), so the value at index t equals what the module
# would return given df.iloc[:t+1]. validate_vectorisation() proves it.

def macd_states(df: pd.DataFrame) -> np.ndarray:
    close = df["close"]
    line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = line.ewm(span=9, adjust=False).mean()
    out = np.where(line > signal, "bullish", np.where(line < signal, "bearish", "neutral"))
    out[: 26 + 9 - 1] = "neutral"          # module's "insufficient history" guard
    return out


def _wilder(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(alpha=1 / period, adjust=False).mean()


def adx_states(df: pd.DataFrame, period: int = 14, threshold: float = 25.0) -> np.ndarray:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    up, down = high.diff(), -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr = _wilder(tr, period).replace(0, np.nan)
    plus_di = 100 * _wilder(pd.Series(plus_dm, index=df.index), period) / atr
    minus_di = 100 * _wilder(pd.Series(minus_dm, index=df.index), period) / atr
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx = _wilder(dx.fillna(0), period)
    p = plus_di.fillna(0.0).to_numpy()
    m = minus_di.fillna(0.0).to_numpy()
    a = adx.to_numpy()
    out = np.where((a > threshold) & (p > m), "bullish",
                   np.where((a > threshold) & (m > p), "bearish", "neutral"))
    out[: period * 2 - 1] = "neutral"
    return out


def ichimoku_states(df: pd.DataFrame) -> np.ndarray:
    high, low, close = df["high"], df["low"], df["close"]
    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    senkou_a = (tenkan + kijun) / 2
    senkou_b = (high.rolling(52).max() + low.rolling(52).min()) / 2
    top = pd.concat([senkou_a, senkou_b], axis=1).max(axis=1).shift(26)
    bottom = pd.concat([senkou_a, senkou_b], axis=1).min(axis=1).shift(26)
    c = close.to_numpy(); t = top.to_numpy(); b = bottom.to_numpy()
    out = np.where(np.isnan(t) | np.isnan(b), "neutral",
                   np.where(c > t, "bullish", np.where(c < b, "bearish", "neutral")))
    out[: 52 + 26 - 1] = "neutral"
    return out


def vwap_states(df: pd.DataFrame) -> np.ndarray:
    day = df["time"].dt.floor("D")
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (typical * df["volume"]).groupby(day).cumsum() / \
           df["volume"].groupby(day).cumsum().replace(0, np.nan)
    c = df["close"].to_numpy(); v = vwap.to_numpy()
    return np.where(np.isnan(v), "neutral", np.where(c > v, "bullish", np.where(c < v, "bearish", "neutral")))


def atr_series(df: pd.DataFrame, period: int = ATR_PERIOD) -> np.ndarray:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    out = tr.ewm(alpha=1 / period, adjust=False).mean().to_numpy().copy()
    out[:period] = np.nan
    return out


def validate_vectorisation(rng: np.random.Generator, checks: int = 120) -> None:
    """Proves the series above equal the real modules on truncated history."""
    from indicators import adx as m_adx, atr as m_atr, ichimoku as m_ich, macd as m_macd, vwap as m_vwap
    pairs = [(m_macd, macd_states), (m_adx, adx_states), (m_ich, ichimoku_states), (m_vwap, vwap_states)]
    tested = 0
    for _ in range(checks):
        sym = SYMBOLS[rng.integers(len(SYMBOLS))]
        tf = ["1h", "4h", "1d"][rng.integers(3)]
        df = pd.read_parquet(DATA / f"{sym}_{tf}.parquet")
        t = int(rng.integers(120, len(df)))
        for module, vec in pairs:
            expected = module.compute(df.iloc[:t + 1])["state"]
            got = vec(df)[t]
            assert expected == got, f"{sym} {tf} @{t} {module.__name__}: {expected!r} != {got!r}"
        exp_atr = m_atr.compute(df.iloc[:t + 1], period=ATR_PERIOD)
        got_atr = atr_series(df)[t]
        assert abs(exp_atr - got_atr) < 1e-9, f"{sym} {tf} @{t} atr: {exp_atr} != {got_atr}"
        tested += 1
    print(f"  vectorisation validated against the real modules on {tested} random cut points "
          f"({tested * 5} comparisons), all exact.")


# ------------------------------------------------------------------- levels
def levels_clusters(levels_df: pd.DataFrame) -> list:
    """Per levels-bar, the support/resistance clusters of the trailing
    LEVELS_LOOKBACK window -- exactly support_resistance.compute()'s window,
    before the current-price filter (which depends on the entry bar, not this
    one, and is applied later)."""
    highs = levels_df["high"].tolist()
    lows = levels_df["low"].tolist()
    times = levels_df["time"].tolist()
    out: list = [None] * len(levels_df)
    for j in range(LEVELS_LOOKBACK - 1, len(levels_df)):
        lo = j - LEVELS_LOOKBACK + 1
        res = [c for c in _cluster(list(zip(highs[lo:j + 1], times[lo:j + 1])), LEVELS_TOL_PCT)
               if c["touches"] >= LEVELS_MIN_TOUCHES]
        sup = [c for c in _cluster(list(zip(lows[lo:j + 1], times[lo:j + 1])), LEVELS_TOL_PCT)
               if c["touches"] >= LEVELS_MIN_TOUCHES]
        out[j] = ([c["level"] for c in res], [c["level"] for c in sup])
    return out


def sr_at(clusters, price: float) -> dict:
    if clusters is None:
        return {"nearest_resistance": None, "nearest_support": None}
    res, sup = clusters
    above = [x for x in res if x > price]
    below = [x for x in sup if x < price]
    return {"nearest_resistance": min(above) if above else None,
            "nearest_support": max(below) if below else None}


# ------------------------------------------------------------------- replay
def collect_signals(symbol: str, combo: str) -> list:
    tf = COMBOS[combo]
    entry = pd.read_parquet(DATA / f"{symbol}_{tf['entry']}.parquet")
    trend = pd.read_parquet(DATA / f"{symbol}_{tf['trend']}.parquet")
    levels = pd.read_parquet(DATA / f"{symbol}_{tf['levels']}.parquet")

    t_macd, t_adx, t_ich = macd_states(trend), adx_states(trend), ichimoku_states(trend)
    e_vwap, e_atr = vwap_states(entry), atr_series(entry)
    lv_clusters = levels_clusters(levels)

    # A bar with open time T on timeframe X is only CLOSED at T + X. Map each
    # entry bar to the newest higher-timeframe bar that had already closed.
    e_close = entry["timestamp"].to_numpy() + TF_MINUTES[tf["entry"]] * 60_000
    t_close = trend["timestamp"].to_numpy() + TF_MINUTES[tf["trend"]] * 60_000
    l_close = levels["timestamp"].to_numpy() + TF_MINUTES[tf["levels"]] * 60_000
    t_idx = np.searchsorted(t_close, e_close, side="right") - 1
    l_idx = np.searchsorted(l_close, e_close, side="right") - 1

    close = entry["close"].to_numpy()
    signals: list = []
    confirmed, pending, pending_count = None, None, 0

    for i in range(len(entry)):
        ti, li = t_idx[i], l_idx[i]
        if ti < 0 or li < 0 or np.isnan(e_atr[i]):
            continue
        states = {"macd": {"state": t_macd[ti]}, "adx": {"state": t_adx[ti]},
                  "ichimoku": {"state": t_ich[ti]}, "vwap": {"state": e_vwap[i]}}
        conf = confluence.compute(states)
        price = float(close[i])
        sr = sr_at(lv_clusters[li], price)
        # max_entry_distance_pct is deliberately disabled here: the study
        # records the distance instead, so the reachability filter can be
        # applied (or not) afterwards without re-running the replay.
        tz = trade_zone.compute(conf["direction"], price, sr, float(e_atr[i]),
                                stop_atr_mult=1.0, target_atr_mult=TARGET_ATR_MULT,
                                entry_zone_pct=ENTRY_ZONE_PCT, max_entry_distance_pct=float("inf"))
        setup = tz["setup"]

        fired = False
        if confirmed is None:
            confirmed = setup                      # baseline, exactly like a first run
        elif setup == confirmed:
            pending, pending_count = None, 0
        elif setup == pending:
            pending_count += 1
            if pending_count >= CONFIRMATIONS_REQUIRED:
                fired = setup == "long"
                confirmed, pending, pending_count = setup, None, 0
        else:
            pending, pending_count = setup, 1

        if fired:
            signals.append({
                "symbol": symbol, "combo": combo, "bar": i,
                "time": entry["time"].iloc[i].isoformat(),
                "timestamp": int(entry["timestamp"].iloc[i]),
                "price": price, "atr": float(e_atr[i]),
                "reference": tz["reference_level"],
                "entry_low": tz["entry_zone"][0], "entry_high": tz["entry_zone"][1],
                "target": tz["target"],
                "entry_distance_pct": tz["entry_distance_pct"],
            })
    return signals


def simulate(signal: dict, entry_df: pd.DataFrame, stop_mult: float) -> dict:
    """Resting limit at the midpoint of the quoted zone, from the bar AFTER
    the signal bar (the signal is only known at that bar's close)."""
    high = entry_df["high"].to_numpy()
    low = entry_df["low"].to_numpy()
    close = entry_df["close"].to_numpy()
    mid = (signal["entry_low"] + signal["entry_high"]) / 2
    stop = signal["reference"] - stop_mult * signal["atr"]
    target = signal["target"]
    risk = mid - stop
    if risk <= 0 or target <= mid:
        return {"outcome": "invalid", "R": None, "net_pct": None}

    start = signal["bar"] + 1
    fill = None
    for k in range(start, min(start + FILL_WINDOW_BARS, len(entry_df))):
        if low[k] <= mid:
            fill = k
            break
    if fill is None:
        return {"outcome": "never_entered", "R": 0.0, "net_pct": 0.0,
                "stop": stop, "mid": mid, "risk_pct": risk / mid * 100}

    exit_price, outcome = None, None
    for k in range(fill, min(fill + MAX_HOLD_BARS, len(entry_df))):
        if low[k] <= stop:            # conservative: a bar spanning both is a loss
            exit_price, outcome = stop, "hit_stop"
            break
        if high[k] >= target:
            exit_price, outcome = target, "hit_target"
            break
    if outcome is None:
        last = min(fill + MAX_HOLD_BARS, len(entry_df)) - 1
        if last <= fill:
            return {"outcome": "open_at_end", "R": None, "net_pct": None,
                    "stop": stop, "mid": mid, "risk_pct": risk / mid * 100}
        exit_price, outcome = float(close[last]), "time_exit"

    return {"outcome": outcome, "R": (exit_price - mid) / risk, "stop": stop, "mid": mid,
            "net_pct": (exit_price - mid) / mid * 100 - ROUND_TRIP_COST_PCT,
            "risk_pct": risk / mid * 100}


def main() -> None:
    rng = np.random.default_rng(20260903)
    print("Validating the vectorised indicators against indicators/*.py ...")
    validate_vectorisation(rng)

    rows = []
    for combo in COMBOS:
        for symbol in SYMBOLS:
            entry_df = pd.read_parquet(DATA / f"{symbol}_{COMBOS[combo]['entry']}.parquet")
            sigs = collect_signals(symbol, combo)
            for sig in sigs:
                for mult in STOP_MULTS:
                    rows.append({**sig, "stop_mult": mult, **simulate(sig, entry_df, mult)})
            print(f"  {combo:<7} {symbol:<9} {len(sigs):>5} signals", flush=True)

    df = pd.DataFrame(rows)
    out = Path(__file__).resolve().parent / "stop_width_results.parquet"
    df.to_parquet(out, index=False)
    print(f"\n{len(df)} simulated rows "
          f"({len(df) // len(STOP_MULTS)} signals x {len(STOP_MULTS)} stop widths)")
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
