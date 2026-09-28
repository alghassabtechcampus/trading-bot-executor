"""Append-only log of REAL executed fills from the live Bybit account.

NOT the alert pipeline. indicators/alert_watcher.py records what the strategy
SUGGESTED (alert_log.jsonl); this records what the account actually FILLED, so
the two can be compared instead of assuming every alert became a trade.

Uses GET /v5/execution/list with category="spot", not /v5/position/closed-pnl:
closed-pnl accepts only linear/inverse, and this account is spot-only (every
order app.py places is category="spot"), so closed-pnl would return an empty
list forever and look like "no trades" rather than an error.

An execution is one FILL, not a round trip, and spot fills carry no PnL field.
Pairing buys to sells is deliberately NOT done here -- it belongs in
backtest/analysis/ where it can be re-run and corrected without risking the
archive.

Files under TRADE_LOGGER_DATA_DIR (default /data, same convention as
INDICATORS_DATA_DIR in the indicators service):
  executions_real.jsonl      - append-only, one JSON object per fill
  executions_real_state.json - resume cursor + seen execIds (dedupe)

One-shot by default (the project's pattern: n8n Schedule -> Execute Command,
as with signal_runner.py). Set TRADE_LOGGER_INTERVAL to loop in-process.
"""

import json,os,sys,time,hmac,hashlib,urllib.request,urllib.parse

API_KEY = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
# rstrip("/") to match app.py:31 -- without it a trailing slash in .env yields
# a double-slash path that 404s, and because the signature covers only the
# query string the failure looks like a routing bug, not a config typo.
BASE = os.environ.get("BYBIT_BASE_URL", "https://api.bybit.com").rstrip("/")
DATA_DIR = os.environ.get("TRADE_LOGGER_DATA_DIR", "/data")
LOG_PATH = os.path.join(DATA_DIR, "executions_real.jsonl")
STATE_PATH = os.path.join(DATA_DIR, "executions_real_state.json")
INTERVAL = os.environ.get("TRADE_LOGGER_INTERVAL")
TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))  # same env var app.py reads
CATEGORY = "spot"
RECV_WINDOW = "5000"
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
MAX_PAGES = 50


class FetchError(RuntimeError):
    """A window could not be fully read. Raised so the caller does NOT advance
    last_end_ms past data it never saw -- see run_once()."""


def _sign(query_string, ts):
    payload = ts + API_KEY + RECV_WINDOW + query_string
    return hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

def signed_get(path, params):
    ts = str(int(time.time() * 1000))
    # qs is built once and used for BOTH the signature and the URL. Signing one
    # string while sending another is the usual cause of retCode 10004
    # ("error sign"), so do not re-encode or re-order either side.
    qs = urllib.parse.urlencode(params)
    sig = _sign(qs, ts)
    url = BASE + path + "?" + qs
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-BAPI-API-KEY", API_KEY)
    req.add_header("X-BAPI-TIMESTAMP", ts)
    req.add_header("X-BAPI-RECV-WINDOW", RECV_WINDOW)
    req.add_header("X-BAPI-SIGN", sig)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())

def fetch_executions_window(start_ms, end_ms, cursor=None, limit=100):
    params = {
        "category": CATEGORY,
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": str(limit),
    }
    if cursor:
        params["cursor"] = cursor
    data = signed_get("/v5/execution/list", params)
    if data.get("retCode") != 0:
        # RAISE, do not return empty. Bybit answers HTTP 200 even for auth and
        # signature failures, so a bad key produces retCode 10003/10004 inside
        # a "successful" response. Returning ([], None) here made that
        # indistinguishable from "no fills in this window": the caller then
        # advanced last_end_ms past the window and the fills in it were never
        # requested again. With a wrong key on first deploy that silently
        # consumed the whole 7-day backfill, and no later fix could recover it.
        # retMsg is Bybit's own text and never contains the key or secret.
        raise FetchError(f"execution/list retCode={data.get('retCode')} retMsg={data.get('retMsg')!r}")
    result = data.get("result", {})
    rows = result.get("list", [])
    next_cursor = result.get("nextPageCursor") or None
    return rows, next_cursor

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
        except Exception as exc:
            # Do NOT fall through to a fresh state. seen_exec_ids would be
            # empty while last_end_ms reset to "7 days ago", re-appending a
            # week of fills that are already in the archive. A stale archive is
            # recoverable; a duplicated append-only one is not. Fail loudly.
            raise RuntimeError(
                f"state file unreadable at {STATE_PATH}: {exc}. Refusing to "
                f"re-bootstrap, which would duplicate rows in {LOG_PATH}. "
                f"Inspect or delete the state file deliberately."
            ) from exc
        state.setdefault("seen_exec_ids", [])
        state.setdefault("last_end_ms", 0)
        return state
    return {"seen_exec_ids": [], "last_end_ms": 0}

def save_state(state):
    state["seen_exec_ids"] = state["seen_exec_ids"][-10000:]
    # Atomic: write a temp file, fsync, then rename over the original. The
    # plain open(...,"w") this replaced truncated the real file first, so a
    # container restart mid-write (routine here -- restart: unless-stopped plus
    # redeploys) could leave a half-written state file, which load_state now
    # correctly refuses to read.
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)

