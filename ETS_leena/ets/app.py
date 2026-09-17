import json
import time
import random
import threading
import functools
from datetime import datetime

from flask import Flask, request, jsonify, session, render_template, Response
from werkzeug.security import generate_password_hash, check_password_hash

import database as db

app = Flask(__name__)
app.secret_key = "change-this-secret-key-in-production"  # TODO: set via env var
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
)

print("[BOOT] Initializing database (create + auto-seed if empty)...")
db.init_db()
db.seed_db()
print("[BOOT] Database ready.")

# ---------------------------------------------------------------------------
# yfinance helpers (LAZY — imported only when actually needed)
# ---------------------------------------------------------------------------

_yf_module = None
_yf_import_error = None


def _get_yf():
    """Import yfinance on first use only. Never blocks app startup or login."""
    global _yf_module, _yf_import_error
    if _yf_module is not None:
        return _yf_module
    if _yf_import_error is not None:
        return None
    try:
        import yfinance as yf  # heavy import — only happens here, on demand
        _yf_module = yf
        return yf
    except Exception as e:
        _yf_import_error = str(e)
        print("[yfinance] Not available:", e)
        return None


def to_yf_symbol(symbol, exchange):
    """Map an internal symbol/exchange to the Yahoo Finance ticker string."""
    if exchange == "NSE":
        return symbol + ".NS"
    return symbol  # NASDAQ / NYSE trade as-is on Yahoo Finance


def fetch_live_quote(symbol, exchange, timeout=6):
    """
    Fetch a single live quote from Yahoo Finance.
    Returns a dict of fields or None on failure/timeout/unavailable.
    Never raises — safe to call from a request handler.
    """
    yf = _get_yf()
    if yf is None:
        return None

    yf_sym = to_yf_symbol(symbol, exchange)
    try:
        import requests
        sess = requests.Session()
        sess.request = functools.partial(sess.request, timeout=timeout)  # hard timeout
        t = yf.Ticker(yf_sym, session=sess)
        fi = t.fast_info
        ltp = float(fi.get("last_price") or fi.get("lastPrice") or 0)
        prev_close = float(fi.get("previous_close") or fi.get("previousClose") or ltp)
        day_high = float(fi.get("day_high") or ltp)
        day_low = float(fi.get("day_low") or ltp)
        day_open = float(fi.get("open") or ltp)
        volume = int(fi.get("last_volume") or fi.get("lastVolume") or 0)
        if not ltp:
            return None
        change_pct = round(((ltp - prev_close) / prev_close) * 100, 2) if prev_close else 0
        return {
            "ltp": round(ltp, 2),
            "open": round(day_open, 2),
            "high": round(day_high, 2),
            "low": round(day_low, 2),
            "close": round(ltp, 2),
            "prev_close": round(prev_close, 2),
            "volume": volume,
            "change_pct": change_pct,
        }
    except Exception as e:
        print("yfinance error for", yf_sym, ":", e)
        return None


# ---------------------------------------------------------------------------
# Background price simulator
# ---------------------------------------------------------------------------
# Runs forever in a daemon thread. Every SIM_INTERVAL_SECONDS it nudges each
# stock's ltp by a small random percentage (a simple random walk), so that
# P&L on the dashboard/portfolio pages actually changes over time instead of
# staying frozen at 0.0. This is purely cosmetic/demo market movement and is
# completely independent of the yfinance sync — either one can update ltp,
# whichever ran most recently wins.

SIM_INTERVAL_SECONDS = 5      # how often prices tick
SIM_MAX_MOVE_PCT = 0.35       # max % move per tick, e.g. 0.35 = +/-0.35%


def _simulate_market_tick():
    conn = db.get_db()
    try:
        stocks = conn.execute("SELECT * FROM stocks").fetchall()
        for s in stocks:
            prev_close = s["prev_close"] or s["ltp"] or 1
            move_pct = random.uniform(-SIM_MAX_MOVE_PCT, SIM_MAX_MOVE_PCT)
            new_ltp = round(max(0.01, s["ltp"] * (1 + move_pct / 100)), 2)
            new_high = round(max(s["high"], new_ltp), 2)
            new_low = round(min(s["low"], new_ltp), 2) if s["low"] else new_ltp
            new_change_pct = round(((new_ltp - prev_close) / prev_close) * 100, 2) if prev_close else 0
            conn.execute(
                """UPDATE stocks SET ltp=?, high=?, low=?, close=?, change_pct=?,
                   updated_at=datetime('now') WHERE symbol=?""",
                (new_ltp, new_high, new_low, new_ltp, new_change_pct, s["symbol"]),
            )
        conn.commit()
    except Exception as e:
        print("[price-sim] tick error:", e)
    finally:
        conn.close()


def _price_simulator_loop():
    print(f"[price-sim] Background simulator started (every {SIM_INTERVAL_SECONDS}s, "
          f"+/-{SIM_MAX_MOVE_PCT}% per tick).")
    while True:
        time.sleep(SIM_INTERVAL_SECONDS)
        _simulate_market_tick()


