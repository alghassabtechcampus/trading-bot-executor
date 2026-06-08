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
BYBIT_BASE_URL   = "https://api-demo.bybit.com"
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "my_super_secret_123")

# العملات الأصلية التي لا يلمسها البوت
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

def format_qty(coin: str, qty: float) -> str:
    if coin in ["BTC", "ETH"]:
        return "{:.6f}".format(qty)
    elif coin in ["XRP", "ADA", "XLM", "DOT", "BNB", "LINK"]:
        return "{:.1f}".format(qty)
    else:
        return "{:.4f}".format(qty)

def place_order(symbol, side, qty, stop_loss, take_profit):
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
    return resp.json()

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": int(time.time())}), 200

@app.route("/execute", methods=["POST"])
def execute():
    if not verify_webhook(request):
        return jsonify({"error": "Unauthorized — invalid signature"}), 401

    data = request.get_json(force=True)
    required = ["symbol", "action", "price", "stopLoss", "takeProfit", "qty"]
    if not all(k in data for k in required):
        return jsonify({"error": f"Missing fields. Required: {required}"}), 400

    symbol      = data["symbol"].upper()
    action      = data["action"].upper()
    stop_loss   = data["stopLoss"]
    take_profit = data["takeProfit"]
    qty         = data["qty"]

    if action not in ("BUY", "SELL"):
        return jsonify({"error": "action must be BUY or SELL"}), 400

    side = "Buy" if action == "BUY" else "Sell"
    result = place_order(symbol, side, qty, stop_loss, take_profit)

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

@app.route("/pnl", methods=["GET"])
def pnl():
    params = {
        "category": "spot",
        "limit": "50"
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
        qty = float(order.get("cumExecQty", 0))
        value = float(order.get("cumExecValue", 0))
        fee = float(order.get("cumExecFee", 0))

        if symbol not in summary:
            summary[symbol] = {"buy_value": 0, "sell_value": 0, "buy_qty": 0, "sell_qty": 0, "fees": 0}

        if side == "Buy":
            summary[symbol]["buy_value"] += value
            summary[symbol]["buy_qty"] += qty
        else:
            summary[symbol]["sell_value"] += value
            summary[symbol]["sell_qty"] += qty

        summary[symbol]["fees"] += fee

    result_list = []
    for symbol, s in summary.items():
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

    if data.get("retCode") != 0:
        return jsonify({"has_position": False}), 200

    coins = data["result"]["list"][0]["coin"]
    for c in coins:
        if c["coin"] == coin:
            bal = float(c["walletBalance"])
            usd_value = float(c["usdValue"])
            if usd_value > 5:
                return jsonify({"has_position": True, "balance": bal, "usd_value": usd_value}), 200

    return jsonify({"has_position": False}), 200

@app.route("/sell", methods=["POST"])
def sell():
    if not verify_webhook(request):
        return jsonify({"error": "Unauthorized — invalid signature"}), 401

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

    qty = 0
    for c in balance_data["result"]["list"][0]["coin"]:
        if c["coin"] == coin:
            avail = c.get("availableToWithdraw", "")
            qty = float(avail) if avail else float(c["walletBalance"])
            break

    if qty <= 0:
        return jsonify({"error": "No balance to sell"}), 400

    qty_str = format_qty(coin, qty)
    print(f"DEBUG sell: coin={coin} qty={qty} qty_str={qty_str}", flush=True)

    body = {
        "category"   : "spot",
        "symbol"     : symbol,
        "side"       : "Sell",
        "orderType"  : "Market",
        "qty"        : qty_str,
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

    print(json.dumps({"time": int(time.time()), "symbol": symbol, "action": "SELL", "qty": qty_str, "result": result}, ensure_ascii=False))

    if result.get("retCode") == 0:
        return jsonify({"status": "sold", "order": result}), 200
    else:
        return jsonify({"status": "bybit_error", "detail": result}), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
