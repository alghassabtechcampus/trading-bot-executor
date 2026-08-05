from __future__ import annotations

from urllib.parse import urlencode
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from functools import wraps
from typing import Any
import hashlib
import hmac
import json
import logging
import os
import time

import requests
from flask import Flask, jsonify, request
from dotenv import load_dotenv
load_dotenv('.env')

app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("bybit-trading-server")

# ============================================================
# الإعدادات
# ============================================================

BYBIT_API_KEY = os.environ["BYBIT_API_KEY"]
BYBIT_API_SECRET = os.environ["BYBIT_API_SECRET"]
WEBHOOK_SECRET = os.environ["TRADING_WEBHOOK_SECRET"]

BYBIT_BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com").rstrip("/")
REDIS_URL = os.environ["REDIS_URL"].rstrip("/")
REDIS_TOKEN = os.environ["REDIS_TOKEN"]

TRADE_AMOUNT_USDT = Decimal(os.getenv("TRADE_AMOUNT_USDT", "100"))
MIN_BALANCE_BUFFER = Decimal(os.getenv("MIN_BALANCE_BUFFER", "1.01"))
MIN_POSITION_USD = Decimal(os.getenv("MIN_POSITION_USD", "5"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "12"))
SIGNAL_LOCK_SECONDS = int(os.getenv("SIGNAL_LOCK_SECONDS", "86400"))
MAX_HOLD_MINUTES = int(os.getenv("MAX_HOLD_MINUTES", "60"))

# العملات الموجودة قبل تشغيل البوت والتي لا نريد بيع رصيدها القديم.
# البوت سيبيع فقط executed_qty المخزنة للصفقة، لذلك القائمة للحماية الإضافية.
PROTECTED_COINS = {
    coin.strip().upper()
    for coin in os.getenv("PROTECTED_COINS", "BTC,ETH,USDC").split(",")
    if coin.strip()
}

# ============================================================
# أدوات عامة
# ============================================================

def now_ms() -> str:
    return str(int(time.time() * 1000))


def decimal_str(value: Decimal) -> str:
    """تحويل Decimal إلى نص عادي بدون صيغة علمية."""
    return format(value.normalize(), "f")


def as_decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc

    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def coin_from_symbol(symbol: str) -> str:
    if not symbol.endswith("USDT"):
        raise ValueError("Only USDT spot symbols are supported")
    return symbol[:-4]


def safe_json_response(response: requests.Response) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid JSON response: HTTP {response.status_code}"
        ) from exc


# ============================================================
# حماية Webhook
# ============================================================

def verify_webhook() -> bool:
    supplied = request.headers.get("X-Webhook-Signature", "")
    return hmac.compare_digest(supplied, WEBHOOK_SECRET)


