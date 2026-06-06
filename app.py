from flask import Flask, request, jsonify
import hashlib
import hmac
import time
import requests
import os
import json

app = Flask(__name__)

# === إعدادات Bybit ===
BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY", "YOUR_API_KEY")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "YOUR_API_SECRET")
BYBIT_BASE_URL   = "https://api-demo.bybit.com"   # Demo — غيّره إلى api.bybit.com للحساب الحقيقي

# سر مشترك بين n8n وهذا السيرفر للتحقق من الطلبات
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "my_super_secret_123")

# ============================================================
# توليد توقيع Bybit (HMAC SHA256) — الإصلاح الذي كان معلقاً
# ============================================================
def bybit_sign(params: dict) -> dict:
    """يضيف timestamp وrecv_window والتوقيع لأي طلب Bybit"""
    timestamp  = str(int(time.time() * 1000))
    recv_window = "5000"

    # ترتيب الـ params أبجدياً ثم بناء query string
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    pre_sign = f"{timestamp}{BYBIT_API_KEY}{recv_window}{sorted_params}"

    signature = hmac.new(
        BYBIT_API_SECRET.encode("utf-8"),
        pre_sign.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return {
        "X-BAPI-API-KEY"    : BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP"  : timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN"       : signature,
        "Content-Type"      : "application/json",
    }

# ============================================================
# التحقق من صحة الطلب القادم من n8n
# ============================================================
def verify_webhook(req) -> bool:
    sig = req.headers.get("X-Webhook-Signature", "")
    return sig == WEBHOOK_SECRET
# ============================================================
# تنفيذ أمر شراء / بيع على Bybit
# ============================================================
def place_order(symbol: str, side: str, qty: float,
                stop_loss: float, take_profit: float) -> dict:
    endpoint = "/v5/order/create"
    body = {
        "category"   : "spot",
        "symbol"     : symbol,
        "side"       : side,           # "Buy" أو "Sell"
        "orderType"  : "Market",
        "qty"        : str(qty),
        "stopLoss"   : str(round(stop_loss, 4)),
        "takeProfit" : str(round(take_profit, 4)),
        "timeInForce": "IOC",
    }
    headers = bybit_sign(body)
    resp = requests.post(
        BYBIT_BASE_URL + endpoint,
        headers=headers,
        json=body,
        timeout=10
    )
    return resp.json()

# ============================================================
# Endpoints
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": int(time.time())}), 200


@app.route("/execute", methods=["POST"])
def execute():
    """
    يستقبل إشارة من n8n وينفذها على Bybit.
    Body مثال:
    {
      "symbol": "BTCUSDT",
      "action": "BUY",
      "price": 65000,
      "stopLoss": 64350,
      "takeProfit": 67600,
      "qty": 0.001
    }
    """
    # التحقق من التوقيع
    if not verify_webhook(request):
        return jsonify({"error": "Unauthorized — invalid signature"}), 401

    data = request.get_json(force=True)
    required = ["symbol", "action", "price", "stopLoss", "takeProfit", "qty"]
    if not all(k in data for k in required):
        return jsonify({"error": f"Missing fields. Required: {required}"}), 400

    symbol      = data["symbol"].upper()
    action      = data["action"].upper()   # BUY أو SELL
    stop_loss   = float(data["stopLoss"])
    take_profit = float(data["takeProfit"])
    qty         = float(data["qty"])

    if action not in ("BUY", "SELL"):
        return jsonify({"error": "action must be BUY or SELL"}), 400

    side = "Buy" if action == "BUY" else "Sell"

    result = place_order(symbol, side, qty, stop_loss, take_profit)

    # تسجيل في الـ log
    log_entry = {
        "time"      : int(time.time()),
        "symbol"    : symbol,
        "action"    : action,
        "qty"       : qty,
        "stopLoss"  : stop_loss,
        "takeProfit": take_profit,
        "bybitResp" : result,
    }
    print(json.dumps(log_entry, ensure_ascii=False))

    if result.get("retCode") == 0:
        return jsonify({"status": "executed", "order": result}), 200
    else:
        return jsonify({"status": "bybit_error", "detail": result}), 502


@app.route("/balance", methods=["GET"])
def balance():
    """جلب رصيد الحساب"""
    params = {"accountType": "UNIFIED"}
    headers = bybit_sign(params)
    resp = requests.get(
        BYBIT_BASE_URL + "/v5/account/wallet-balance",
        headers=headers,
        params=params,
        timeout=10
    )
    return jsonify(resp.json()), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
