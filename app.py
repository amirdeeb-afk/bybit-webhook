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
# V16 — ערכים תואמים בדיוק ל-Pine Script V16 (trail_pts=222, trail_off=11)
# TradingView: trail_points=222 = ה-SL נשמר 222 נקודות מאחורי השיא
# Bybit: trailingStop=222 = offset מהשיא, activePrice = מחיר כניסה (מיידי)
TRAIL_TRIGGER = 0.0    # 0 = הטריילינג מתחיל מיד עם הכניסה (כמו TradingView trail_points)
TRAIL_OFFSET  = 222.0  # 222 נקודות מאחורי השיא — תואם ל-trail_points=222 ב-TradingView
TRAIL_CHECK_INTERVAL = 30  # בדיקה כל 30 שניות

# מילון לשמירת מצב הטריילינג לכל סימבול
# { "BTCPERP": { "side": "Buy", "entry": 80000, "best_price": 80000, "sl": 79993, "active": True } }
trailing_state = {}
trailing_lock = threading.Lock()

# ══════════════════════════════════════════════
# Cooldown — מניעת סגירה מיידית של פוזיציה חדשה
# ══════════════════════════════════════════════
COOLDOWN_SECONDS = 30  # לא לסגור פוזיציה שנפתחה לפני פחות מ-30 שניות
position_open_time = {}  # { "BTCPERP": timestamp }

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

