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
# TRAIL_ACTIVATION = 222 נקודות רווח מהכניסה — רק אז מגדירים trailing stop
# TRAIL_DISTANCE   = 22  נקודות מהשיא — מרחק הטריילינג
TRAIL_ACTIVATION     = 222.0   # נקודות רווח לפני הפעלת trailing
TRAIL_DISTANCE       = 22.0    # נקודות מרחק מהשיא
TRAIL_CHECK_INTERVAL = 30      # בדיקה כל 30 שניות

# ══════════════════════════════════════════════
# pending_trail — פוזיציות שממתינות להפעלת trailing
# { "BTCPERP": { "side": "Buy"/"Sell", "entry": 80000.0, "activation_target": 80222.0 } }
# ══════════════════════════════════════════════
pending_trail = {}
pending_trail_lock = threading.Lock()

# ══════════════════════════════════════════════
# Cooldown — מניעת סגירה מיידית של פוזיציה חדשה
# ══════════════════════════════════════════════
COOLDOWN_SECONDS = 30
position_open_time = {}

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

def set_native_trailing_stop(symbol: str, trailing_stop: float) -> dict:
    """
    מגדיר Trailing Stop מובנה של Bybit — ללא activePrice.
    כשמגדירים ללא activePrice, Bybit מפעיל את הטריילינג מיד מהמחיר הנוכחי.
    אנחנו קוראים לפונקציה זו רק כשהמחיר כבר הגיע ל-Entry+222,
    כך שהטריילינג מופעל בנקודה הנכונה.
    """
    params = {
        "category": "linear",
        "symbol": symbol,
        "tpslMode": "Full",
        "trailingStop": str(round(trailing_stop, 2)),
        "positionIdx": 0
    }
    result = bybit_post("/v5/position/trading-stop", params)
    print(f"[TRAIL-API] Params sent: {params}")
    print(f"[TRAIL-API] Response: {result}")
    return result

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
# Activation Monitor Thread
# ══════════════════════════════════════════════
def activation_monitor():
    """
    Thread רץ ברקע כל TRAIL_CHECK_INTERVAL שניות.
    בודק כל פוזיציה ב-pending_trail:
      - אם המחיר הגיע ל-activation_target → מגדיר trailing stop (distance=22, ללא activePrice)
      - אם הפוזיציה נסגרה → מסיר מהרשימה
    """
    print("[ACTIVATION-MONITOR] Thread started")
    while True:
        time.sleep(TRAIL_CHECK_INTERVAL)
        try:
            with pending_trail_lock:
                symbols = list(pending_trail.keys())

            for symbol in symbols:
                try:
                    with pending_trail_lock:
                        state = pending_trail.get(symbol)
                    if not state:
                        continue

                    side             = state["side"]
                    entry            = state["entry"]
                    activation_target = state["activation_target"]

                    # בדוק אם הפוזיציה עדיין פתוחה
                    pos = get_position(symbol)
                    if not pos.get("has_position"):
                        print(f"[ACTIVATION-MONITOR] {symbol}: position closed, removing from pending")
                        with pending_trail_lock:
                            pending_trail.pop(symbol, None)
                        continue

                    # בדוק מחיר נוכחי
                    mark_price = get_mark_price(symbol)
                    if mark_price <= 0:
                        print(f"[ACTIVATION-MONITOR] {symbol}: could not get mark price, skipping")
                        continue

                    # בדוק אם הגענו ל-activation target
                    if side == "Buy":
                        reached = mark_price >= activation_target
                    else:  # Sell / Short
                        reached = mark_price <= activation_target

                    print(f"[ACTIVATION-MONITOR] {symbol} {side}: mark={mark_price:.2f}, "
                          f"activation_target={activation_target:.2f}, reached={reached}")

                    if reached:
                        print(f"[ACTIVATION-MONITOR] ✅ {symbol}: activation target reached! "
                              f"Setting trailing stop distance={TRAIL_DISTANCE}")
                        ts_result = set_native_trailing_stop(symbol, TRAIL_DISTANCE)
                        print(f"[ACTIVATION-MONITOR] Trail set result: {ts_result}")
                        if ts_result.get("retCode") == 0:
                            print(f"[ACTIVATION-MONITOR] ✅ Trailing stop set successfully for {symbol}")
                            with pending_trail_lock:
                                pending_trail.pop(symbol, None)
                        else:
                            print(f"[ACTIVATION-MONITOR] ❌ Failed to set trailing stop: {ts_result}")
                            # נשאר ב-pending ונסה שוב בסיבוב הבא

                except Exception as e:
                    print(f"[ACTIVATION-MONITOR] Error processing {symbol}: {e}")
                    print(traceback.format_exc())

        except Exception as e:
            print(f"[ACTIVATION-MONITOR] Outer error: {e}")
            print(traceback.format_exc())

