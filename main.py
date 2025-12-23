from fastapi import FastAPI
import MetaTrader5 as mt5
from decimal import Decimal, ROUND_HALF_UP
import threading
import time

app = FastAPI()

ticket_map = {}
MASTER_SECRET = "TopFrag?!"

# ===============================
# MT5 STATE
# ===============================
mt5_ready = False
mt5_lock = threading.Lock()

# ===============================
# MT5 SAFE INIT (NON-BLOCKING)
# ===============================
def init_mt5_safe():
    global mt5_ready

    for attempt in range(1, 6):
        if mt5.initialize():
            print("✅ MT5 initialized")
            mt5_ready = True
            return
        print(f"⚠️ MT5 init failed (attempt {attempt}):", mt5.last_error())
        time.sleep(2)

    print("❌ MT5 failed to initialize after retries")

@app.on_event("startup")
def start_mt5_background():
    threading.Thread(target=init_mt5_safe, daemon=True).start()

# ===============================
# CLEAN SHUTDOWN
# ===============================
@app.on_event("shutdown")
def shutdown_mt5():
    if mt5_ready:
        mt5.shutdown()

# ===============================
# ENSURE MT5 READY
# ===============================
def ensure_mt5(timeout=10):
    start = time.time()
    while not mt5_ready:
        if time.time() - start > timeout:
            raise RuntimeError("MT5 not ready (timeout)")
        time.sleep(0.2)

# ===============================
# LOT CALC (DEBUG HEAVY)
# ===============================
def calculate_volume():
    print("\n[LOT DEBUG] ===== START LOT CALC =====")

    account = mt5.account_info()
    if account is None:
        print("[LOT DEBUG] ❌ account_info() = None")
        raise Exception("Gagal mengambil account info MT5")

    print("[LOT DEBUG] Login:", account.login)
    print("[LOT DEBUG] Server:", account.server)
    print("[LOT DEBUG] Balance:", account.balance)
    print("[LOT DEBUG] Equity:", account.equity)
    print("[LOT DEBUG] Free Margin:", account.margin_free)
    print("[LOT DEBUG] Leverage:", account.leverage)

    balance = Decimal(str(account.balance))
    print("[LOT DEBUG] Balance (Decimal):", balance)

    # rumus utama
    raw_volume = (balance / Decimal("10000")) / Decimal("2")
    print("[LOT DEBUG] Raw volume (before rounding):", raw_volume)

    volume = raw_volume.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    print("[LOT DEBUG] Volume after rounding:", volume)

    if volume < Decimal("0.01"):
        print("[LOT DEBUG] ⚠️ Volume < 0.01 → force to 0.01")
        volume = Decimal("0.01")

    print("[LOT DEBUG] FINAL LOT:", volume)
    print("[LOT DEBUG] ===== END LOT CALC =====\n")

    return float(volume)

# ===============================
# CANCEL ALL PENDING
# ===============================
def cancel_all_pending(symbol):
    orders = mt5.orders_get(symbol=symbol)
    if not orders:
        print(f"[MT5 DEBUG] No pending orders for {symbol}")
        return 0

    count = 0
    for o in orders:
        mt5.order_send({
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": o.ticket
        })
        count += 1

    print(f"[MT5 DEBUG] Canceled {count} pending orders for {symbol}")
    return count

# ===============================
# WEBHOOK
# ===============================
@app.post("/webhook")
def webhook(data: dict):
    try:
        print("\n[WEBHOOK] Incoming data:", data)

        ensure_mt5()

        if data.get("secret") != MASTER_SECRET:
            print("[WEBHOOK] ❌ Invalid secret")
            return {"error": "unauthorized"}

        action = data.get("action")
        print("[WEBHOOK] Action:", action)

        # ===============================
        # OPEN → PENDING ORDER
        # ===============================
        if action == "OPEN":

            lot = calculate_volume()
            print("[WEBHOOK] Lot to be used:", lot)

            order_type_map = {
                "BUY_LIMIT": mt5.ORDER_TYPE_BUY_LIMIT,
                "SELL_LIMIT": mt5.ORDER_TYPE_SELL_LIMIT
            }

            order_type = order_type_map.get(data.get("type"))
            if order_type is None:
                print("[WEBHOOK] ❌ Invalid order type:", data.get("type"))
                return {"error": "invalid_order_type"}

            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": data["symbol"],
                "volume": lot,
                "type": order_type,
                "price": float(data["entry"]),
                "deviation": 20,
                "magic": 86421357,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN
            }

            print("[WEBHOOK] Order request:", request)

            result = mt5.order_send(request)

            if result is None:
                print("[WEBHOOK] ❌ order_send returned None")
                return {"error": "order_send_failed", "detail": mt5.last_error()}

            print("[WEBHOOK] Order result:", result)

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return {
                    "error": "order_rejected",
                    "retcode": result.retcode,
                    "comment": result.comment
                }

            ticket_map[data["master_ticket"]] = result.order

            return {
                "status": "pending_created",
                "slave_ticket": result.order,
                "lot": lot
            }

        # ===============================
        # CANCEL PENDING
        # ===============================
        elif action == "CANCEL_PENDING":

            symbol = data["symbol"]
            canceled = cancel_all_pending(symbol)

            return {
                "status": "pending_canceled",
                "symbol": symbol,
                "count": canceled
            }

        # ===============================
        # CLOSE POSITION
        # ===============================
        elif action == "CLOSE":

            ticket = ticket_map.get(data["master_ticket"])
            if not ticket:
                return {"error": "ticket_not_found"}

            positions = mt5.positions_get(ticket=ticket)
            if not positions:
                return {"error": "position_not_found"}

            pos = positions[0]

            close_type = (
                mt5.ORDER_TYPE_SELL
                if pos.type == mt5.POSITION_TYPE_BUY
                else mt5.ORDER_TYPE_BUY
            )

            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL,
                "position": pos.ticket,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": close_type,
                "deviation": 20
            })

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                return {
                    "error": "close_failed",
                    "retcode": result.retcode,
                    "comment": result.comment
                }

            return {"status": "closed"}

        return {"error": "unknown_action"}

    except Exception as e:
        print("[EXCEPTION]", str(e))
        return {"error": "exception", "detail": str(e)}