def protected_route(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not verify_webhook():
            return jsonify({"error": "Unauthorized"}), 401
        return function(*args, **kwargs)
    return wrapper


# ============================================================
# Redis REST
# متوافق مع Upstash Redis REST عبر أوامر JSON
# ============================================================

def redis_command(*command: Any) -> Any:
    headers = {
        "Authorization": f"Bearer {REDIS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            REDIS_URL,
            headers=headers,
            json=[str(part) for part in command],
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = safe_json_response(response)
    except requests.RequestException as exc:
        logger.exception("Redis request failed")
        raise RuntimeError("Redis unavailable") from exc

    if "error" in payload:
        raise RuntimeError(f"Redis error: {payload['error']}")

    return payload.get("result")


def redis_set_json(
    key: str,
    value: dict[str, Any],
    *,
    ex_seconds: int | None = None,
    nx: bool = False,
) -> bool:
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    command: list[Any] = ["SET", key, encoded]

    if nx:
        command.append("NX")
    if ex_seconds is not None:
        command.extend(["EX", ex_seconds])

    result = redis_command(*command)
    return result == "OK"


def redis_get_json(key: str) -> dict[str, Any] | None:
    result = redis_command("GET", key)
    if result is None:
        return None

    try:
        parsed = json.loads(result)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid Redis JSON for key {key}") from exc

    return parsed if isinstance(parsed, dict) else None


def redis_delete(key: str) -> None:
    redis_command("DEL", key)


def redis_keys(pattern: str) -> list[str]:
    result = redis_command("KEYS", pattern)
    return result if isinstance(result, list) else []


# ============================================================
# توقيع Bybit V5
# ============================================================

def sign(payload: str) -> str:
    return hmac.new(
        BYBIT_API_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def base_headers(timestamp: str, signature: str) -> dict[str, str]:
    return {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json",
    }


def get_headers(params: dict[str, Any]) -> dict[str, str]:
    timestamp = now_ms()
    query = urlencode(params)

    signature = sign(
        f"{timestamp}{BYBIT_API_KEY}5000{query}"
    )

    return base_headers(timestamp, signature)

def post_headers(body: dict[str, Any]) -> dict[str, str]:
    timestamp = now_ms()
    body_string = json.dumps(body, separators=(",", ":"))
    signature = sign(f"{timestamp}{BYBIT_API_KEY}5000{body_string}")
    return base_headers(timestamp, signature)


def bybit_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.get(
            BYBIT_BASE_URL + path,
            params=params,
            headers=get_headers(params),
            timeout=REQUEST_TIMEOUT,
        )
        payload = safe_json_response(response)
    except requests.RequestException as exc:
        logger.exception("Bybit GET failed: %s", path)
        raise RuntimeError("Bybit network error") from exc

    if response.status_code >= 400:
        raise RuntimeError(f"Bybit HTTP {response.status_code}: {payload}")

    return payload


def bybit_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    body_string = json.dumps(body, separators=(",", ":"))

    try:
        response = requests.post(
            BYBIT_BASE_URL + path,
            headers=post_headers(body),
            data=body_string,
            timeout=REQUEST_TIMEOUT,
        )
        payload = safe_json_response(response)
    except requests.RequestException as exc:
        logger.exception("Bybit POST failed: %s", path)
        raise RuntimeError("Bybit network error") from exc

    if response.status_code >= 400:
        raise RuntimeError(f"Bybit HTTP {response.status_code}: {payload}")

    return payload


def ensure_bybit_success(payload: dict[str, Any], operation: str) -> None:
    if payload.get("retCode") != 0:
        raise RuntimeError(
            f"{operation} failed: "
            f"{payload.get('retCode')} {payload.get('retMsg')}"
        )


# ============================================================
# بيانات الحساب والسوق
# ============================================================

def get_wallet_balance() -> dict[str, Any]:
    params = {"accountType": "UNIFIED"}
    payload = bybit_get("/v5/account/wallet-balance", params)
    ensure_bybit_success(payload, "wallet balance")
    return payload


def wallet_coins() -> list[dict[str, Any]]:
    payload = get_wallet_balance()
    lists = payload.get("result", {}).get("list", [])

    if not lists:
        raise RuntimeError("Wallet response contains no account list")

    coins = lists[0].get("coin", [])
    if not isinstance(coins, list):
        raise RuntimeError("Wallet response contains invalid coin list")

    return coins


def get_coin_balance(coin: str) -> dict[str, Decimal]:
    for item in wallet_coins():
        if item.get("coin") == coin:
            wallet_value = item.get("walletBalance") or "0"
            available_value = (
                item.get("availableToWithdraw")
                or item.get("walletBalance")
                or "0"
            )
            usd_value = item.get("usdValue") or "0"

            return {
                "wallet_balance": as_decimal(
                    wallet_value,
                    "walletBalance",
                ),
                "available": as_decimal(
                    available_value,
                    "available",
                ),
                "usd_value": as_decimal(
                    usd_value,
                    "usdValue",
                ),
            }

    return {
        "wallet_balance": Decimal("0"),
        "available": Decimal("0"),
        "usd_value": Decimal("0"),
    }


def get_usdt_balance() -> Decimal:
    return get_coin_balance("USDT")["usd_value"]


def get_ticker_price(symbol: str) -> Decimal:
    params = {"category": "spot", "symbol": symbol}
    payload = bybit_get("/v5/market/tickers", params)
    ensure_bybit_success(payload, "ticker")

    tickers = payload.get("result", {}).get("list", [])
    if not tickers:
        raise RuntimeError(f"No ticker returned for {symbol}")

    return as_decimal(tickers[0].get("lastPrice"), "lastPrice")


def get_qty_step(symbol: str) -> tuple[Decimal, Decimal]:
    params = {"category": "spot", "symbol": symbol}
    payload = bybit_get("/v5/market/instruments-info", params)
    ensure_bybit_success(payload, "instrument info")

    instruments = payload.get("result", {}).get("list", [])
    if not instruments:
        raise RuntimeError(f"No instrument info returned for {symbol}")

    lot = instruments[0].get("lotSizeFilter", {})
    qty_step = as_decimal(lot.get("basePrecision", "0.00000001"), "basePrecision")
    min_qty = as_decimal(lot.get("minOrderQty", "0"), "minOrderQty")
    return qty_step, min_qty


# ============================================================
# أوامر Bybit
# ============================================================

def create_market_buy(
    symbol: str,
    quote_amount: Decimal,
    order_link_id: str,
) -> dict[str, Any]:
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Market",
        "marketUnit": "quoteCoin",
        "qty": decimal_str(quote_amount),
        "timeInForce": "IOC",
        "orderLinkId": order_link_id[:36],
    }

    payload = bybit_post("/v5/order/create", body)
    ensure_bybit_success(payload, "market buy")
    return payload


def create_market_sell(
    symbol: str,
    base_quantity: Decimal,
    order_link_id: str,
) -> dict[str, Any]:
    qty_step, min_qty = get_qty_step(symbol)
    quantity = floor_to_step(base_quantity, qty_step)

    if quantity <= 0 or quantity < min_qty:
        raise ValueError(
            f"Sell quantity {quantity} is below minimum {min_qty}"
        )

    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Sell",
        "orderType": "Market",
        "marketUnit": "baseCoin",
        "qty": decimal_str(quantity),
        "timeInForce": "IOC",
        "orderLinkId": order_link_id[:36],
    }

    payload = bybit_post("/v5/order/create", body)
    ensure_bybit_success(payload, "market sell")
    return payload


def get_order(symbol: str, order_id: str) -> dict[str, Any] | None:
    params = {
        "category": "spot",
        "symbol": symbol,
        "orderId": order_id,
    }

    payload = bybit_get("/v5/order/realtime", params)
    ensure_bybit_success(payload, "order query")

    orders = payload.get("result", {}).get("list", [])
    return orders[0] if orders else None


def wait_for_fill(
    symbol: str,
    order_id: str,
    attempts: int = 8,
    delay: float = 0.75,
) -> dict[str, Any]:
    last_order: dict[str, Any] | None = None

    for _ in range(attempts):
        order = get_order(symbol, order_id)
        if order:
            last_order = order
            status = order.get("orderStatus")

            if status == "Filled":
                return order

            if status in {"Cancelled", "Rejected", "Deactivated"}:
                raise RuntimeError(f"Order ended with status {status}")

        time.sleep(delay)

    if last_order:
        executed_qty = as_decimal(
            last_order.get("cumExecQty", "0"),
            "cumExecQty",
        )
        if executed_qty > 0:
            # نقبل الامتلاء الجزئي، ونخزن فقط الكمية المنفذة.
            return last_order

    raise RuntimeError("Order was not filled within confirmation window")


def execution_details(order: dict[str, Any]) -> dict[str, Decimal | str]:
    executed_qty = as_decimal(order.get("cumExecQty", "0"), "cumExecQty")
    executed_value = as_decimal(
        order.get("cumExecValue", "0"),
        "cumExecValue",
    )
    average_price = as_decimal(order.get("avgPrice", "0"), "avgPrice")

    if average_price <= 0 and executed_qty > 0:
        average_price = executed_value / executed_qty

    if executed_qty <= 0 or executed_value <= 0 or average_price <= 0:
        raise RuntimeError("Order execution details are incomplete")

    return {
        "order_id": str(order.get("orderId", "")),
        "status": str(order.get("orderStatus", "")),
        "executed_qty": executed_qty,
        "executed_value": executed_value,
        "average_price": average_price,
    }


# ============================================================
# الصفقات المخزنة
# ============================================================

def trade_key(symbol: str) -> str:
    return f"trade:{symbol}"


def signal_key(signal_id: str) -> str:
    return f"signal:{signal_id}"


def reserve_signal(signal_id: str, symbol: str) -> bool:
    return redis_set_json(
        signal_key(signal_id),
        {
            "status": "processing",
            "symbol": symbol,
            "time": int(time.time()),
        },
        ex_seconds=SIGNAL_LOCK_SECONDS,
        nx=True,
    )


def mark_signal(
    signal_id: str,
    symbol: str,
    status: str,
    **extra: Any,
) -> None:
    redis_set_json(
        signal_key(signal_id),
        {
            "status": status,
            "symbol": symbol,
            "time": int(time.time()),
            **extra,
        },
        ex_seconds=SIGNAL_LOCK_SECONDS,
    )


def tracked_trade(symbol: str) -> dict[str, Any] | None:
    return redis_get_json(trade_key(symbol))


def close_tracked_trade(
    symbol: str,
    reason: str,
) -> dict[str, Any]:
    trade = tracked_trade(symbol)
    if not trade:
        raise ValueError(f"No tracked trade for {symbol}")

    requested_qty = as_decimal(
        trade.get("executed_qty", "0"),
        "executed_qty",
    )
    coin = coin_from_symbol(symbol)
    wallet_qty = get_coin_balance(coin)["wallet_balance"]

    # نبيع فقط الأقل بين كمية الصفقة المسجلة والرصيد المتاح.
    quantity = min(requested_qty, wallet_qty) * Decimal("0.999")

    response = create_market_sell(
        symbol,
        quantity,
        f"sell-{int(time.time())}-{symbol}",
    )

    order_id = response.get("result", {}).get("orderId")
    if not order_id:
        raise RuntimeError("Sell response does not contain orderId")

    order = wait_for_fill(symbol, order_id)
    details = execution_details(order)

    redis_set_json(
        f"closed:{symbol}:{int(time.time())}",
        {
            **trade,
            "sell_reason": reason,
            "sell_order_id": details["order_id"],
            "sell_price": decimal_str(details["average_price"]),
            "sell_qty": decimal_str(details["executed_qty"]),
            "sell_value": decimal_str(details["executed_value"]),
            "sell_time": int(time.time()),
        },
        ex_seconds=60 * 60 * 24 * 90,
    )

    redis_delete(trade_key(symbol))

    return {
        "status": "sold",
        "trade_id": trade.get("trade_id"),
        "symbol": symbol,
        "reason": reason,
        "order": response,
        "execution": {
            key: decimal_str(value) if isinstance(value, Decimal) else value
            for key, value in details.items()
        },
    }


# ============================================================
# المسارات
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "time": int(time.time()),
    }), 200


