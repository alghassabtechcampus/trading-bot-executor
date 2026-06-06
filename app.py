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
    print(f"POST pre_sign: {pre_sign}", flush=True)
    return base_headers(ts, sign(pre_sign))

def verify_webhook(req) -> bool:
    sig = req.headers.get("X-Webhook-Signature", "")
    return sig == WEBHOOK_SECRET

def place_order(symbol, side, qty, stop_loss, take_profit):
    body = {
        "category"   : "spot",
        "symbol"     : symbol,
        "side"       : side,
        "orderType"  : "Market",
        "qty"        : str(qty),
        "timeInForce": "IOC",
    }
    if stop_loss and float(stop_loss) > 0:
        body["stopLoss"] = str(stop_loss)
    if take_profit and float(take_profit) > 0:
        body["takeProfit"] = str(take_profit)
    
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
    resp = requests.get(BYBIT_BASE_URL + "/v5/account/wallet-balance", headers=headers, params=params, timeout=10)
    return jsonify(resp.json()), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
