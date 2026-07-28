from flask import Flask, request, jsonify
import hashlib
import hmac
import time
import requests
import os
import json

app = Flask(__name__)

BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY", "YOUR_API_KEY")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "YOUR_API_SECRET")
BYBIT_BASE_URL   = "https://api.bybit.com"
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "my_super_secret_123")
REDIS_URL        = os.environ.get("REDIS_URL", "")
REDIS_TOKEN      = os.environ.get("REDIS_TOKEN", "")

ORIGINAL_COINS = ["BTC", "ETH", "USDC"]

# طريقة البيع لكل عملة - مختبرة ومؤكدة
SELL_BY_QTY = {
    "BTC" : "{:.6f}",
    "ETH" : "{:.6f}",
    "BNB" : "{:.3f}",
    "XRP" : "{:.2f}",
    "ADA" : "{:.2f}",
    "LTC" : "{:.4f}",
}
# باقي العملات تُباع بـ quoteCoin

def redis_req(method, path, **kwargs):
    try:
        url = f"{REDIS_URL}/{path}"
        headers = {"Authorization": f"Bearer {REDIS_TOKEN}"}
        resp = requests.request(method, url, headers=headers, timeout=5, **kwargs)
        return resp.json()
    except:
        return {}

def redis_set(key, value):
    encoded = requests.utils.quote(json.dumps(value), safe='')
    return redis_req("POST", f"set/{key}/{encoded}")

def redis_get(key):
    result = redis_req("GET", f"get/{key}").get("result")
    if result:
        try:
            return json.loads(result)
        except:
            return {}
    return {}

def redis_del(key):
    redis_req("GET", f"del/{key}")

def redis_keys(pattern):
    return redis_req("GET", f"keys/{pattern}").get("result", [])

def sign(pre_sign):
    return hmac.new(BYBIT_API_SECRET.encode(), pre_sign.encode(), hashlib.sha256).hexdigest()

def base_headers(ts, sig):
    return {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": sig,
        "Content-Type": "application/json",
    }

def get_headers(params):
    ts = str(int(time.time() * 1000))
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return base_headers(ts, sign(f"{ts}{BYBIT_API_KEY}5000{query}"))

def post_headers(body):
    ts = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(',', ':'))
    return base_headers(ts, sign(f"{ts}{BYBIT_API_KEY}5000{body_str}"))

def verify_webhook(req):
    return req.headers.get("X-Webhook-Signature", "") == WEBHOOK_SECRET

def get_balance():
    params = {"accountType": "UNIFIED"}
    resp = requests.get(BYBIT_BASE_URL + "/v5/account/wallet-balance",
                       headers=get_headers(params), params=params, timeout=10)
    return resp.json()

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": int(time.time()), "redis": bool(REDIS_URL)}), 200

@app.route("/balance", methods=["GET"])
def balance():
    return jsonify(get_balance()), 200

@app.route("/usdt_balance", methods=["GET"])
def usdt_balance():
    data = get_balance()
    usdt = 0
    for c in data["result"]["list"][0]["coin"]:
        if c["coin"] == "USDT":
            usdt = float(c["usdValue"])
            break
    return jsonify({"usdt": usdt, "has_balance": usdt >= 100}), 200

@app.route("/check_position", methods=["GET"])
def check_position():
    symbol = request.args.get("symbol", "")
    coin = symbol.replace("USDT", "")
    if coin in ORIGINAL_COINS:
        return jsonify({"has_position": False}), 200
    trade = redis_get(f"trade:{symbol}")
    if not trade:
        return jsonify({"has_position": False}), 200
    data = get_balance()
    for c in data["result"]["list"][0]["coin"]:
        if c["coin"] == coin and float(c["usdValue"]) > 5:
            return jsonify({"has_position": True}), 200
    redis_del(f"trade:{symbol}")
    return jsonify({"has_position": False}), 200

@app.route("/positions", methods=["GET"])
def positions():
    keys = redis_keys("trade:*")
    if not keys:
        return jsonify({"result": []}), 200
    data = get_balance()
    coin_values = {c["coin"]: float(c["usdValue"]) 
                   for c in data["result"]["list"][0]["coin"]}
    result = []
    for key in keys:
        symbol = key.replace("trade:", "")
        coin = symbol.replace("USDT", "")
        trade = redis_get(key)
        if not trade:
            continue
        usd_value = coin_values.get(coin, 0)
        if usd_value < 5:
            redis_del(key)
            continue
        buy_value = trade.get("buy_value", 100)
        pnl_pct = ((usd_value - buy_value) / buy_value) * 100
        result.append({
            "symbol": symbol,
            "buy_price": trade.get("buy_price", 0),
            "buy_value": buy_value,
            "buy_time": trade.get("time", 0),
            "usd_value": usd_value,
            "pnl_pct": round(pnl_pct, 2)
        })
    return jsonify({"result": result}), 200

