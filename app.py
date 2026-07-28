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

ORIGINAL_COINS = ["BTC", "ETH", "USDC"]

def sign(pre_sign: str) -> str:
    return hmac.new(
        BYBIT_API_SECRET.encode(),
        pre_sign.encode(),
        hashlib.sha256
    ).hexdigest()

def base_headers(timestamp: str, sig: str) -> dict:
    return {
        "X-BAPI-API-KEY"    : BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP"  : timestamp,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN"       : sig,
        "Content-Type"      : "application/json",
    }

def get_headers(params: dict) -> dict:
    ts = str(int(time.time() * 1000))
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    pre_sign = f"{ts}{BYBIT_API_KEY}5000{query}"
    return base_headers(ts, sign(pre_sign))

def post_headers(body: dict) -> dict:
    ts = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(',', ':'))
    pre_sign = f"{ts}{BYBIT_API_KEY}5000{body_str}"
    return base_headers(ts, sign(pre_sign))

def verify_webhook(req) -> bool:
    sig = req.headers.get("X-Webhook-Signature", "")
    return sig == WEBHOOK_SECRET

def get_last_buy_price(symbol: str) -> dict:
    """جلب آخر سعر شراء للعملة من Bybit مباشرة"""
    params = {
        "category": "spot",
        "symbol": symbol,
        "limit": "10"
    }
    headers = get_headers(params)
    resp = requests.get(
        BYBIT_BASE_URL + "/v5/order/history",
        headers=headers,
        params=params,
        timeout=10
    )
    data = resp.json()
    if data.get("retCode") != 0:
        return {}
    
    orders = data.get("result", {}).get("list", [])
    for order in orders:
        if order.get("side") == "Buy" and order.get("orderStatus") == "Filled":
            return {
                "buy_price": float(order.get("avgPrice", 0)),
                "buy_value": float(order.get("cumExecValue", 100)),
                "buy_time": int(order.get("updatedTime", 0)) // 1000
            }
    return {}

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": int(time.time())}), 200

@app.route("/execute", methods=["POST"])
def execute():
    if not verify_webhook(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True)
    required = ["symbol", "action", "price", "stopLoss", "takeProfit", "qty"]
    if not all(k in data for k in required):
        return jsonify({"error": "Missing fields"}), 400

    symbol      = data["symbol"].upper()
    action      = data["action"].upper()
    stop_loss   = data["stopLoss"]
    take_profit = data["takeProfit"]
    qty         = data["qty"]

    if action not in ("BUY", "SELL"):
        return jsonify({"error": "action must be BUY or SELL"}), 400

    side = "Buy" if action == "BUY" else "Sell"
    
    body = {
        "category"   : "spot",
        "symbol"     : symbol,
        "side"       : side,
        "orderType"  : "Market",
        "qty"        : str(qty),
        "timeInForce": "IOC",
    }
    body_str = json.dumps(body, separators=(',', ':'))
    headers = post_headers(body)
    resp = requests.post(
        BYBIT_BASE_URL + "/v5/order/create",
        headers=headers,
        data=body_str,
        timeout=10
    )
    result = resp.json()

    print(json.dumps({"time": int(time.time()), "symbol": symbol, "action": action, "result": result}, ensure_ascii=False))

    if result.get("retCode") == 0:
        return jsonify({"status": "executed", "order": result}), 200
    else:
        return jsonify({"status": "bybit_error", "detail": result}), 502

@app.route("/balance", methods=["GET"])
def balance():
    params = {"accountType": "UNIFIED"}
    headers = get_headers(params)
    resp = requests.get(
        BYBIT_BASE_URL + "/v5/account/wallet-balance",
        headers=headers,
        params=params,
        timeout=10
    )
    return jsonify(resp.json()), 200

@app.route("/positions", methods=["GET"])
def positions():
    """يجلب المواقف المفتوحة من Bybit مباشرة بدون ذاكرة"""
    params = {"accountType": "UNIFIED"}
    headers = get_headers(params)
    resp = requests.get(
        BYBIT_BASE_URL + "/v5/account/wallet-balance",
        headers=headers,
        params=params,
        timeout=10
    )
    balance_data = resp.json()

    watch_list = ["BNB", "LINK", "XRP", "ADA", "LTC"]
    result = []

    for c in balance_data["result"]["list"][0]["coin"]:
        coin = c["coin"]
        if coin not in watch_list:
            continue
        
        usd_value = float(c["usdValue"])
        if usd_value < 5:
            continue

        symbol = coin + "USDT"
        trade_info = get_last_buy_price(symbol)
        
        if not trade_info:
            continue

        buy_price = trade_info.get("buy_price", 0)
        buy_value = trade_info.get("buy_value", 100)
        buy_time = trade_info.get("buy_time", 0)

        if buy_value <= 0:
            continue

        pnl_pct = ((usd_value - buy_value) / buy_value) * 100

        result.append({
            "symbol": symbol,
            "buy_price": buy_price,
            "buy_value": buy_value,
            "buy_time": buy_time,
            "usd_value": usd_value,
            "pnl_pct": round(pnl_pct, 2)
        })

    return jsonify({"result": result}), 200