def start_price_simulator():
    t = threading.Thread(target=_price_simulator_loop, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def current_user(conn):
    uid = session.get("user_id")
    if not uid:
        return None
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        conn = db.get_db()
        u = current_user(conn)
        conn.close()
        if not u:
            return jsonify({"error": "Not authenticated"}), 401
        if not u["is_admin"]:
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return wrapper


def user_public(u):
    return {
        "id": u["id"], "name": u["name"], "email": u["email"], "phone": u["phone"],
        "is_admin": u["is_admin"], "balance": u["balance"],
        "available_balance": u["available_balance"],
    }


def stock_to_dict(s):
    return {
        "symbol": s["symbol"], "name": s["name"], "exchange": s["exchange"], "sector": s["sector"],
        "currency": s["currency"], "ltp": s["ltp"], "open": s["open"], "high": s["high"], "low": s["low"],
        "close": s["close"], "prev_close": s["prev_close"], "volume": s["volume"], "change": s["change_pct"],
        "pe": s["pe"], "market_cap": s["market_cap"], "dividend_yield": s["div_yield"], "rsi": s["rsi"],
        "sma20": s["sma20"], "ema50": s["ema50"], "high52": s["high52"], "low52": s["low52"],
        "updated_at": s["updated_at"],
    }


def stock_field_value(st, field):
    """Given a stocks row and an alert 'field' name, return the current numeric value to compare."""
    if field == "rsi":
        return st["rsi"]
    if field == "sma20":
        return st["sma20"]
    if field == "ema50":
        return st["ema50"]
    if field == "change":
        return st["change_pct"]
    return st["ltp"]  # default / "price"


ALERT_FIELDS = {"price", "rsi", "sma20", "ema50", "change"}
ALERT_FIELD_LABELS = {"price": "Price", "rsi": "RSI", "sma20": "SMA20", "ema50": "EMA50", "change": "Change %"}


# ---------------------------------------------------------------------------
# Static page
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    """Quick way to confirm the server is actually up and responsive."""
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


# ---------------------------------------------------------------------------
# Stocks (full universe — used by search / filters / watchlist / trade)
# ---------------------------------------------------------------------------

@app.route("/api/stocks")
@login_required
def list_stocks():
    """Returns the FULL stock universe. The frontend previously had no way to
    fetch this outside the admin panel, so it silently fell back to a small
    ~22-symbol hard-coded list — meaning most real symbols (and Filters/
    Watchlist lookups for them) simply couldn't be found. This route fixes
    that for every logged-in user, not just admins."""
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM stocks ORDER BY symbol ASC").fetchall()
    conn.close()
    return jsonify({"data": [stock_to_dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route("/api/filter", methods=["POST"])
@login_required
def filter_stocks():
    conn = db.get_db()
    try:
        data = request.get_json() or {}
        filters = data.get("filters", [])
        sort_by = data.get("sort_by", "symbol")
        sort_desc = data.get("sort_desc", False)

        stocks = conn.execute("SELECT * FROM stocks").fetchall()
        results = []

        for s in stocks:
            sd = stock_to_dict(s)
            match = True
            for f in filters:
                field = f.get("field", "price")
                op = f.get("op", "=")
                try:
                    value = float(f.get("value", 0))
                except:
                    continue

                if field == "price":
                    cur = sd.get("ltp") or 0
                elif field == "rsi":
                    cur = sd.get("rsi") or 0
                elif field == "sma20":
                    cur = sd.get("sma20") or 0
                elif field == "ema50":
                    cur = sd.get("ema50") or 0
                elif field == "change":
                    cur = sd.get("change") or 0
                elif field == "volume":
                    cur = sd.get("volume") or 0
                elif field == "pe":
                    cur = sd.get("pe") or 0
                elif field == "market_cap":
                    cur = sd.get("market_cap") or 0
                else:
                    cur = 0

                try:
                    cur = float(cur)
                except:
                    cur = 0

                if op == ">" and not (cur > value):
                    match = False
                elif op == ">=" and not (cur >= value):
                    match = False
                elif op == "<" and not (cur < value):
                    match = False
                elif op == "<=" and not (cur <= value):
                    match = False
                elif op == "=" and not (cur == value):
                    match = False

            if match:
                results.append(sd)

        # Sort
        sort_map = {
            "symbol": "symbol", "price": "ltp", "change": "change",
            "volume": "volume", "rsi": "rsi", "pe": "pe"
        }
        sort_key = sort_map.get(sort_by, "symbol")
        results.sort(key=lambda x: x.get(sort_key) or 0, reverse=sort_desc)

        return jsonify({"data": results})
    finally:
        conn.close()


@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    password = data.get("password") or ""

    if not name or not email or len(password) < 6:
        return jsonify({"error": "Name, email and a 6+ char password are required"}), 400

    conn = db.get_db()
    exists = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if exists:
        conn.close()
        return jsonify({"error": "Email already registered"}), 409

    pw_hash = generate_password_hash(password)
    cur = conn.execute(
        "INSERT INTO users (name,email,phone,password_hash,balance,available_balance) VALUES (?,?,?,?,?,?)",
        (name, email, phone, pw_hash, 100000, 100000)
    )
    uid = cur.lastrowid
    conn.execute("INSERT INTO folders (user_id, name) VALUES (?,'Default')", (uid,))
    db.log_activity(conn, uid, "REGISTER", "New account created", request.remote_addr)
    conn.commit()
    conn.close()
    return jsonify({"data": {"id": uid, "name": name, "email": email}}), 201


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    conn = db.get_db()
    u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u or not check_password_hash(u["password_hash"], password):
        conn.close()
        return jsonify({"error": "Invalid email or password"}), 401

    session["user_id"] = u["id"]
    db.log_activity(conn, u["id"], "ADMIN_LOGIN" if u["is_admin"] else "LOGIN",
                     u["email"], request.remote_addr)
    conn.close()
    return jsonify({"data": user_public(u)})


@app.route("/api/logout", methods=["POST"])
def logout():
    uid = session.get("user_id")
    if uid:
        conn = db.get_db()
        db.log_activity(conn, uid, "LOGOUT", "", request.remote_addr)
        conn.close()
    session.clear()
    return jsonify({"data": "ok"})


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/api/dashboard")
@login_required
def dashboard():
    conn = db.get_db()
    u = current_user(conn)
    stocks = conn.execute("SELECT * FROM stocks ORDER BY change_pct DESC").fetchall()
    stocks = [stock_to_dict(s) for s in stocks]

    advancers = sum(1 for s in stocks if s["change"] > 0)
    decliners = sum(1 for s in stocks if s["change"] < 0)
    gainers = stocks[:5]
    losers = sorted(stocks, key=lambda s: s["change"])[:5]

    holdings = conn.execute("SELECT * FROM holdings WHERE user_id=?", (u["id"],)).fetchall()
    pnl = 0.0
    for h in holdings:
        st = conn.execute("SELECT ltp FROM stocks WHERE symbol=?", (h["symbol"],)).fetchone()
        if st:
            pnl += (st["ltp"] - h["avg_price"]) * h["quantity"]

    recent = conn.execute(
        "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT 5", (u["id"],)
    ).fetchall()
    recent_orders = [{
        "order_id": o["order_id"], "symbol": o["symbol"], "order_type": o["order_type"],
        "quantity": o["quantity"], "price": o["price"], "status": o["status"],
        "created_at": o["created_at"],
    } for o in recent]

    conn.close()
    return jsonify({"data": {
        "market": {"total": len(stocks), "advancers": advancers, "decliners": decliners},
        "portfolio": {"pnl": round(pnl, 2)},
        "gainers": gainers, "losers": losers, "recent_orders": recent_orders,
    }})




# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

@app.route("/api/portfolio")
@login_required
def portfolio():
    conn = db.get_db()
    u = current_user(conn)
    holdings = conn.execute("SELECT * FROM holdings WHERE user_id=?", (u["id"],)).fetchall()

    out, total_invested, total_value = [], 0.0, 0.0
    for h in holdings:
        st = conn.execute("SELECT * FROM stocks WHERE symbol=?", (h["symbol"],)).fetchone()
        if not st:
            continue
        invested = h["avg_price"] * h["quantity"]
        value = st["ltp"] * h["quantity"]
        pnl = value - invested
        pnl_pct = (pnl / invested * 100) if invested else 0
        total_invested += invested
        total_value += value
        out.append({
            "symbol": h["symbol"], "name": st["name"], "quantity": h["quantity"],
            "avg_price": round(h["avg_price"], 2), "current_price": st["ltp"],
            "value": round(value, 2), "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
        })
    conn.close()
    return jsonify({
        "data": out,
        "total_invested": round(total_invested, 2),
        "total_value": round(total_value, 2),
        "total_pnl": round(total_value - total_invested, 2),
    })


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@app.route("/api/orders", methods=["GET", "POST"])
@login_required
def orders():
    conn = db.get_db()
    u = current_user(conn)

    if request.method == "GET":
        rows = conn.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC", (u["id"],)
        ).fetchall()
        data = [dict(r) for r in rows]
        conn.close()
        return jsonify({"data": data})

    # POST — place a new order
    data = request.get_json(force=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    order_type = data.get("order_type")       # BUY / SELL
    order_mode = data.get("order_mode", "Market")
    quantity = int(data.get("quantity") or 0)
    price = float(data.get("price") or 0)
    duration = data.get("duration", "DAY")

    st = conn.execute("SELECT * FROM stocks WHERE symbol=?", (symbol,)).fetchone()
    if not st:
        conn.close()
        return jsonify({"error": "Unknown symbol"}), 400
    if order_type not in ("BUY", "SELL") or quantity <= 0 or price <= 0:
        conn.close()
        return jsonify({"error": "Invalid order parameters"}), 400

    oid = db.next_order_id(conn)
    status = "NEW"
    amount = quantity * price

    if order_mode == "Market":
        # Market orders execute immediately against live LTP
        exec_price = st["ltp"]
        amount = quantity * exec_price
        charges = db.calc_charges(amount)

        if order_type == "BUY":
            total_cost = amount + charges["total_charges"]
            if total_cost > u["available_balance"]:
                conn.close()
                return jsonify({"error": "Insufficient available balance"}), 400
            conn.execute("UPDATE users SET available_balance = available_balance - ?, "
                         "balance = balance - ? WHERE id=?", (total_cost, total_cost, u["id"]))
            existing = conn.execute("SELECT * FROM holdings WHERE user_id=? AND symbol=?",
                                     (u["id"], symbol)).fetchone()
            if existing:
                new_qty = existing["quantity"] + quantity
                new_avg = ((existing["avg_price"] * existing["quantity"]) + amount) / new_qty
                conn.execute("UPDATE holdings SET quantity=?, avg_price=? WHERE id=?",
                             (new_qty, new_avg, existing["id"]))
            else:
                conn.execute("INSERT INTO holdings (user_id,symbol,quantity,avg_price) VALUES (?,?,?,?)",
                             (u["id"], symbol, quantity, exec_price))
        else:  # SELL
            existing = conn.execute("SELECT * FROM holdings WHERE user_id=? AND symbol=?",
                                     (u["id"], symbol)).fetchone()
            if not existing or existing["quantity"] < quantity:
                conn.close()
                return jsonify({"error": "Insufficient holdings to sell"}), 400
            proceeds = amount - charges["total_charges"]
            conn.execute("UPDATE users SET available_balance = available_balance + ?, "
                         "balance = balance + ? WHERE id=?", (proceeds, proceeds, u["id"]))
            new_qty = existing["quantity"] - quantity
            if new_qty <= 0:
                conn.execute("DELETE FROM holdings WHERE id=?", (existing["id"],))
            else:
                conn.execute("UPDATE holdings SET quantity=? WHERE id=?", (new_qty, existing["id"]))

        status = "EXECUTED"
        price = exec_price
        conn.execute("""INSERT INTO transactions
                        (user_id,type,symbol,quantity,price,amount,commission,exchange_fee,gst,total_charges)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (u["id"], order_type, symbol, quantity, exec_price, amount,
                      charges["commission"], charges["exchange_fee"], charges["gst"], charges["total_charges"]))
        db.create_notification(conn, u["id"], f"{order_type} Order Executed",
                                f"{quantity} {symbol} @ ₹{exec_price:.2f}", "success", "✅")

    conn.execute("""INSERT INTO orders (order_id,user_id,symbol,order_type,order_mode,quantity,price,duration,status)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (oid, u["id"], symbol, order_type, order_mode, quantity, price, duration, status))
    db.log_activity(conn, u["id"], "ORDER", f"{order_type} {quantity} {symbol} ({status})", request.remote_addr)
    conn.commit()
    conn.close()
    return jsonify({"data": {"order_id": oid, "status": status}}), 201


@app.route("/api/orders/<order_id>/cancel", methods=["POST"])
@login_required
def cancel_order(order_id):
    conn = db.get_db()
    u = current_user(conn)
    o = conn.execute("SELECT * FROM orders WHERE order_id=? AND user_id=?", (order_id, u["id"])).fetchone()
    if not o:
        conn.close()
        return jsonify({"error": "Order not found"}), 404
    if o["status"] not in ("NEW", "ACKNOWLEDGED"):
        conn.close()
        return jsonify({"error": "Order cannot be cancelled"}), 400
    conn.execute("UPDATE orders SET status='CANCELLED' WHERE order_id=?", (order_id,))
    db.log_activity(conn, u["id"], "CANCEL_ORDER", order_id, request.remote_addr)
    conn.commit()
    conn.close()
    return jsonify({"data": "cancelled"})


@app.route("/api/orders/<order_id>/execute", methods=["POST"])
@login_required
def execute_order(order_id):
    """Manually execute a pending Limit/Stop order at its set price."""
    conn = db.get_db()
    u = current_user(conn)
    o = conn.execute("SELECT * FROM orders WHERE order_id=? AND user_id=?", (order_id, u["id"])).fetchone()
    if not o:
        conn.close()
        return jsonify({"error": "Order not found"}), 404
    if o["status"] not in ("NEW", "ACKNOWLEDGED"):
        conn.close()
        return jsonify({"error": "Order cannot be executed"}), 400

    amount = o["quantity"] * o["price"]
    charges = db.calc_charges(amount)

    if o["order_type"] == "BUY":
        total_cost = amount + charges["total_charges"]
        if total_cost > u["available_balance"]:
            conn.close()
            return jsonify({"error": "Insufficient available balance"}), 400
        conn.execute("UPDATE users SET available_balance = available_balance - ?, "
                     "balance = balance - ? WHERE id=?", (total_cost, total_cost, u["id"]))
        existing = conn.execute("SELECT * FROM holdings WHERE user_id=? AND symbol=?",
                                 (u["id"], o["symbol"])).fetchone()
        if existing:
            new_qty = existing["quantity"] + o["quantity"]
            new_avg = ((existing["avg_price"] * existing["quantity"]) + amount) / new_qty
            conn.execute("UPDATE holdings SET quantity=?, avg_price=? WHERE id=?",
                         (new_qty, new_avg, existing["id"]))
        else:
            conn.execute("INSERT INTO holdings (user_id,symbol,quantity,avg_price) VALUES (?,?,?,?)",
                         (u["id"], o["symbol"], o["quantity"], o["price"]))
    else:
        existing = conn.execute("SELECT * FROM holdings WHERE user_id=? AND symbol=?",
                                 (u["id"], o["symbol"])).fetchone()
        if not existing or existing["quantity"] < o["quantity"]:
            conn.close()
            return jsonify({"error": "Insufficient holdings to sell"}), 400
        proceeds = amount - charges["total_charges"]
        conn.execute("UPDATE users SET available_balance = available_balance + ?, "
                     "balance = balance + ? WHERE id=?", (proceeds, proceeds, u["id"]))
        new_qty = existing["quantity"] - o["quantity"]
        if new_qty <= 0:
            conn.execute("DELETE FROM holdings WHERE id=?", (existing["id"],))
        else:
            conn.execute("UPDATE holdings SET quantity=? WHERE id=?", (new_qty, existing["id"]))

    conn.execute("UPDATE orders SET status='EXECUTED' WHERE order_id=?", (order_id,))
    conn.execute("""INSERT INTO transactions
                    (user_id,type,symbol,quantity,price,amount,commission,exchange_fee,gst,total_charges)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (u["id"], o["order_type"], o["symbol"], o["quantity"], o["price"], amount,
                  charges["commission"], charges["exchange_fee"], charges["gst"], charges["total_charges"]))
    db.create_notification(conn, u["id"], "Order Executed",
                            f"{o['order_type']} {o['quantity']} {o['symbol']} @ ₹{o['price']:.2f}", "success", "✅")
    db.log_activity(conn, u["id"], "EXECUTE_ORDER", order_id, request.remote_addr)
    conn.commit()
    conn.close()
    return jsonify({"data": "executed"})


# ---------------------------------------------------------------------------
# Trading account
# ---------------------------------------------------------------------------

@app.route("/api/trading-account")
@login_required
def trading_account():
    conn = db.get_db()
    u = current_user(conn)
    holdings = conn.execute("SELECT * FROM holdings WHERE user_id=?", (u["id"],)).fetchall()
    holdings_value = 0.0
    for h in holdings:
        st = conn.execute("SELECT ltp FROM stocks WHERE symbol=?", (h["symbol"],)).fetchone()
        if st:
            holdings_value += st["ltp"] * h["quantity"]

    today = datetime.now().strftime("%Y-%m-%d")
    today_tx = conn.execute(
        "SELECT * FROM transactions WHERE user_id=? AND date(created_at)=?", (u["id"], today)
    ).fetchall()
    today_buy = sum(t["amount"] for t in today_tx if t["type"] == "BUY")
    today_sell = sum(t["amount"] for t in today_tx if t["type"] == "SELL")

    all_tx = conn.execute("SELECT * FROM transactions WHERE user_id=?", (u["id"],)).fetchall()
    commission = sum(t["commission"] for t in all_tx)
    exchange_fee = sum(t["exchange_fee"] for t in all_tx)
    gst = sum(t["gst"] for t in all_tx)
    total_charges = sum(t["total_charges"] for t in all_tx)
    today_charges = sum(t["total_charges"] for t in today_tx)

    blocked = u["balance"] - u["available_balance"]
    net_worth = u["available_balance"] + holdings_value
    net_settlement = today_sell - today_buy - today_charges

    conn.close()
    return jsonify({"data": {
        "account_balance": round(u["balance"], 2),
        "available_balance": round(u["available_balance"], 2),
        "blocked_amount": round(blocked, 2),
        "net_worth": round(net_worth, 2),
        "today_buy": round(today_buy, 2),
        "today_sell": round(today_sell, 2),
        "holdings_value": round(holdings_value, 2),
        "commission": round(commission, 2),
        "exchange_fee": round(exchange_fee, 2),
        "gst": round(gst, 2),
        "total_charges": round(total_charges, 2),
        "net_settlement": round(net_settlement, 2),
    }})


@app.route("/api/trading-account/transactions")
@login_required
def trading_transactions():
    conn = db.get_db()
    u = current_user(conn)
    rows = conn.execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (u["id"],)
    ).fetchall()
    conn.close()
    return jsonify({"data": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Alerts (Price / RSI / SMA20 / EMA50 / Change % — with field + op)
# ---------------------------------------------------------------------------

@app.route("/api/alerts", methods=["GET", "POST"])
@login_required
def alerts():
    conn = db.get_db()
    u = current_user(conn)

    if request.method == "GET":
        rows = conn.execute("SELECT * FROM alerts WHERE user_id=? ORDER BY created_at DESC",
                             (u["id"],)).fetchall()
        out = []
        for a in rows:
            st = conn.execute("SELECT * FROM stocks WHERE symbol=?", (a["symbol"],)).fetchone()
            field = a["field"] or "price"
            op = a["op"] or "gte"
            cur_val = stock_field_value(st, field) if st else 0
            out.append({
                "id": a["id"], "symbol": a["symbol"], "field": field, "op": op,
                "target_price": a["target_price"], "current_value": cur_val, "status": a["status"],
            })
        conn.close()
        return jsonify({"data": out})

    data = request.get_json(force=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    target = float(data.get("target_price") or 0)
    field = (data.get("field") or "price").strip().lower()
    op = (data.get("op") or "gte").strip().lower()
    if field not in ALERT_FIELDS:
        field = "price"
    if op not in ("gte", "lte"):
        op = "gte"

    st = conn.execute("SELECT * FROM stocks WHERE symbol=?", (symbol,)).fetchone()
    if not st:
        conn.close()
        return jsonify({"error": "Unknown symbol"}), 400
    if target <= 0:
        conn.close()
        return jsonify({"error": "Enter a valid target price"}), 400
    conn.execute("INSERT INTO alerts (user_id,symbol,field,op,target_price) VALUES (?,?,?,?,?)",
                 (u["id"], symbol, field, op, target))
    conn.commit()
    conn.close()
    return jsonify({"data": "created"}), 201


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
@login_required
def delete_alert(alert_id):
    conn = db.get_db()
    u = current_user(conn)
    conn.execute("DELETE FROM alerts WHERE id=? AND user_id=?", (alert_id, u["id"]))
    conn.commit()
    conn.close()
    return jsonify({"data": "deleted"})


# ---------------------------------------------------------------------------
# Watchlist & Folders
# ---------------------------------------------------------------------------

@app.route("/api/folders", methods=["GET", "POST"])
@login_required
def folders_route():
    conn = db.get_db()
    u = current_user(conn)

    if request.method == "GET":
        rows = conn.execute("SELECT * FROM folders WHERE user_id=? ORDER BY id ASC", (u["id"],)).fetchall()
        conn.close()
        return jsonify({"data": [{"id": r["id"], "name": r["name"]} for r in rows]})

    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        conn.close()
        return jsonify({"error": "Folder name required"}), 400
    exists = conn.execute("SELECT id FROM folders WHERE user_id=? AND name=?", (u["id"], name)).fetchone()
    if exists:
        conn.close()
        return jsonify({"error": f'A folder named "{name}" already exists'}), 409
    cur = conn.execute("INSERT INTO folders (user_id, name) VALUES (?,?)", (u["id"], name))
    fid = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"data": {"id": fid, "name": name}}), 201


@app.route("/api/watchlist", methods=["GET", "POST"])
@login_required
def watchlist_route():
    conn = db.get_db()
    u = current_user(conn)

    if request.method == "GET":
        rows = conn.execute("SELECT * FROM watchlist WHERE user_id=? ORDER BY id ASC", (u["id"],)).fetchall()
        conn.close()
        return jsonify({"data": [{"id": r["id"], "symbol": r["symbol"], "folder_id": r["folder_id"]} for r in rows]})

    data = request.get_json(force=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    folder_id = data.get("folder_id")
    if not symbol or not folder_id:
        conn.close()
        return jsonify({"error": "Symbol and folder are required"}), 400
    st = conn.execute("SELECT id FROM stocks WHERE symbol=?", (symbol,)).fetchone()
    if not st:
        conn.close()
        return jsonify({"error": f'Stock "{symbol}" not found'}), 400
    folder = conn.execute("SELECT id FROM folders WHERE id=? AND user_id=?", (folder_id, u["id"])).fetchone()
    if not folder:
        conn.close()
        return jsonify({"error": "Invalid folder"}), 400
    exists = conn.execute("SELECT id FROM watchlist WHERE user_id=? AND folder_id=? AND symbol=?",
                           (u["id"], folder_id, symbol)).fetchone()
    if exists:
        conn.close()
        return jsonify({"error": f"{symbol} is already in this folder"}), 409
    cur = conn.execute("INSERT INTO watchlist (user_id, folder_id, symbol) VALUES (?,?,?)",
                        (u["id"], folder_id, symbol))
    wid = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"data": {"id": wid, "symbol": symbol, "folder_id": folder_id}}), 201


@app.route("/api/watchlist/<int:wid>", methods=["DELETE"])
@login_required
def delete_watchlist_item(wid):
    conn = db.get_db()
    u = current_user(conn)
    conn.execute("DELETE FROM watchlist WHERE id=? AND user_id=?", (wid, u["id"]))
    conn.commit()
    conn.close()
    return jsonify({"data": "deleted"})


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@app.route("/api/profile", methods=["PUT"])
@login_required
def update_profile():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    conn = db.get_db()
    u = current_user(conn)
    if name:
        conn.execute("UPDATE users SET name=?, phone=? WHERE id=?", (name, phone, u["id"]))
        conn.commit()
    conn.close()
    return jsonify({"data": "saved"})


# ---------------------------------------------------------------------------
# Notifications (incl. Server-Sent Events stream)
# ---------------------------------------------------------------------------

@app.route("/api/notifications")
@login_required
def list_notifications():
    conn = db.get_db()
    u = current_user(conn)
    rows = conn.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (u["id"],)
    ).fetchall()
    conn.close()
    return jsonify({"data": [dict(r) for r in rows]})


@app.route("/api/notifications/<int:nid>/read", methods=["POST"])
@login_required
def mark_notif_read(nid):
    conn = db.get_db()
    u = current_user(conn)
    conn.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (nid, u["id"]))
    conn.commit()
    conn.close()
    return jsonify({"data": "ok"})


@app.route("/api/notifications/read-all", methods=["POST"])
@login_required
def mark_all_read():
    conn = db.get_db()
    u = current_user(conn)
    conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (u["id"],))
    conn.commit()
    conn.close()
    return jsonify({"data": "ok"})


@app.route("/api/notifications/stream")
@login_required
def notifications_stream():
    """
    Server-Sent Events stream. Polls the DB every 3 seconds for notifications
    newer than the last one seen and pushes them to the browser.
    NOTE: run the app with `threaded=True` (already set below) so this
    long-lived connection doesn't block other requests.
    """
    uid = session.get("user_id")

    def gen():
        conn = db.get_db()
        last_id_row = conn.execute(
            "SELECT COALESCE(MAX(id),0) AS m FROM notifications WHERE user_id=?", (uid,)
        ).fetchone()
        last_id = last_id_row["m"]
        conn.close()
        try:
            while True:
                conn = db.get_db()
                rows = conn.execute(
                    "SELECT * FROM notifications WHERE user_id=? AND id>? ORDER BY id ASC",
                    (uid, last_id)
                ).fetchall()
                conn.close()
                for r in rows:
                    last_id = r["id"]
                    payload = {
                        "type": "notification", "id": r["id"], "title": r["title"],
                        "message": r["message"], "icon": r["icon"], "ntype": r["type"],
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                time.sleep(3)
        except GeneratorExit:
            return

    return Response(gen(), mimetype="text/event-stream")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/api/admin/settings")
@admin_required
def admin_settings():
    conn = db.get_db()
    user_count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    stock_count = conn.execute("SELECT COUNT(*) c FROM stocks").fetchone()["c"]
    order_count = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    today = datetime.now().strftime("%Y-%m-%d")
    today_orders = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE date(created_at)=?", (today,)
    ).fetchone()["c"]
    total_volume = conn.execute("SELECT COALESCE(SUM(volume),0) v FROM stocks").fetchone()["v"]
    conn.close()
    return jsonify({"data": {
        "user_count": user_count, "stock_count": stock_count, "order_count": order_count,
        "today_orders": today_orders, "total_volume": total_volume,
    }})


@app.route("/api/admin/users")
@admin_required
def admin_users():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
    out = []
    for u in rows:
        oc = conn.execute("SELECT COUNT(*) c FROM orders WHERE user_id=?", (u["id"],)).fetchone()["c"]
        hc = conn.execute("SELECT COUNT(*) c FROM holdings WHERE user_id=?", (u["id"],)).fetchone()["c"]
        out.append({
            "id": u["id"], "name": u["name"], "email": u["email"], "phone": u["phone"],
            "is_admin": u["is_admin"], "balance": u["balance"], "available_balance": u["available_balance"],
            "order_count": oc, "holding_count": hc, "created_at": u["created_at"],
        })
    conn.close()
    return jsonify({"data": out})


@app.route("/api/admin/user-logs")
@admin_required
def admin_logs():
    conn = db.get_db()
    rows = conn.execute("""
        SELECT al.*, u.name AS user_name, u.email AS email
        FROM activity_logs al LEFT JOIN users u ON u.id = al.user_id
        ORDER BY al.created_at DESC LIMIT 200
    """).fetchall()
    conn.close()
    return jsonify({"data": [dict(r) for r in rows]})


@app.route("/api/admin/stocks")
@admin_required
def admin_stocks():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM stocks ORDER BY symbol ASC").fetchall()
    conn.close()
    return jsonify({"data": [stock_to_dict(r) for r in rows]})


@app.route("/api/admin/orders")
@admin_required
def admin_orders():
    conn = db.get_db()
    rows = conn.execute("""
        SELECT o.*, u.name AS user_name, u.email AS email
        FROM orders o LEFT JOIN users u ON u.id = o.user_id
        ORDER BY o.created_at DESC LIMIT 300
    """).fetchall()
    conn.close()
    return jsonify({"data": [dict(r) for r in rows]})


@app.route("/api/admin/notify-user", methods=["POST"])
@admin_required
def admin_notify_user():
    data = request.get_json(force=True) or {}
    uid = data.get("user_id")
    title = (data.get("title") or "Admin Notification").strip()
    message = (data.get("message") or "").strip()
    ntype = data.get("type", "info")
    icon = data.get("icon", "🔔")
    if not uid or not message:
        return jsonify({"error": "user_id and message are required"}), 400
    conn = db.get_db()
    target = conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        conn.close()
        return jsonify({"error": "User not found"}), 404
    db.create_notification(conn, uid, title, message, ntype, icon)
    admin = current_user(conn)
    db.log_activity(conn, admin["id"], "ADMIN_NOTIFY", f"To user #{uid}: {title}", request.remote_addr)
    conn.close()
    return jsonify({"data": "sent"})


@app.route("/api/admin/notify-all", methods=["POST"])
@admin_required
def admin_notify_all():
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "Platform Announcement").strip()
    message = (data.get("message") or "").strip()
    ntype = data.get("type", "info")
    icon = data.get("icon", "📢")
    if not message:
        return jsonify({"error": "message is required"}), 400
    conn = db.get_db()
    users = conn.execute("SELECT id FROM users").fetchall()
    for u in users:
        db.create_notification(conn, u["id"], title, message, ntype, icon)
    admin = current_user(conn)
    db.log_activity(conn, admin["id"], "ADMIN_NOTIFY", f"Broadcast: {title}", request.remote_addr)
    conn.close()
    return jsonify({"message": f"Notified {len(users)} users"})


@app.route("/api/admin/refresh-prices", methods=["POST"])
@admin_required
def admin_refresh_prices():
    """Pull live prices for every stock in the DB from Yahoo Finance (best-effort)."""
    conn = db.get_db()
    stocks = conn.execute("SELECT * FROM stocks").fetchall()
    updated, errors = 0, []
    for s in stocks:
        q = fetch_live_quote(s["symbol"], s["exchange"])
        if q:
            conn.execute("""UPDATE stocks SET ltp=?, open=?, high=?, low=?, close=?, prev_close=?,
                             volume=?, change_pct=?, updated_at=datetime('now') WHERE symbol=?""",
                         (q["ltp"], q["open"], q["high"], q["low"], q["close"], q["prev_close"],
                          q["volume"], q["change_pct"], s["symbol"]))
            updated += 1
        else:
            errors.append(s["symbol"])
    admin = current_user(conn)
    db.log_activity(conn, admin["id"], "ADMIN_REFRESH", f"{updated} stocks updated", request.remote_addr)
    conn.commit()
    conn.close()
    note = None if _yf_import_error is None else f"yfinance unavailable: {_yf_import_error}"
    return jsonify({"data": {"updated": updated, "errors": errors, "note": note}})


@app.route("/api/admin/sync-12data", methods=["POST"])
@admin_required
def admin_sync_yfinance():
    """
    Kept the original endpoint name/shape used by the frontend's "12Data"
    panel, but implemented with yfinance (no API key required — the
    'api_key' field from the UI is accepted but ignored).
    """
    data = request.get_json(force=True) or {}
    symbols = data.get("symbols") or []
    conn = db.get_db()
    updated, errors = [], []
    for sym in symbols:
        st = conn.execute("SELECT * FROM stocks WHERE symbol=?", (sym,)).fetchone()
        if not st:
            errors.append(sym)
            continue
        q = fetch_live_quote(st["symbol"], st["exchange"])
        if q:
            conn.execute("""UPDATE stocks SET ltp=?, open=?, high=?, low=?, close=?, prev_close=?,
                             volume=?, change_pct=?, updated_at=datetime('now') WHERE symbol=?""",
                         (q["ltp"], q["open"], q["high"], q["low"], q["close"], q["prev_close"],
                          q["volume"], q["change_pct"], st["symbol"]))
            updated.append({"symbol": sym, "ltp": q["ltp"], "change": q["change_pct"]})
        else:
            errors.append(sym)
    admin = current_user(conn)
    db.log_activity(conn, admin["id"], "ADMIN_12DATA_SYNC",
                     f"{len(updated)} ok / {len(errors)} failed", request.remote_addr)
    conn.commit()
    conn.close()
    msg = f"Synced {len(updated)}/{len(symbols)} symbols via Yahoo Finance"
    if _yf_import_error is not None:
        msg = f"yfinance not installed/available ({_yf_import_error}) — 0 synced"
    return jsonify({
        "message": msg,
        "updated": updated, "errors": errors,
    })


@app.route("/api/admin/check-alerts", methods=["POST"])
@admin_required
def admin_check_alerts():
    """
    Trigger any ACTIVE alert whose condition has been met, evaluated against
    the field the alert was actually created for (Price / RSI / SMA20 /
    EMA50 / Change %) and its operator (>= "crosses up" or <= "crosses down").
    Previously this ALWAYS compared ltp >= target_price regardless of what
    the user picked in the UI, which is why RSI/SMA/EMA alerts never fired.
    """
    conn = db.get_db()
    active = conn.execute("SELECT * FROM alerts WHERE status='ACTIVE'").fetchall()
    count = 0
    for a in active:
        st = conn.execute("SELECT * FROM stocks WHERE symbol=?", (a["symbol"],)).fetchone()
        if not st:
            continue
        field = a["field"] or "price"
        op = a["op"] or "gte"
        cur_val = stock_field_value(st, field)
        if cur_val is None:
            continue
        hit = (cur_val >= a["target_price"]) if op == "gte" else (cur_val <= a["target_price"])
        if hit:
            conn.execute("UPDATE alerts SET status='TRIGGERED', triggered_at=datetime('now') WHERE id=?",
                         (a["id"],))
            label = ALERT_FIELD_LABELS.get(field, field)
            arrow = "≥" if op == "gte" else "≤"
            db.create_notification(
                conn, a["user_id"], "Price Alert Triggered",
                f"{a['symbol']} {label} is {cur_val:.2f} (target {arrow} {a['target_price']:.2f})",
                "alert", "🎯"
            )
            count += 1
    conn.commit()
    conn.close()
    return jsonify({"count": count})


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print()
    print("=" * 60)
    print(" Equity Trade Pro is starting...")
    print(" Demo accounts:")
    print("   leena@gmail.com   / password123")
    print("   admin@equitypro.com / admin123")
    print(" Once you see 'Running on http://127.0.0.1:5000' below,")
    print(" the app is ready — sign-in will work immediately.")
    print(" (yfinance is loaded lazily; it is NOT required to log in")
    print("  or use the app — only for the Admin 'Refresh Prices' button.)")
    print(" Background price simulator is ON: stock prices drift slightly")
    print(" every 5s so Dashboard/Portfolio P&L updates without yfinance.")
    print("=" * 60)
    print()
    start_price_simulator()
    # use_reloader=False avoids double-process startup, which can look
    # like the app is "hanging" or randomly restarting.
    app.run(debug=True, use_reloader=False, threaded=True, host="0.0.0.0", port=5000)
