from flask import Flask, request, jsonify
import hashlib
import hmac
import time
import requests
import json
import os
import traceback
import threading

app = Flask(__name__)

BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "wyckoff2024")
BYBIT_BASE_URL   = "https://api.bybit.com"
RENDER_URL       = os.environ.get("RENDER_URL", "https://bybit-webhook-l0y4.onrender.com")

# ══════════════════════════════════════════════
# Trailing Stop הגדרות
# ══════════════════════════════════════════════
TRAIL_TRIGGER = 10.0   # כמה נקודות רווח לפני שהטריילינג מתחיל
TRAIL_OFFSET  = 100.0  # כמה נקודות מאחורי הגבוה/נמוך ביותר ה-SL נשאר
TRAIL_CHECK_INTERVAL = 30  # בדיקה כל 30 שניות

# מילון לשמירת מצב הטריילינג לכל סימבול
# { "BTCPERP": { "side": "Buy", "entry": 80000, "best_price": 80000, "sl": 79993, "active": True } }
trailing_state = {}
trailing_lock = threading.Lock()

# ══════════════════════════════════════════════
# Keep-Alive — מונע שינה של Render Free Tier
# ══════════════════════════════════════════════
def keep_alive():
    """שולח ping לעצמו כל 8 דקות כדי שהשרת לא יירדם"""
    while True:
        time.sleep(480)  # 8 דקות
        try:
            requests.get(RENDER_URL + "/ping", timeout=5)
            print("[KEEP-ALIVE] Ping sent")
        except Exception as e:
            print(f"[KEEP-ALIVE] Failed: {e}")

# ══════════════════════════════════════════════
# Bybit API
# ══════════════════════════════════════════════
def bybit_get(endpoint: str, params: dict) -> dict:
    """שולח GET ל-Bybit עם חתימה"""
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    query_str = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    sign_str = timestamp + BYBIT_API_KEY + recv_window + query_str
    signature = hmac.new(
        BYBIT_API_SECRET.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": recv_window,
    }
    url = BYBIT_BASE_URL + endpoint + "?" + query_str
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        try:
            return resp.json()
        except Exception:
            return {"error": "Non-JSON response", "raw": resp.text[:500]}
    except Exception as e:
        return {"error": f"Request failed: {str(e)}"}

def bybit_post(endpoint: str, params: dict) -> dict:
    """שולח POST ל-Bybit עם חתימה נכונה"""
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    body_str = json.dumps(params, separators=(',', ':'))
    sign_str = timestamp + BYBIT_API_KEY + recv_window + body_str
    signature = hmac.new(
        BYBIT_API_SECRET.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type": "application/json"
    }
    url = BYBIT_BASE_URL + endpoint
    try:
        resp = requests.post(url, headers=headers, data=body_str, timeout=10)
        try:
            return resp.json()
        except Exception:
            return {"error": "Non-JSON response", "raw": resp.text[:500], "status_code": resp.status_code}
    except Exception as e:
        return {"error": f"Request failed: {str(e)}"}

def get_position(symbol: str) -> dict:
    """בודק אם יש פוזיציה פתוחה ב-symbol"""
    result = bybit_get("/v5/position/list", {
        "category": "linear",
        "symbol": symbol
    })
    try:
        positions = result.get("result", {}).get("list", [])
        for pos in positions:
            size = float(pos.get("size", 0))
            if size > 0:
                return {
                    "has_position": True,
                    "side": pos.get("side"),
                    "size": size,
                    "entry_price": float(pos.get("avgPrice", 0)),
                    "mark_price": float(pos.get("markPrice", 0)),
                    "stop_loss": float(pos.get("stopLoss", 0)) if pos.get("stopLoss") else None
                }
        return {"has_position": False}
    except Exception:
        return {"has_position": False, "error": str(result)}

def get_mark_price(symbol: str) -> float:
    """מחזיר את המחיר הנוכחי של הסימבול"""
    result = bybit_get("/v5/market/tickers", {
        "category": "linear",
        "symbol": symbol
    })
    try:
        items = result.get("result", {}).get("list", [])
        if items:
            return float(items[0].get("markPrice", 0))
    except Exception:
        pass
    return 0.0

def update_stop_loss(symbol: str, new_sl: float) -> dict:
    """מעדכן SL על פוזיציה קיימת ב-Bybit"""
    params = {
        "category": "linear",
        "symbol": symbol,
        "stopLoss": str(round(new_sl, 2)),
        "positionIdx": 0,
        "slTriggerBy": "MarkPrice"
    }
    return bybit_post("/v5/position/trading-stop", params)

def place_order(symbol, side, qty, stop_loss=None, take_profit=None, order_type="Market", price=None):
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
    return bybit_post("/v5/order/create", params)