@app.route("/balance", methods=["GET"])
@protected_route
def balance():
    return jsonify(get_wallet_balance()), 200


@app.route("/usdt_balance", methods=["GET"])
@protected_route
def usdt_balance():
    usdt = get_usdt_balance()
    required = TRADE_AMOUNT_USDT * MIN_BALANCE_BUFFER

    return jsonify({
        "usdt": decimal_str(usdt),
        "required": decimal_str(required),
        "has_balance": usdt >= required,
    }), 200


@app.route("/check_position", methods=["GET"])
@protected_route
def check_position():
    symbol = request.args.get("symbol", "").upper().strip()

    try:
        coin_from_symbol(symbol)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    trade = tracked_trade(symbol)
    if not trade:
        return jsonify({"has_position": False}), 200

    executed_qty = as_decimal(
        trade.get("executed_qty", "0"),
        "executed_qty",
    )
    coin = coin_from_symbol(symbol)
    wallet_qty = get_coin_balance(coin)["wallet_balance"]

    if executed_qty > 0 and wallet_qty > 0:
        return jsonify({
            "has_position": True,
            "trade": trade,
        }), 200

    redis_delete(trade_key(symbol))
    return jsonify({"has_position": False}), 200


@app.route("/check_signal", methods=["GET"])
@protected_route
def check_signal():
    signal_id = request.args.get("signal_id", "").strip()

    if not signal_id:
        return jsonify({"exists": False}), 200

    record = redis_get_json(signal_key(signal_id))
    return jsonify({
        "exists": record is not None,
        "signal": record,
    }), 200