@app.route("/pnl", methods=["GET"])
def pnl():
    hours = request.args.get("hours", "24")
    start_time = str(int((time.time() - int(hours) * 3600) * 1000))
    params = {
        "category": "spot",
        "limit": "50",
        "startTime": start_time
    }
    headers = get_headers(params)
    resp = requests.get(
        BYBIT_BASE_URL + "/v5/order/history",
        headers=headers,
        params=params,
        timeout=10
    )
    data = resp.json()

    if data.get("retCode") != 0:
        return jsonify(data), 200

    orders = data.get("result", {}).get("list", [])

    summary = {}
    for order in orders:
        if order.get("orderStatus") != "Filled":
            continue
        symbol = order.get("symbol", "")
        side = order.get("side", "")
        value = float(order.get("cumExecValue", 0))
        fee = float(order.get("cumExecFee", 0))

        if symbol not in summary:
            summary[symbol] = {"buy_value": 0, "sell_value": 0, "fees": 0}

        if side == "Buy":
            summary[symbol]["buy_value"] += value
        else:
            summary[symbol]["sell_value"] += value

        summary[symbol]["fees"] += fee

    result_list = []
    for symbol, s in summary.items():
        if s["sell_value"] < 5:
            continue
        pnl_val = s["sell_value"] - s["buy_value"] - s["fees"]
        pnl_pct = (pnl_val / s["buy_value"] * 100) if s["buy_value"] > 0 else 0
        result_list.append({
            "symbol": symbol,
            "pnl": round(pnl_val, 4),
            "pnl_pct": round(pnl_pct, 2),
            "fees": round(s["fees"], 4),
            "buy_value": round(s["buy_value"], 4),
            "sell_value": round(s["sell_value"], 4),
        })

    return jsonify({"result": result_list}), 200

@app.route("/check_position", methods=["GET"])
def check_position():
    symbol = request.args.get("symbol", "")
    coin = symbol.replace("USDT", "")

    if coin in ORIGINAL_COINS:
        return jsonify({"has_position": False}), 200

    params = {"accountType": "UNIFIED"}
    headers = get_headers(params)
    resp = requests.get(
        BYBIT_BASE_URL + "/v5/account/wallet-balance",
        headers=headers,
        params=params,
        timeout=10
    )
    data = resp.json()
    for c in data["result"]["list"][0]["coin"]:
        if c["coin"] == coin:
            usd_value = float(c["usdValue"])
            if usd_value > 5:
                return jsonify({"has_position": True, "usd_value": usd_value}), 200

    return jsonify({"has_position": False}), 200

@app.route("/usdt_balance", methods=["GET"])
def usdt_balance():
    params = {"accountType": "UNIFIED"}
    headers = get_headers(params)
    resp = requests.get(
        BYBIT_BASE_URL + "/v5/account/wallet-balance",
        headers=headers,
        params=params,
        timeout=10
    )
    data = resp.json()
    usdt = 0
    for c in data["result"]["list"][0]["coin"]:
        if c["coin"] == "USDT":
            usdt = float(c["usdValue"])
            break
    has_balance = usdt >= 100
    return jsonify({"usdt": usdt, "has_balance": has_balance}), 200

@app.route("/sell", methods=["POST"])
def sell():
    if not verify_webhook(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True)
    symbol = data.get("symbol", "").upper()
    coin = symbol.replace("USDT", "")

    if coin in ORIGINAL_COINS:
        return jsonify({"error": "Cannot sell original coin"}), 400

    params = {"accountType": "UNIFIED"}
    headers = get_headers(params)
    resp = requests.get(
        BYBIT_BASE_URL + "/v5/account/wallet-balance",
        headers=headers,
        params=params,
        timeout=10
    )
    balance_data = resp.json()

    usd_value = 0
    qty_balance = 0
    for c in balance_data["result"]["list"][0]["coin"]:
        if c["coin"] == coin:
            usd_value = float(c["usdValue"])
            qty_balance = float(c["walletBalance"])
            break

    if usd_value < 10:
        return jsonify({"error": "Balance too small"}), 400

    if coin in ["BTC", "ETH"]:
        qty_str = "{:.6f}".format(qty_balance * 0.999)
        body = {
            "category"   : "spot",
            "symbol"     : symbol,
            "side"       : "Sell",
            "orderType"  : "Market",
            "qty"        : qty_str,
            "timeInForce": "IOC",
        }
    elif coin == "BNB":
        qty_str = "{:.3f}".format(qty_balance * 0.999)
        body = {
            "category"   : "spot",
            "symbol"     : symbol,
            "side"       : "Sell",
            "orderType"  : "Market",
            "qty"        : qty_str,
            "timeInForce": "IOC",
        }
    elif coin in ["XRP", "ADA"]:
        qty_str = "{:.2f}".format(qty_balance * 0.999)
        body = {
            "category"   : "spot",
            "symbol"     : symbol,
            "side"       : "Sell",
            "orderType"  : "Market",
            "qty"        : qty_str,
            "timeInForce": "IOC",
        }
    elif coin == "LTC":
        qty_str = "{:.4f}".format(qty_balance * 0.999)
        body = {
            "category"   : "spot",
            "symbol"     : symbol,
            "side"       : "Sell",
            "orderType"  : "Market",
            "qty"        : qty_str,
            "timeInForce": "IOC",
        }
    else:
        sell_amount = str(round(usd_value * 0.999, 2))
        body = {
            "category"   : "spot",
            "symbol"     : symbol,
            "side"       : "Sell",
            "orderType"  : "Market",
            "marketUnit" : "quoteCoin",
            "qty"        : sell_amount,
            "timeInForce": "IOC",
        }

    body_str = json.dumps(body, separators=(',', ':'))
    headers = post_headers(body)
    resp = requests.post(
        BYBIT_BASE_URL + "/v5/order/create",
        headers=headers,
        data=body_str,
        timeout=10
    )
    result = resp.json()

    print(json.dumps({"time": int(time.time()), "symbol": symbol, "action": "SELL", "result": result}, ensure_ascii=False))

    if result.get("retCode") == 0:
        return jsonify({"status": "sold", "order": result}), 200
    else:
        return jsonify({"status": "bybit_error", "detail": result}), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