def close_position(symbol, side, qty):
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
    return bybit_post("/v5/order/create", params)

def cancel_all_orders(symbol):
    params = {"category": "linear", "symbol": symbol}
    return bybit_post("/v5/order/cancel-all", params)

# ══════════════════════════════════════════════
# Trailing Stop Thread — עוקב אחרי פוזיציות ומעדכן SL
# ══════════════════════════════════════════════
def trailing_stop_worker():
    """
    רץ ברקע כל 30 שניות.
    לכל סימבול עם trailing פעיל:
      - מביא מחיר נוכחי
      - אם Long: אם מחיר > entry + TRAIL_TRIGGER → עדכן SL = best_price - TRAIL_OFFSET
      - אם Short: אם מחיר < entry - TRAIL_TRIGGER → עדכן SL = best_price + TRAIL_OFFSET
      - SL רק מתקדם, אף פעם לא חוזר אחורה
    """
    print("[TRAIL] Trailing Stop Worker started")
    while True:
        time.sleep(TRAIL_CHECK_INTERVAL)
        with trailing_lock:
            symbols = list(trailing_state.keys())

        for symbol in symbols:
            try:
                with trailing_lock:
                    state = trailing_state.get(symbol)
                    if not state or not state.get("active"):
                        continue

                # בדוק שהפוזיציה עדיין קיימת
                pos = get_position(symbol)
                if not pos.get("has_position"):
                    print(f"[TRAIL] {symbol}: Position closed — removing trailing state")
                    with trailing_lock:
                        trailing_state.pop(symbol, None)
                    continue

                mark_price = get_mark_price(symbol)
                if mark_price == 0:
                    continue

                with trailing_lock:
                    state = trailing_state.get(symbol)
                    if not state:
                        continue

                    side       = state["side"]      # "Buy" or "Sell"
                    entry      = state["entry"]
                    best_price = state["best_price"]
                    current_sl = state["sl"]

                    if side == "Buy":
                        # Long: עדכן best_price אם המחיר עלה
                        if mark_price > best_price:
                            state["best_price"] = mark_price
                            best_price = mark_price

                        # בדוק אם הטריילינג צריך להתחיל
                        profit_pts = best_price - entry
                        if profit_pts >= TRAIL_TRIGGER:
                            new_sl = round(best_price - TRAIL_OFFSET, 2)
                            # SL רק עולה — אף פעם לא יורד
                            if new_sl > current_sl:
                                state["sl"] = new_sl
                                print(f"[TRAIL] {symbol} LONG: price={mark_price:.2f}, best={best_price:.2f}, new SL={new_sl:.2f} (was {current_sl:.2f})")
                                result = update_stop_loss(symbol, new_sl)
                                print(f"[TRAIL] Update SL result: {result}")

                    elif side == "Sell":
                        # Short: עדכן best_price אם המחיר ירד
                        if mark_price < best_price:
                            state["best_price"] = mark_price
                            best_price = mark_price

                        # בדוק אם הטריילינג צריך להתחיל
                        profit_pts = entry - best_price
                        if profit_pts >= TRAIL_TRIGGER:
                            new_sl = round(best_price + TRAIL_OFFSET, 2)
                            # SL רק יורד — אף פעם לא עולה
                            if new_sl < current_sl:
                                state["sl"] = new_sl
                                print(f"[TRAIL] {symbol} SHORT: price={mark_price:.2f}, best={best_price:.2f}, new SL={new_sl:.2f} (was {current_sl:.2f})")
                                result = update_stop_loss(symbol, new_sl)
                                print(f"[TRAIL] Update SL result: {result}")

            except Exception as e:
                print(f"[TRAIL] Error processing {symbol}: {e}")

# הפעלת threads
keep_alive_thread = threading.Thread(target=keep_alive, daemon=True)
keep_alive_thread.start()

trailing_thread = threading.Thread(target=trailing_stop_worker, daemon=True)
trailing_thread.start()

