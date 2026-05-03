from flask import Flask, request, jsonify
import hashlib
import hmac
import time
import requests
import json
import os
import traceback

app = Flask(__name__)

# ============================================================
# הגדרות — נקראות מ-Environment Variables
# ============================================================
BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "wyckoff2024")
BYBIT_BASE_URL   = "https://api.bybit.com"

# ============================================================
# פונקציות עזר לחתימת Bybit API
# ============================================================
def sign_request(params: dict, timestamp: str) -> str:
    param_str = timestamp + BYBIT_API_KEY + "5000" + json.dumps(params, separators=(',', ':'))
    signature = hmac.new(
        BYBIT_API_SECRET.encode("utf-8"),
        param_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return signature

def bybit_request(method: str, endpoint: str, params: dict) -> dict:
    timestamp = str(int(time.time() * 1000))
    signature = sign_request(params, timestamp)
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json"
    }
    url = BYBIT_BASE_URL + endpoint
    if method == "POST":
        resp = requests.post(url, headers=headers, json=params, timeout=10)
    else:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
    return resp.json()

# ============================================================
# פתיחת פוזיציה
# ============================================================
def place_order(symbol: str, side: str, qty: float,
                stop_loss: float = None, take_profit: float = None,
                order_type: str = "Market", price: float = None) -> dict:
    params = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": order_type,
        "qty": str(qty),
        "timeInForce": "GTC" if order_type == "Limit" else "IOC",
        "reduceOnly": False,
        "closeOnTrigger": False,
        "positionIdx": 0
    }
    if price and order_type == "Limit":
        params["price"] = str(price)
    if stop_loss:
        params["stopLoss"] = str(stop_loss)
    if take_profit:
        params["takeProfit"] = str(take_profit)

    return bybit_request("POST", "/v5/order/create", params)

# ============================================================
# סגירת פוזיציה (Market Close)
# ============================================================
def close_position(symbol: str, side: str, qty: float) -> dict:
    close_side = "Sell" if side == "long" else "Buy"
    params = {
        "category": "linear",
        "symbol": symbol,
        "side": close_side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
        "reduceOnly": True,
        "closeOnTrigger": False,
        "positionIdx": 0
    }
    return bybit_request("POST", "/v5/order/create", params)

# ============================================================
# ביטול כל הפקודות הפתוחות
# ============================================================
def cancel_all_orders(symbol: str) -> dict:
    params = {
        "category": "linear",
        "symbol": symbol
    }
    return bybit_request("POST", "/v5/order/cancel-all", params)

# ============================================================
# Webhook Endpoint
# ============================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        # בדיקת סיסמה
        secret = request.args.get("secret", "")
        if secret != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "No JSON data"}), 400

        print(f"[WEBHOOK] Received: {data}")

        action    = data.get("action", "").lower()
        sentiment = data.get("sentiment", "").lower()
        symbol    = data.get("ticker", "ETHUSDT").replace("-", "")
        qty       = float(data.get("quantity", 1))
        sl        = data.get("stopLoss")
        tp        = data.get("takeProfit")
        price     = data.get("price")

        result = {}

        # ---- כניסת LONG ----
        if action == "buy" and sentiment == "long":
            result = place_order(
                symbol=symbol,
                side="Buy",
                qty=qty,
                stop_loss=float(sl) if sl else None,
                take_profit=float(tp) if tp else None
            )

        # ---- כניסת SHORT ----
        elif action == "sell" and sentiment == "short":
            result = place_order(
                symbol=symbol,
                side="Sell",
                qty=qty,
                stop_loss=float(sl) if sl else None,
                take_profit=float(tp) if tp else None
            )

        # ---- יציאה (סגירת פוזיציה) ----
        elif action == "exit" or sentiment == "flat":
            result = close_position(symbol=symbol, side=sentiment, qty=qty)

        # ---- ביטול כל הפקודות ----
        elif action == "cancel":
            result = cancel_all_orders(symbol=symbol)

        # ---- Limit TP (Strategy B) ----
        elif action == "limit_tp":
            limit_side = "Sell" if sentiment == "long" else "Buy"
            tp_price = float(tp) if tp else (float(price) if price else None)
            if tp_price is None:
                return jsonify({"error": "Missing takeProfit or price for limit_tp"}), 400
            result = place_order(
                symbol=symbol,
                side=limit_side,
                qty=qty,
                order_type="Limit",
                price=tp_price,
            )

        else:
            return jsonify({"error": f"Unknown action: {action}", "sentiment": sentiment}), 400

        print(f"[BYBIT] Response: {result}")
        return jsonify(result)

    except Exception as e:
        error_msg = traceback.format_exc()
        print(f"[ERROR] {error_msg}")
        return jsonify({"error": str(e), "traceback": error_msg}), 500

# ============================================================
# Health Check
# ============================================================
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "message": "Bybit Webhook Server Running",
        "api_key_set": bool(BYBIT_API_KEY),
        "secret_set": bool(BYBIT_API_SECRET)
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
