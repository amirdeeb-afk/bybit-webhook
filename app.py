from flask import Flask, request, jsonify
import hashlib
import hmac
import time
import requests
import json
import os

app = Flask(__name__)

# ============================================================
# הגדרות — יש למלא את הפרטים שלך
# ============================================================
BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "wyckoff2024")  # סיסמה סודית ל-Webhook
BYBIT_BASE_URL   = "https://api.bybit.com"

# ============================================================
# פונקציות עזר לחתימת Bybit API
# ============================================================
def sign_request(params: dict, timestamp: str) -> str:
    param_str = timestamp + BYBIT_API_KEY + "5000" + json.dumps(params, separators=(',', ':'))
    return hmac.new(BYBIT_API_SECRET.encode("utf-8"), param_str.encode("utf-8"), hashlib.sha256).hexdigest()

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
        resp = requests.post(url, headers=headers, json=params)
    else:
        resp = requests.get(url, headers=headers, params=params)
    return resp.json()

# ============================================================
# פתיחת פוזיציה
# ============================================================
def place_order(symbol: str, side: str, qty: float,
                stop_loss: float = None, take_profit: float = None,
                order_type: str = "Market", price: float = None) -> dict:
    params = {
        "category": "linear",          # USDT Perpetual
        "symbol": symbol,
        "side": side,                   # "Buy" או "Sell"
        "orderType": order_type,        # "Market" או "Limit"
        "qty": str(qty),
        "timeInForce": "GTC" if order_type == "Limit" else "IOC",
        "reduceOnly": False,
        "closeOnTrigger": False,
        "positionIdx": 0               # One-Way Mode
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
    # כדי לסגור Long → Sell; כדי לסגור Short → Buy
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
# Webhook Endpoint — מקבל סיגנלים מ-TradingView
# ============================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    # בדיקת סיסמה
    secret = request.args.get("secret", "")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON data"}), 400

    print(f"[WEBHOOK] Received: {data}")

    action    = data.get("action", "").lower()    # buy / sell / exit / cancel
    sentiment = data.get("sentiment", "").lower() # long / short / flat
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
        result = place_order(
            symbol=symbol,
            side=limit_side,
            qty=qty,
            order_type="Limit",
            price=float(tp) if tp else float(price),
        )

    print(f"[BYBIT] Response: {result}")
    return jsonify(result)

# ============================================================
# Health Check
# ============================================================
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Bybit Webhook Server Running"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