def set_native_trailing_stop(symbol: str, trailing_stop: float, active_price: float) -> dict:
    """מגדיר Trailing Stop מובנה של Bybit"""
    params = {
        "category": "linear",
        "symbol": symbol,
        "tpslMode": "Full",          # שדה חובה לפי תיעוד Bybit
        "trailingStop": str(round(trailing_stop, 2)),
        "activePrice": str(round(active_price, 2)),
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

# הפעלת threads
keep_alive_thread = threading.Thread(target=keep_alive, daemon=True)
keep_alive_thread.start()

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

        # ══════════════════════════════════════════════════════
        # בדיקת פוזיציה פתוחה — אם יש פוזיציה, מתעלמים מהסיגנל
        # ══════════════════════════════════════════════════════
        # V16 ואילך: הפוזיציה נסגרת רק ע"י Trailing Stop של Bybit.
        # אם מגיע סיגנל כניסה חדש בזמן שיש פוזיציה פתוחה — מתעלמים ממנו.
        # זה מונע: (1) כניסה כפולה באותו כיוון, (2) סגירה+פתיחה בכיוון הפוך.
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
            # הפעל Trailing מובנה של Bybit — בדיקה גמישה של retCode
            ret_code = result.get("retCode", result.get("ret_code", -1))
            order_ok = str(ret_code) == "0"
            if order_ok:
                position_open_time[symbol] = time.time()
                time.sleep(1)  # המתן שניה אחת לפני שליחת Trailing
                current_price = get_mark_price(symbol)
                # activePrice = מחיר נוכחי (לא מחיר כניסה) כדי ש-Bybit יקבל את הבקשה
                active_price = current_price if current_price > 0 else float(sl) + TRAIL_OFFSET if sl else 0
                ts_result = set_native_trailing_stop(symbol, TRAIL_OFFSET, active_price)
                print(f"[TRAIL] LONG trailing stop sent: offset={TRAIL_OFFSET}, active_price={active_price:.2f}. Result: {ts_result}")
            else:
                print(f"[ORDER] LONG order failed or retCode not 0: {result}")

        elif action == "sell" and sentiment == "short":
            result = place_order(symbol, "Sell", qty,
                                 stop_loss=float(sl) if sl else None,
                                 take_profit=float(tp) if tp else None)
            print(f"[ORDER] SHORT result: {result}")
            # הפעל Trailing מובנה של Bybit — בדיקה גמישה של retCode
            ret_code = result.get("retCode", result.get("ret_code", -1))
            order_ok = str(ret_code) == "0"
            if order_ok:
                position_open_time[symbol] = time.time()
                time.sleep(1)  # המתן שניה אחת לפני שליחת Trailing
                current_price = get_mark_price(symbol)
                # activePrice = מחיר נוכחי (לא מחיר כניסה) כדי ש-Bybit יקבל את הבקשה
                active_price = current_price if current_price > 0 else float(sl) - TRAIL_OFFSET if sl else 0
                ts_result = set_native_trailing_stop(symbol, TRAIL_OFFSET, active_price)
                print(f"[TRAIL] SHORT trailing stop sent: offset={TRAIL_OFFSET}, active_price={active_price:.2f}. Result: {ts_result}")
            else:
                print(f"[ORDER] SHORT order failed or retCode not 0: {result}")

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

@app.route("/trail_sync", methods=["GET"])
def trail_sync():
    """
    סורק את כל הפוזיציות הפתוחות ב-Bybit ומזריק אותן לטריילינג אוטומטית.
    שימושי כשפוזיציה נפתחה לפני שהשרת הופעל.
    """
    synced = []
    skipped = []
    try:
        result = bybit_get("/v5/position/list", {"category": "linear", "settleCoin": "USDC"})
        positions = result.get("result", {}).get("list", [])
        for pos in positions:
            size = float(pos.get("size", 0))
            if size == 0:
                continue
            symbol     = pos.get("symbol")
            side       = pos.get("side")          # "Buy" or "Sell"
            entry      = float(pos.get("avgPrice", 0))
            mark_price = float(pos.get("markPrice", 0))
            current_sl = float(pos.get("stopLoss", 0)) if pos.get("stopLoss") else 0

            with trailing_lock:
                if symbol in trailing_state:
                    skipped.append({"symbol": symbol, "reason": "already tracked"})
                    continue
                # best_price = המחיר הנוכחי (הטוב ביותר שידוע לנו)
                best = mark_price if mark_price > 0 else entry
                # SL ראשוני = SL הנוכחי ב-Bybit (אם קיים), אחרת חישוב לפי offset
                if current_sl > 0:
                    init_sl = current_sl
                else:
                    init_sl = (best - TRAIL_OFFSET) if side == "Buy" else (best + TRAIL_OFFSET)

                trailing_state[symbol] = {
                    "side":       side,
                    "entry":      entry,
                    "best_price": best,
                    "sl":         init_sl,
                    "active":     False,
                    "synced":     True
                }
                synced.append({
                    "symbol":     symbol,
                    "side":       side,
                    "entry":      entry,
                    "mark_price": mark_price,
                    "init_sl":    init_sl
                })
                print(f"[SYNC] Injected {symbol} {side} @ {entry:.2f}, mark={mark_price:.2f}, SL={init_sl:.2f}")

        return jsonify({
            "status":  "ok",
            "synced":  synced,
            "skipped": skipped
        })
    except Exception as e:
        err = traceback.format_exc()
        print(f"[SYNC ERROR] {err}")
        return jsonify({"error": str(e), "traceback": err}), 500

@app.route("/trail_inject", methods=["GET"])
def trail_inject():
    """
    הזרקה ידנית של פוזיציה לטריילינג.
    פרמטרים: symbol, side (Buy/Sell), entry, sl
    דוגמה: /trail_inject?symbol=BTCUSDC&side=Buy&entry=79094.30&sl=78841.50
    """
    try:
        symbol = request.args.get("symbol", "BTCPERP")
        side   = request.args.get("side", "Buy")
        entry  = float(request.args.get("entry", 0))
        sl     = float(request.args.get("sl", 0))

        if entry == 0:
            return jsonify({"error": "Missing entry price"}), 400

        mark_price = get_mark_price(symbol)
        best = mark_price if mark_price > 0 else entry
        init_sl = sl if sl > 0 else ((best - TRAIL_OFFSET) if side == "Buy" else (best + TRAIL_OFFSET))

        with trailing_lock:
            trailing_state[symbol] = {
                "side":       side,
                "entry":      entry,
                "best_price": best,
                "sl":         init_sl,
                "active":     False,
                "injected":   True
            }

        print(f"[INJECT] {symbol} {side} @ {entry:.2f}, mark={best:.2f}, SL={init_sl:.2f}")
        return jsonify({
            "status":     "ok",
            "symbol":     symbol,
            "side":       side,
            "entry":      entry,
            "mark_price": best,
            "init_sl":    init_sl,
            "message":    "Position injected into trailing stop. Trailing will activate after " + str(TRAIL_TRIGGER) + " points profit."
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/positions_debug", methods=["GET"])
def positions_debug():
    """
    מחזיר את כל הפוזיציות הפתוחות מ-Bybit API (raw).
    שימושי לגלות את שם הסימבול הנכון.
    """
    try:
        # נסה ללא סימבול ספציפי
        result_all = bybit_get("/v5/position/list", {
            "category": "linear",
            "settleCoin": "USDC"
        })
        result_usdc = bybit_get("/v5/position/list", {
            "category": "linear",
            "symbol": "BTCPERP"
        })
        result_perp = bybit_get("/v5/position/list", {
            "category": "linear",
            "symbol": "BTCPERP"
        })
        # סנן רק פוזיציות עם size > 0
        all_positions = result_all.get("result", {}).get("list", [])
        open_positions = [p for p in all_positions if float(p.get("size", 0)) > 0]
        return jsonify({
            "status": "ok",
            "open_positions_usdc_settle": open_positions,
            "btcusdc_direct": result_usdc,
            "btcperp_direct": result_perp,
            "all_count": len(all_positions)
        })
    except Exception as e:
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500

@app.route("/trail_debug", methods=["GET"])
def trail_debug():
    """
    מריץ מחזור אחד של הטריילינג ידנית ומחזיר תוצאות מפורטות.
    שימושי לבדיקה שהטריילינג עובד כמו שצריך.
    """
    debug_results = []
    with trailing_lock:
        symbols = list(trailing_state.keys())

    if not symbols:
        return jsonify({"status": "no_positions", "message": "No positions in trailing state"})

    for symbol in symbols:
        result = {"symbol": symbol, "steps": []}
        try:
            with trailing_lock:
                state = trailing_state.get(symbol)
            if not state:
                result["steps"].append("No state found")
                continue

            result["state_before"] = dict(state)

            # בדוק פוזיציה
            pos = get_position(symbol)
            result["position"] = pos
            result["steps"].append(f"Position check: has_position={pos.get('has_position')}")

            if not pos.get("has_position"):
                result["steps"].append("Position closed — would remove from trailing")
                debug_results.append(result)
                continue

            # בדוק מחיר
            mark_price = get_mark_price(symbol)
            result["mark_price"] = mark_price
            result["steps"].append(f"Mark price: {mark_price}")

            if mark_price == 0:
                result["steps"].append("Mark price is 0 — skipping")
                debug_results.append(result)
                continue

            with trailing_lock:
                state = trailing_state.get(symbol)
                side       = state["side"]
                entry      = state["entry"]
                best_price = state["best_price"]
                current_sl = state["sl"]

                if side == "Buy":
                    if mark_price > best_price:
                        state["best_price"] = mark_price
                        best_price = mark_price
                        result["steps"].append(f"Updated best_price to {best_price}")

                    profit_pts = best_price - entry
                    result["profit_pts"] = profit_pts
                    result["steps"].append(f"Profit: {profit_pts:.2f} pts (trigger={TRAIL_TRIGGER})")

                    if profit_pts >= TRAIL_TRIGGER:
                        if not state.get("active"):
                            state["active"] = True
                            result["steps"].append("Trailing ACTIVATED!")
                        new_sl = round(best_price - TRAIL_OFFSET, 2)
                        result["new_sl_calculated"] = new_sl
                        if new_sl > current_sl:
                            state["sl"] = new_sl
                            update_result = update_stop_loss(symbol, new_sl)
                            result["sl_update_result"] = update_result
                            result["steps"].append(f"SL updated: {current_sl} -> {new_sl}")
                        else:
                            result["steps"].append(f"SL not updated: new_sl={new_sl} <= current_sl={current_sl}")
                    else:
                        result["steps"].append(f"Waiting for trigger: {profit_pts:.2f}/{TRAIL_TRIGGER} pts")

                elif side == "Sell":
                    if mark_price < best_price:
                        state["best_price"] = mark_price
                        best_price = mark_price
                        result["steps"].append(f"Updated best_price to {best_price}")

                    profit_pts = entry - best_price
                    result["profit_pts"] = profit_pts
                    result["steps"].append(f"Profit: {profit_pts:.2f} pts (trigger={TRAIL_TRIGGER})")

                    if profit_pts >= TRAIL_TRIGGER:
                        if not state.get("active"):
                            state["active"] = True
                            result["steps"].append("Trailing ACTIVATED!")
                        new_sl = round(best_price + TRAIL_OFFSET, 2)
                        result["new_sl_calculated"] = new_sl
                        if new_sl < current_sl:
                            state["sl"] = new_sl
                            update_result = update_stop_loss(symbol, new_sl)
                            result["sl_update_result"] = update_result
                            result["steps"].append(f"SL updated: {current_sl} -> {new_sl}")
                        else:
                            result["steps"].append(f"SL not updated: new_sl={new_sl} >= current_sl={current_sl}")
                    else:
                        result["steps"].append(f"Waiting for trigger: {profit_pts:.2f}/{TRAIL_TRIGGER} pts")

                result["state_after"] = dict(state)

        except Exception as e:
            result["error"] = str(e)
            result["traceback"] = traceback.format_exc()

        debug_results.append(result)

    return jsonify({"status": "ok", "debug": debug_results})

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