# ══════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "alive", "time": int(time.time())})

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        secret = request.args.get("secret", "")
        if secret != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

        data = request.get_json(force=True, silent=True)
        if not data:
            raw_body = request.get_data(as_text=True)
            print(f"[WEBHOOK] Bad JSON: {raw_body[:200]}")
            return jsonify({"error": "No valid JSON", "raw": raw_body[:200]}), 400

        print(f"[WEBHOOK] Received: {data}")

        action    = data.get("action", "").lower()
        sentiment = data.get("sentiment", "").lower()
        symbol    = data.get("ticker", "BTCPERP").replace("-", "")
        qty       = float(data.get("quantity", 1))
        sl        = data.get("stopLoss")
        tp        = data.get("takeProfit")
        price     = data.get("price")

        # ══════════════════════════════════════
        # הגנת כפל פוזיציות — בדיקה לפני כניסה
        # ══════════════════════════════════════
        if action in ("buy", "sell"):
            pos = get_position(symbol)
            print(f"[POSITION CHECK] {symbol}: {pos}")
            if pos.get("has_position"):
                existing_side = pos.get("side", "").lower()
                # אם כבר יש פוזיציה באותו כיוון — דלג
                if (action == "buy" and existing_side == "buy") or \
                   (action == "sell" and existing_side == "sell"):
                    msg = f"Position already open ({existing_side} {pos.get('size')} BTC) — skipping duplicate entry"
                    print(f"[SKIP] {msg}")
                    return jsonify({"status": "skipped", "reason": msg})
                # אם יש פוזיציה בכיוון הפוך — סגור אותה קודם
                else:
                    print(f"[FLIP] Closing opposite position before new entry")
                    close_result = close_position(symbol, existing_side, pos.get("size"))
                    print(f"[FLIP] Close result: {close_result}")
                    # נקה trailing state של הפוזיציה הישנה
                    with trailing_lock:
                        trailing_state.pop(symbol, None)
                    time.sleep(0.5)

        if action == "buy" and sentiment == "long":
            result = place_order(symbol, "Buy", qty,
                                 stop_loss=float(sl) if sl else None,
                                 take_profit=float(tp) if tp else None)
            # הפעל Trailing לפוזיציה החדשה
            if result.get("retCode") == 0 or result.get("result"):
                entry_price = get_mark_price(symbol)
                initial_sl  = float(sl) if sl else (entry_price - TRAIL_OFFSET)
                with trailing_lock:
                    trailing_state[symbol] = {
                        "side":       "Buy",
                        "entry":      entry_price,
                        "best_price": entry_price,
                        "sl":         initial_sl,
                        "active":     True
                    }
                print(f"[TRAIL] Started trailing for {symbol} LONG @ {entry_price:.2f}, initial SL={initial_sl:.2f}")

        elif action == "sell" and sentiment == "short":
            result = place_order(symbol, "Sell", qty,
                                 stop_loss=float(sl) if sl else None,
                                 take_profit=float(tp) if tp else None)
            # הפעל Trailing לפוזיציה החדשה
            if result.get("retCode") == 0 or result.get("result"):
                entry_price = get_mark_price(symbol)
                initial_sl  = float(sl) if sl else (entry_price + TRAIL_OFFSET)
                with trailing_lock:
                    trailing_state[symbol] = {
                        "side":       "Sell",
                        "entry":      entry_price,
                        "best_price": entry_price,
                        "sl":         initial_sl,
                        "active":     True
                    }
                print(f"[TRAIL] Started trailing for {symbol} SHORT @ {entry_price:.2f}, initial SL={initial_sl:.2f}")

        elif action == "exit" or sentiment == "flat":
            result = close_position(symbol, sentiment, qty)
            # נקה trailing state
            with trailing_lock:
                trailing_state.pop(symbol, None)
            print(f"[TRAIL] Cleared trailing state for {symbol}")

        elif action == "cancel":
            result = cancel_all_orders(symbol)

        elif action == "limit_tp":
            limit_side = "Sell" if sentiment == "long" else "Buy"
            tp_price = float(tp) if tp else (float(price) if price else None)
            if not tp_price:
                return jsonify({"error": "Missing takeProfit"}), 400
            result = place_order(symbol, limit_side, qty, order_type="Limit", price=tp_price)

        else:
            return jsonify({"error": f"Unknown action: {action}"}), 400

        print(f"[BYBIT] Response: {result}")
        return jsonify(result)

    except Exception as e:
        err = traceback.format_exc()
        print(f"[ERROR] {err}")
        return jsonify({"error": str(e), "traceback": err}), 500

@app.route("/trail_status", methods=["GET"])
def trail_status():
    """מציג את מצב הטריילינג הנוכחי"""
    with trailing_lock:
        return jsonify({
            "trailing_state": trailing_state,
            "trail_trigger": TRAIL_TRIGGER,
            "trail_offset": TRAIL_OFFSET
        })

@app.route("/", methods=["GET"])
def health():
    with trailing_lock:
        active_trails = list(trailing_state.keys())
    return jsonify({
        "status": "ok",
        "message": "Bybit Webhook Server Running — Trailing Stop Active",
        "api_key_set": bool(BYBIT_API_KEY),
        "secret_set": bool(BYBIT_API_SECRET),
        "keep_alive": "active",
        "trailing_stop": "active",
        "trail_trigger_pts": TRAIL_TRIGGER,
        "trail_offset_pts": TRAIL_OFFSET,
        "active_trails": active_trails
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