@app.route("/execute", methods=["POST"])
@protected_route
def execute():
    data = request.get_json(silent=True) or {}

    required = {
        "symbol",
        "action",
        "price",
        "stopLoss",
        "takeProfit",
        "qty",
        "signal_id",
        "trade_id",
    }
    missing = sorted(required.difference(data))

    if missing:
        return jsonify({
            "error": "Missing fields",
            "fields": missing,
        }), 400

    try:
        symbol = str(data["symbol"]).upper().strip()
        action = str(data["action"]).upper().strip()
        signal_id = str(data["signal_id"]).strip()
        trade_id = str(data["trade_id"]).strip()

        analysis_price = as_decimal(data["price"], "price")
        stop_loss = as_decimal(data["stopLoss"], "stopLoss")
        take_profit = as_decimal(data["takeProfit"], "takeProfit")
        quote_amount = as_decimal(data["qty"], "qty")

        coin_from_symbol(symbol)

        if action != "BUY":
            raise ValueError("/execute accepts BUY only")

        if not signal_id:
            raise ValueError("signal_id is required")
        if not trade_id:
            raise ValueError("trade_id is required")   

        if analysis_price <= 0 or quote_amount <= 0:
            raise ValueError("price and qty must be positive")

        if stop_loss <= 0 or stop_loss >= analysis_price:
            raise ValueError("stopLoss must be below price")

        if take_profit <= analysis_price:
            raise ValueError("takeProfit must be above price")

        if quote_amount > TRADE_AMOUNT_USDT:
            raise ValueError(
                f"qty exceeds configured maximum {TRADE_AMOUNT_USDT}"
            )

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # منع صفقة جديدة إذا كانت هناك صفقة مسجلة لنفس العملة.
    if tracked_trade(symbol):
        return jsonify({
            "status": "position_exists",
            "symbol": symbol,
        }), 409

    # حجز ذري للإشارة قبل إرسال أمر الشراء.
    try:
        reserved = reserve_signal(signal_id, symbol)
    except RuntimeError as exc:
        return jsonify({
            "status": "redis_error",
            "error": str(exc),
        }), 503

    if not reserved:
        return jsonify({
            "status": "duplicate_signal",
            "signal_id": signal_id,
        }), 409

    try:
        usdt = get_usdt_balance()
        required_balance = quote_amount * MIN_BALANCE_BUFFER

        if usdt < required_balance:
            mark_signal(
                signal_id,
                symbol,
                "rejected_balance",
                available=decimal_str(usdt),
                required=decimal_str(required_balance),
            )
            return jsonify({
                "status": "insufficient_balance",
                "available": decimal_str(usdt),
                "required": decimal_str(required_balance),
            }), 400

        response = create_market_buy(
            symbol,
            quote_amount,
            signal_id,
        )

        order_id = response.get("result", {}).get("orderId")
        if not order_id:
            raise RuntimeError("Buy response does not contain orderId")

        order = wait_for_fill(symbol, order_id)
        details = execution_details(order)

        trade = {
            "trade_id": trade_id,
            "symbol": symbol,
            "coin": coin_from_symbol(symbol),
            "signal_id": signal_id,
            "buy_order_id": details["order_id"],
            "buy_status": details["status"],
            "analysis_price": decimal_str(analysis_price),
            "buy_price": decimal_str(details["average_price"]),
            "buy_value": decimal_str(details["executed_value"]),
            "executed_qty": decimal_str(details["executed_qty"]),
            "stop_loss": decimal_str(stop_loss),
            "take_profit": decimal_str(take_profit),
            "time": int(time.time()),
        }

        redis_set_json(trade_key(symbol), trade)
        mark_signal(
            signal_id,
            symbol,
            "executed",
            order_id=details["order_id"],
        )

        logger.info(
            json.dumps(
                {
                    "symbol": symbol,
                    "action": "BUY",
                    "trade": trade,
                },
                ensure_ascii=False,
            )
        )

        return jsonify({
            "status": "executed",
            "trade": trade,
            "order": response,
        }), 200

    except Exception as exc:
        logger.exception("Buy execution failed for %s", symbol)

        try:
            mark_signal(
                signal_id,
                symbol,
                "failed",
                error=str(exc),
            )
        except Exception:
            logger.exception("Could not update failed signal state")

        return jsonify({
            "status": "execution_error",
            "error": str(exc),
        }), 502