@app.route("/execute", methods=["POST"])
def execute():
    if not verify_webhook(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(force=True)
    required = ["symbol", "action", "price", "stopLoss", "takeProfit", "qty"]
    if not all(k in data for k in required):
        return jsonify({"error": "Missing fields"}), 400
    symbol = data["symbol"].upper()
    action = data["action"].upper()
    price  = float(data["price"])
    if action not in ("BUY", "SELL"):
        return jsonify({"error": "action must be BUY or SELL"}), 400
    body = {
        "category": "spot", "symbol": symbol,
        "side": "Buy" if action == "BUY" else "Sell",
        "orderType": "Market", "qty": str(data["qty"]), "timeInForce": "IOC",
    }
    body_str = json.dumps(body, separators=(',', ':'))
    resp = requests.post(BYBIT_BASE_URL + "/v5/order/create",
                        headers=post_headers(body), data=body_str, timeout=10)
    result = resp.json()
    if result.get("retCode") == 0 and action == "BUY":
        redis_set(f"trade:{symbol}", {
            "buy_price": price, "buy_value": 100, "time": int(time.time())
        })
    print(json.dumps({"time": int(time.time()), "symbol": symbol, 
                      "action": action, "result": result}, ensure_ascii=False))
    if result.get("retCode") == 0:
        return jsonify({"status": "executed", "order": result}), 200
    return jsonify({"status": "bybit_error", "detail": result}), 502

@app.route("/sell", methods=["POST"])
def sell():
    if not verify_webhook(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(force=True)
    symbol = data.get("symbol", "").upper()
    coin = symbol.replace("USDT", "")
    if coin in ORIGINAL_COINS:
        return jsonify({"error": "Cannot sell original coin"}), 400
    balance_data = get_balance()
    usd_value = qty_balance = 0
    for c in balance_data["result"]["list"][0]["coin"]:
        if c["coin"] == coin:
            usd_value = float(c["usdValue"])
            qty_balance = float(c["walletBalance"])
            break
    if usd_value < 10:
        return jsonify({"error": "Balance too small"}), 400
    fmt = SELL_BY_QTY.get(coin)
    if fmt:
        qty_str = fmt.format(qty_balance * 0.999)
        body = {
            "category": "spot", "symbol": symbol, "side": "Sell",
            "orderType": "Market", "qty": qty_str, "timeInForce": "IOC",
        }
    else:
        body = {
            "category": "spot", "symbol": symbol, "side": "Sell",
            "orderType": "Market", "marketUnit": "quoteCoin",
            "qty": str(round(usd_value * 0.999, 2)), "timeInForce": "IOC",
        }
    body_str = json.dumps(body, separators=(',', ':'))
    resp = requests.post(BYBIT_BASE_URL + "/v5/order/create",
                        headers=post_headers(body), data=body_str, timeout=10)
    result = resp.json()
    if result.get("retCode") == 0:
        redis_del(f"trade:{symbol}")
    print(json.dumps({"time": int(time.time()), "symbol": symbol,
                      "action": "SELL", "result": result}, ensure_ascii=False))
    if result.get("retCode") == 0:
        return jsonify({"status": "sold", "order": result}), 200
    return jsonify({"status": "bybit_error", "detail": result}), 502

@app.route("/pnl", methods=["GET"])
def pnl():
    hours = request.args.get("hours", "24")
    start_time = str(int((time.time() - int(hours) * 3600) * 1000))
    params = {"category": "spot", "limit": "50", "startTime": start_time}
    resp = requests.get(BYBIT_BASE_URL + "/v5/order/history",
                       headers=get_headers(params), params=params, timeout=10)
    data = resp.json()
    if data.get("retCode") != 0:
        return jsonify(data), 200
    summary = {}
    for order in data.get("result", {}).get("list", []):
        if order.get("orderStatus") != "Filled":
            continue
        s = order["symbol"]
        side = order["side"]
        val = float(order.get("cumExecValue", 0))
        fee = float(order.get("cumExecFee", 0))
        if s not in summary:
            summary[s] = {"buy_value": 0, "sell_value": 0, "fees": 0}
        if side == "Buy":
            summary[s]["buy_value"] += val
        else:
            summary[s]["sell_value"] += val
        summary[s]["fees"] += fee
    result_list = []
    for s, v in summary.items():
        if v["sell_value"] < 5:
            continue
        pnl_val = v["sell_value"] - v["buy_value"] - v["fees"]
        pnl_pct = (pnl_val / v["buy_value"] * 100) if v["buy_value"] > 0 else 0
        result_list.append({
            "symbol": s, "pnl": round(pnl_val, 4),
            "pnl_pct": round(pnl_pct, 2), "fees": round(v["fees"], 4),
            "buy_value": round(v["buy_value"], 4),
            "sell_value": round(v["sell_value"], 4),
        })
    return jsonify({"result": result_list}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