def row_to_record(r):
    try:
        price = float(r.get("execPrice") or 0)
        qty = float(r.get("execQty") or 0)
        fee = float(r.get("execFee") or 0)
        exec_time = int(r.get("execTime") or 0)
    except Exception:
        price = qty = fee = 0.0
        exec_time = 0
    return {
        "exec_id": r.get("execId"),
        "order_id": r.get("orderId"),
        # order_link_id is the join key back to the strategy, and it is
        # ASYMMETRIC: on BUY fills app.py passes signal_id straight through as
        # orderLinkId, so an entry joins directly to the signal that caused it;
        # on SELL fills it is "sell-<unix_ts>-<SYMBOL>" with no signal_id, so
        # exits must be paired by symbol + time order instead.
        "order_link_id": r.get("orderLinkId") or None,
        "symbol": r.get("symbol"),
        "side": r.get("side"),
        "exec_type": r.get("execType"),
        "order_type": r.get("orderType"),
        "price": price,
        "qty": qty,
        # qty is in BASE coin; exec_value is the QUOTE (USDT) leg. Both are
        # kept because app.py sizes market buys in quote (marketUnit=quoteCoin)
        # and market sells in base (marketUnit=baseCoin), so reconciling an
        # order against its fills needs whichever unit that order used.
        "exec_value": float(r["execValue"]) if r.get("execValue") else None,
        "fee": fee,
        "fee_currency": r.get("feeCurrency") or r.get("feeTokenId"),
        "fee_rate": float(r["feeRate"]) if r.get("feeRate") else None,
        "is_maker": r.get("isMaker"),
        "exec_time": exec_time,
        "fetched_at": int(time.time() * 1000),
        # Full passthrough. A derived field can be recomputed later from raw;
        # a field never written to an append-only archive is gone for good.
        "raw": r,
    }

def run_once(state):
    now_ms = int(time.time() * 1000)
    start_ms = state.get("last_end_ms") or (now_ms - SEVEN_DAYS_MS)
    new_count = 0
    cur_start = start_ms
    # Set, not list: the dedupe test runs per row against up to 10k ids, which
    # is a linear scan on a list. Serialised back to a list in save_state.
    seen = set(state["seen_exec_ids"])
    while cur_start < now_ms:
        cur_end = min(cur_start + SEVEN_DAYS_MS, now_ms)
        cursor = None
        page_guard = 0
        while True:
            page_guard += 1
            rows, cursor = fetch_executions_window(cur_start, cur_end, cursor=cursor)
            if rows:
                new_rows = []
                for r in rows:
                    eid = r.get("execId")
                    if not eid or eid in seen:
                        continue
                    new_rows.append(r)
                    seen.add(eid)
                if new_rows:
                    # Append and fsync BEFORE last_end_ms advances below. If the
                    # process dies in between, the next run re-reads the window
                    # and dedupes on execId; the reverse order would lose fills.
                    with open(LOG_PATH, "a", encoding="utf-8") as f:
                        for r in new_rows:
                            f.write(json.dumps(row_to_record(r), ensure_ascii=False) + "\n")
                        f.flush()
                        os.fsync(f.fileno())
                    new_count += len(new_rows)
            if not cursor or not rows:
                break
            if page_guard >= MAX_PAGES:
                # Raise instead of breaking. Breaking here fell through to the
                # last_end_ms advance below and silently dropped every fill
                # beyond page 50 of this window -- a truncated archive that
                # reads as complete. Raising keeps the window unconsumed so it
                # is retried, and says so out loud.
                raise FetchError(
                    f"window {cur_start}-{cur_end} exceeded {MAX_PAGES} pages; "
                    f"not advancing cursor. Narrow the window or raise MAX_PAGES."
                )
            time.sleep(0.2)
        state["seen_exec_ids"] = sorted(seen)
        state["last_end_ms"] = cur_end
        cur_start = cur_end
        save_state(state)
    return new_count

def main():
    if not API_KEY or not API_SECRET:
        print("Error: BYBIT_API_KEY and BYBIT_API_SECRET are not set as environment variables.")
        sys.exit(2)
    os.makedirs(DATA_DIR, exist_ok=True)
    masked = ("..." + API_KEY[-4:]) if len(API_KEY) >= 4 else "????"
    print(f"trade_logger starting: category={CATEGORY} base={BASE} data_dir={DATA_DIR} key={masked}")
    state = load_state()

    if INTERVAL:
        interval_s = int(INTERVAL)
        while True:
            try:
                n = run_once(state)
                total = sum(1 for _ in open(LOG_PATH, encoding="utf-8")) if os.path.exists(LOG_PATH) else 0
                if n:
                    print(f"appended {n} new fill(s) to {LOG_PATH} (total lines: {total})")
                else:
                    print("no new fills")
            except Exception as e:
                # State is only advanced on a fully-read window, so whatever
                # was not read stays pending and the next cycle re-covers it.
                print("  [error]", e)
                # Re-read state from disk: an exception mid-run can leave the
                # in-memory dict ahead of what was durably saved.
                try:
                    state = load_state()
                except Exception as reload_exc:
                    print("  [fatal] cannot reload state:", reload_exc)
                    sys.exit(1)
            time.sleep(interval_s)
    else:
        n = run_once(state)
        total = sum(1 for _ in open(LOG_PATH, encoding="utf-8")) if os.path.exists(LOG_PATH) else 0
        print(f"appended {n} new fill(s) to {LOG_PATH} (total lines: {total})")

if __name__ == "__main__":
    main()