@app.route("/sell", methods=["POST"])
@protected_route
def sell():
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "")).upper().strip()
    reason = str(data.get("reason", "manual")).strip() or "manual"

    try:
        coin = coin_from_symbol(symbol)

        if coin in PROTECTED_COINS and not tracked_trade(symbol):
            return jsonify({
                "error": "Protected coin without tracked bot trade"
            }), 400

        result = close_tracked_trade(symbol, reason)
        return jsonify(result), 200

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Sell failed for %s", symbol)
        return jsonify({
            "status": "sell_error",
            "error": str(exc),
        }), 502


@app.route("/monitor_positions", methods=["POST"])
@protected_route
def monitor_positions():
    """
    يجب استدعاء هذا المسار دوريًا من n8n، مثل كل دقيقة.
    إذا وصل السعر إلى وقف الخسارة أو الهدف، ينفذ البيع.
    """
    results: list[dict[str, Any]] = []

    for key in redis_keys("trade:*"):
        symbol = key.replace("trade:", "", 1)
        trade = tracked_trade(symbol)

        if not trade:
            continue

        try:
            current_price = get_ticker_price(symbol)
            stop_loss = as_decimal(trade["stop_loss"], "stop_loss")
            take_profit = as_decimal(trade["take_profit"], "take_profit")
            hold_minutes = (time.time() - int(trade["time"])) / 60

            if current_price <= stop_loss:
                sale = close_tracked_trade(symbol, "stop_loss")
                results.append({
                    "symbol": symbol,
                    "trigger": "stop_loss",
                    "price": decimal_str(current_price),
                    "result": sale,
                })

            elif current_price >= take_profit:
                sale = close_tracked_trade(symbol, "take_profit")
                results.append({
                    "symbol": symbol,
                    "trigger": "take_profit",
                    "price": decimal_str(current_price),
                    "result": sale,
                })

            elif hold_minutes >= MAX_HOLD_MINUTES:
                sale = close_tracked_trade(symbol, "max_hold_time")
                results.append({
                    "symbol": symbol,
                    "trigger": "max_hold_time",
                    "price": decimal_str(current_price),
                    "hold_minutes": round(hold_minutes, 1),
                    "result": sale,
                })

            else:
                results.append({
                    "symbol": symbol,
                    "trigger": None,
                    "price": decimal_str(current_price),
                    "stop_loss": decimal_str(stop_loss),
                    "take_profit": decimal_str(take_profit),
                })

        except Exception as exc:
            logger.exception("Position monitoring failed for %s", symbol)
            results.append({
                "symbol": symbol,
                "error": str(exc),
            })

    return jsonify({"result": results}), 200