# הפעלת threads
keep_alive_thread = threading.Thread(target=keep_alive, daemon=True)
keep_alive_thread.start()

activation_monitor_thread = threading.Thread(target=activation_monitor, daemon=True)
activation_monitor_thread.start()

# ══════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════
@app.route("/ping", methods=["GET"])
def ping():
    """Keepalive endpoint — called every 14 minutes by external cron to prevent Render sleep"""
    return jsonify({"status": "pong", "time": time.time()})

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

        # ══════════════════════════════════════════════════════
        # בדיקת פוזיציה פתוחה — אם יש פוזיציה, מתעלמים מהסיגנל
        # ══════════════════════════════════════════════════════
        if action in ("buy", "sell"):
            pos = get_position(symbol)
            print(f"[POSITION CHECK] {symbol}: {pos}")
            if pos.get("has_position"):
                existing_side = pos.get("side", "")
                msg = f"Position already open ({existing_side}) — ignoring new {action} signal. Bybit trailing will manage exit."
                print(f"[SKIP] {msg}")
                return jsonify({"status": "skipped", "reason": msg})
            else:
                print(f"[NEW ENTRY] No existing position, proceeding with fresh entry")

        if action == "buy" and sentiment == "long":
            result = place_order(symbol, "Buy", qty,
                                 stop_loss=float(sl) if sl else None,
                                 take_profit=float(tp) if tp else None)
            print(f"[ORDER] LONG result: {result}")
            ret_code = result.get("retCode", result.get("ret_code", -1))
            order_ok = str(ret_code) == "0"
            if order_ok:
                position_open_time[symbol] = time.time()
                # קבל מחיר כניסה אמיתי מהפוזיציה (המתן קצת שהפקודה תתמלא)
                time.sleep(2)
                pos_info = get_position(symbol)
                if pos_info.get("has_position"):
                    entry_price = pos_info.get("entry_price", 0)
                else:
                    entry_price = get_mark_price(symbol)

                if entry_price > 0:
                    activation_target = round(entry_price + TRAIL_ACTIVATION, 2)
                    with pending_trail_lock:
                        pending_trail[symbol] = {
                            "side": "Buy",
                            "entry": entry_price,
                            "activation_target": activation_target
                        }
                    print(f"[TRAIL] LONG registered for monitoring: entry={entry_price:.2f}, "
                          f"activation_target={activation_target:.2f} (entry+{TRAIL_ACTIVATION})")
                else:
                    print(f"[TRAIL] LONG: could not get entry price — trailing NOT registered")
            else:
                print(f"[ORDER] LONG order failed or retCode not 0: {result}")

        elif action == "sell" and sentiment == "short":
            result = place_order(symbol, "Sell", qty,
                                 stop_loss=float(sl) if sl else None,
                                 take_profit=float(tp) if tp else None)
            print(f"[ORDER] SHORT result: {result}")
            ret_code = result.get("retCode", result.get("ret_code", -1))
            order_ok = str(ret_code) == "0"
            if order_ok:
                position_open_time[symbol] = time.time()
                # קבל מחיר כניסה אמיתי מהפוזיציה
                time.sleep(2)
                pos_info = get_position(symbol)
                if pos_info.get("has_position"):
                    entry_price = pos_info.get("entry_price", 0)
                else:
                    entry_price = get_mark_price(symbol)

                if entry_price > 0:
                    activation_target = round(entry_price - TRAIL_ACTIVATION, 2)
                    with pending_trail_lock:
                        pending_trail[symbol] = {
                            "side": "Sell",
                            "entry": entry_price,
                            "activation_target": activation_target
                        }
                    print(f"[TRAIL] SHORT registered for monitoring: entry={entry_price:.2f}, "
                          f"activation_target={activation_target:.2f} (entry-{TRAIL_ACTIVATION})")
                else:
                    print(f"[TRAIL] SHORT: could not get entry price — trailing NOT registered")
            else:
                print(f"[ORDER] SHORT order failed or retCode not 0: {result}")

        elif action == "exit" or sentiment == "flat":
            # ⛔ Bybit manages all exits via Trailing Stop — ignore XL/exit signals from TradingView
            msg = f"Exit signal ignored — Bybit trailing stop manages position closing (action={action}, sentiment={sentiment})"
            print(f"[SKIP] {msg}")
            return jsonify({"status": "skipped", "reason": msg})

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
    with pending_trail_lock:
        return jsonify({
            "pending_trail": pending_trail,
            "trail_activation_pts": TRAIL_ACTIVATION,
            "trail_distance_pts": TRAIL_DISTANCE
        })