@app.route("/positions", methods=["GET"])
@protected_route
def positions():
    result: list[dict[str, Any]] = []

    for key in redis_keys("trade:*"):
        symbol = key.replace("trade:", "", 1)
        trade = tracked_trade(symbol)

        if not trade:
            continue

        try:
            current_price = get_ticker_price(symbol)
            buy_price = as_decimal(trade["buy_price"], "buy_price")
            quantity = as_decimal(trade["executed_qty"], "executed_qty")
            buy_value = as_decimal(trade["buy_value"], "buy_value")
            current_value = current_price * quantity
            pnl_value = current_value - buy_value
            pnl_percent = (
                pnl_value / buy_value * Decimal("100")
                if buy_value > 0
                else Decimal("0")
            )

            result.append({
                **trade,
                "current_price": decimal_str(current_price),
                "current_value": decimal_str(current_value),
                "pnl": decimal_str(pnl_value),
                "pnl_pct": decimal_str(pnl_percent),
                "price_change_pct": decimal_str(
                    (current_price - buy_price)
                    / buy_price
                    * Decimal("100")
                ),
            })

        except Exception as exc:
            result.append({
                "symbol": symbol,
                "error": str(exc),
            })

    return jsonify({"result": result}), 200


@app.errorhandler(Exception)
def unhandled_error(error):
    logger.exception("Unhandled server error")
    return jsonify({
        "status": "server_error",
        "error": str(error),
    }), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False,
    )