@app.route("/trail_inject", methods=["GET"])
def trail_inject():
    """
    הזרקה ידנית של פוזיציה לניטור trailing.
    פרמטרים: symbol, side (Buy/Sell), entry
    דוגמה: /trail_inject?symbol=BTCPERP&side=Buy&entry=79094.30
    """
    try:
        symbol = request.args.get("symbol", "BTCPERP")
        side   = request.args.get("side", "Buy")
        entry  = float(request.args.get("entry", 0))

        if entry == 0:
            return jsonify({"error": "Missing entry price"}), 400

        if side == "Buy":
            activation_target = round(entry + TRAIL_ACTIVATION, 2)
        else:
            activation_target = round(entry - TRAIL_ACTIVATION, 2)

        with pending_trail_lock:
            pending_trail[symbol] = {
                "side": side,
                "entry": entry,
                "activation_target": activation_target
            }

        print(f"[INJECT] {symbol} {side} @ {entry:.2f}, activation_target={activation_target:.2f}")
        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "activation_target": activation_target,
            "message": f"Position injected. Trailing will be set when price reaches {activation_target:.2f} ({TRAIL_ACTIVATION} pts from entry)"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/trail_set_now", methods=["GET"])
def trail_set_now():
    """
    מגדיר trailing stop מיד על הפוזיציה הפתוחה (ללא המתנה לactivation target).
    שימושי לבדיקה ידנית.
    פרמטר: symbol (ברירת מחדל: BTCPERP)
    """
    try:
        symbol = request.args.get("symbol", "BTCPERP")
        pos = get_position(symbol)
        if not pos.get("has_position"):
            return jsonify({"error": f"No open position for {symbol}"}), 400

        result = set_native_trailing_stop(symbol, TRAIL_DISTANCE)
        # הסר מניטור אם קיים
        with pending_trail_lock:
            pending_trail.pop(symbol, None)

        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "trail_distance": TRAIL_DISTANCE,
            "bybit_result": result
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/positions_debug", methods=["GET"])
def positions_debug():
    """
    מחזיר את כל הפוזיציות הפתוחות מ-Bybit API (raw).
    """
    try:
        result_all = bybit_get("/v5/position/list", {
            "category": "linear",
            "settleCoin": "USDC"
        })
        result_perp = bybit_get("/v5/position/list", {
            "category": "linear",
            "symbol": "BTCPERP"
        })
        all_positions = result_all.get("result", {}).get("list", [])
        open_positions = [p for p in all_positions if float(p.get("size", 0)) > 0]
        return jsonify({
            "status": "ok",
            "open_positions_usdc_settle": open_positions,
            "btcperp_direct": result_perp,
            "all_count": len(all_positions)
        })
    except Exception as e:
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500

@app.route("/", methods=["GET"])
def health():
    with pending_trail_lock:
        pending = dict(pending_trail)
    return jsonify({
        "status": "ok",
        "message": "Bybit Webhook Server Running — Server-Side Activation Monitor Active",
        "api_key_set": bool(BYBIT_API_KEY),
        "secret_set": bool(BYBIT_API_SECRET),
        "keep_alive": "active",
        "activation_monitor": "active",
        "trail_activation_pts": TRAIL_ACTIVATION,
        "trail_distance_pts": TRAIL_DISTANCE,
        "check_interval_sec": TRAIL_CHECK_INTERVAL,
        "pending_trail": pending
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
