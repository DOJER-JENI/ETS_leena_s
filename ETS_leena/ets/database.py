"""
database.py
-----------
SQLite database setup and helper functions for Equity Trade Pro.

Usage:
    python database.py      # Creates / resets equity_pro.db with seed data
    import database as db   # Use in app.py
"""

import sqlite3
import os
import math
import random
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash

DB_PATH = "equity_pro.db"


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if they don't exist (safe to call on every startup)."""
    conn = get_db()
    _create_tables(conn)
    conn.commit()
    _migrate_users_kyc(conn)
    _migrate_alerts_conditions(conn)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Table creation
# ---------------------------------------------------------------------------

def _create_tables(conn):
    conn.executescript("""
    -- Users
    CREATE TABLE IF NOT EXISTS users (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        name              TEXT    NOT NULL,
        email             TEXT    NOT NULL UNIQUE,
        phone             TEXT    DEFAULT '',
        password_hash     TEXT    NOT NULL,
        is_admin          INTEGER DEFAULT 0,
        balance           REAL    DEFAULT 100000,
        available_balance REAL    DEFAULT 100000,
        created_at        TEXT    DEFAULT (datetime('now')),
        -- KYC fields
        kyc_status        TEXT    DEFAULT 'PENDING',   -- PENDING / SUBMITTED / VERIFIED
        pan               TEXT    DEFAULT '',
        dob               TEXT    DEFAULT '',
        address            TEXT    DEFAULT '',
        occupation        TEXT    DEFAULT '',
        annual_income     TEXT    DEFAULT '',
        id_proof_type     TEXT    DEFAULT '',
        id_proof_number   TEXT    DEFAULT '',
        kyc_submitted_at  TEXT
    );

    -- Stocks
    CREATE TABLE IF NOT EXISTS stocks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT    NOT NULL UNIQUE,
        name        TEXT    NOT NULL,
        exchange    TEXT    NOT NULL DEFAULT 'NSE',
        sector      TEXT    DEFAULT 'General',
        currency    TEXT    DEFAULT 'INR',
        ltp         REAL    DEFAULT 0,
        open        REAL    DEFAULT 0,
        high        REAL    DEFAULT 0,
        low         REAL    DEFAULT 0,
        close       REAL    DEFAULT 0,
        prev_close  REAL    DEFAULT 0,
        volume      INTEGER DEFAULT 0,
        change_pct  REAL    DEFAULT 0,
        pe          REAL    DEFAULT 0,
        market_cap  REAL    DEFAULT 0,
        div_yield   REAL    DEFAULT 0,
        rsi         REAL    DEFAULT 50,
        sma20       REAL    DEFAULT 0,
        ema50       REAL    DEFAULT 0,
        high52      REAL    DEFAULT 0,
        low52       REAL    DEFAULT 0,
        updated_at  TEXT    DEFAULT (datetime('now'))
    );

    -- Daily OHLCV candles used to draw charts + compute RSI / SMA / EMA server-side.
    -- The most recent row for each symbol is treated as "today" and is refreshed
    -- live by the price simulator so the chart tab shows genuinely live data.
    CREATE TABLE IF NOT EXISTS price_history (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol   TEXT    NOT NULL,
        date     TEXT    NOT NULL,
        open     REAL    NOT NULL,
        high     REAL    NOT NULL,
        low      REAL    NOT NULL,
        close    REAL    NOT NULL,
        volume   INTEGER NOT NULL,
        UNIQUE(symbol, date)
    );

    -- Holdings
    CREATE TABLE IF NOT EXISTS holdings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        symbol      TEXT    NOT NULL,
        quantity    INTEGER DEFAULT 0,
        avg_price   REAL    DEFAULT 0,
        created_at  TEXT    DEFAULT (datetime('now')),
        UNIQUE(user_id, symbol)
    );

    -- Folders (watchlist grouping)
    CREATE TABLE IF NOT EXISTS folders (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        name       TEXT    NOT NULL,
        created_at TEXT    DEFAULT (datetime('now')),
        UNIQUE(user_id, name)
    );

    -- Watchlist
    CREATE TABLE IF NOT EXISTS watchlist (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        folder_id  INTEGER REFERENCES folders(id),
        symbol     TEXT    NOT NULL,
        created_at TEXT    DEFAULT (datetime('now')),
        UNIQUE(user_id, folder_id, symbol)
    );

    -- Orders
    CREATE TABLE IF NOT EXISTS orders (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id   TEXT    NOT NULL UNIQUE,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        symbol     TEXT    NOT NULL,
        order_type TEXT    NOT NULL,   -- BUY / SELL
        order_mode TEXT    DEFAULT 'Market',
        quantity   INTEGER NOT NULL,
        price      REAL    NOT NULL,
        duration   TEXT    DEFAULT 'DAY',
        status     TEXT    DEFAULT 'NEW',
        created_at TEXT    DEFAULT (datetime('now'))
    );

    -- Transactions (completed fills)
    CREATE TABLE IF NOT EXISTS transactions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL REFERENCES users(id),
        type          TEXT    NOT NULL,   -- BUY / SELL
        symbol        TEXT    NOT NULL,
        quantity      INTEGER NOT NULL,
        price         REAL    NOT NULL,
        amount        REAL    NOT NULL,
        commission    REAL    DEFAULT 0,
        exchange_fee  REAL    DEFAULT 0,
        gst           REAL    DEFAULT 0,
        total_charges REAL    DEFAULT 0,
        created_at    TEXT    DEFAULT (datetime('now'))
    );

    -- Price Alerts
    CREATE TABLE IF NOT EXISTS alerts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL REFERENCES users(id),
        symbol       TEXT    NOT NULL,
        field        TEXT    DEFAULT 'price',   -- price / rsi / sma20 / ema50 / change
        op           TEXT    DEFAULT 'gte',      -- gte (crosses up) / lte (crosses down)
        target_price REAL    NOT NULL,
        status       TEXT    DEFAULT 'ACTIVE',   -- ACTIVE / TRIGGERED
        triggered_at TEXT,
        created_at   TEXT    DEFAULT (datetime('now'))
    );

    -- In-app Notifications
    CREATE TABLE IF NOT EXISTS notifications (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        title      TEXT    NOT NULL,
        message    TEXT    NOT NULL,
        type       TEXT    DEFAULT 'info',   -- info / success / alert / warning
        icon       TEXT    DEFAULT '🔔',
        is_read    INTEGER DEFAULT 0,
        created_at TEXT    DEFAULT (datetime('now'))
    );

    -- Activity / Audit Logs
    CREATE TABLE IF NOT EXISTS activity_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER REFERENCES users(id),
        action     TEXT    NOT NULL,
        details    TEXT    DEFAULT '',
        ip         TEXT    DEFAULT '',
        created_at TEXT    DEFAULT (datetime('now'))
    );
    """)


def _migrate_users_kyc(conn):
    """Adds KYC columns to an already-existing users table (older DB files)."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    wanted = {
        "kyc_status": "TEXT DEFAULT 'PENDING'",
        "pan": "TEXT DEFAULT ''",
        "dob": "TEXT DEFAULT ''",
        "address": "TEXT DEFAULT ''",
        "occupation": "TEXT DEFAULT ''",
        "annual_income": "TEXT DEFAULT ''",
        "id_proof_type": "TEXT DEFAULT ''",
        "id_proof_number": "TEXT DEFAULT ''",
        "kyc_submitted_at": "TEXT",
    }
    for col, decl in wanted.items():
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass


def _migrate_alerts_conditions(conn):
    """Adds 'field' and 'op' columns to an already-existing alerts table
    (older DB files created before RSI/SMA20/EMA50/Change% alert conditions
    existed). Without this, alerts created before the update would keep
    showing 'condition: undefined' since the columns wouldn't exist yet."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    if "field" not in cols:
        try:
            conn.execute("ALTER TABLE alerts ADD COLUMN field TEXT DEFAULT 'price'")
        except sqlite3.OperationalError:
            pass
    if "op" not in cols:
        try:
            conn.execute("ALTER TABLE alerts ADD COLUMN op TEXT DEFAULT 'gte'")
        except sqlite3.OperationalError:
            pass


# ---------------------------------------------------------------------------
# Seed data — 75 stocks across NSE / NASDAQ / NYSE sectors
# ---------------------------------------------------------------------------

SEED_STOCKS = [
    # symbol, name, exchange, sector, currency, ltp, open, high, low, close, prev_close, volume, change_pct, pe, market_cap, div_yield, rsi, sma20, ema50, high52, low52

    # --- NSE : IT ---
    ("TCS",        "Tata Consultancy Services",  "NSE",    "IT",         "INR", 3842.50, 3820.00, 3875.00, 3810.00, 3840.00, 3815.00,  2845620, 0.72,  32.5, 1405000, 1.25, 55.2, 3830.0, 3810.0, 4120.0, 3180.0),
    ("INFY",       "Infosys Limited",             "NSE",    "IT",         "INR", 1585.30, 1575.00, 1598.00, 1570.00, 1580.00, 1572.00,  3521480, 0.46,  28.8,  660000, 1.45, 48.7, 1578.0, 1560.0, 1720.0, 1340.0),
    ("WIPRO",      "Wipro Limited",               "NSE",    "IT",         "INR",  452.80,  448.00,  458.00,  445.00,  450.00,  448.00,  5892340, 0.85,  24.2,  237000, 0.35, 62.3,  450.0,  445.0,  510.0,  380.0),
    ("HCLTECH",    "HCL Technologies",            "NSE",    "IT",         "INR", 1412.65, 1400.00, 1425.00, 1395.00, 1408.00, 1400.00,  1823450, 0.33,  26.1,  382000, 1.10, 51.8, 1405.0, 1395.0, 1560.0, 1180.0),
    ("TECHM",      "Tech Mahindra Limited",       "NSE",    "IT",         "INR", 1620.40, 1605.00, 1635.00, 1598.00, 1612.00, 1608.00,  1512300, 0.75,  22.4,  158000, 1.65, 57.1, 1610.0, 1595.0, 1780.0, 1120.0),
    ("LTIM",       "LTIMindtree Limited",         "NSE",    "IT",         "INR", 5620.10, 5580.00, 5660.00, 5560.00, 5600.00, 5590.00,   612300, 0.54,  30.1,  166000, 0.90, 49.5, 5595.0, 5560.0, 6100.0, 4300.0),
    ("PERSISTENT", "Persistent Systems",          "NSE",    "IT",         "INR", 5480.20, 5420.00, 5520.00, 5390.00, 5460.00, 5440.00,   382000, 0.74,  40.2,   84000, 0.60, 60.5, 5450.0, 5400.0, 6900.0, 3900.0),
    ("COFORGE",    "Coforge Limited",             "NSE",    "IT",         "INR", 6320.40, 6260.00, 6380.00, 6220.00, 6300.00, 6280.00,   242000, 0.64,  38.6,   42000, 0.55, 58.2, 6290.0, 6240.0, 8300.0, 4600.0),

    # --- NSE : Energy ---
    ("RELIANCE",   "Reliance Industries",         "NSE",    "Energy",     "INR", 2523.45, 2500.00, 2548.00, 2495.00, 2510.00, 2500.00,  4523680, 0.54,  28.2, 1720000, 0.30, 58.1, 2510.0, 2490.0, 2680.0, 2120.0),
    ("ONGC",       "Oil & Natural Gas Corp",      "NSE",    "Energy",     "INR",  268.30,  265.00,  272.00,  263.00,  266.00,  265.20,  8234500, 0.42,   9.1,  337000, 4.10, 46.2,  265.0,  262.0,  345.0,  178.0),
    ("NTPC",       "NTPC Limited",                "NSE",    "Energy",     "INR",  368.90,  365.00,  372.00,  362.00,  366.00,  365.50,  6234500, 0.68,  15.8,  357000, 2.20, 53.8,  365.0,  361.0,  420.0,  225.0),
    ("POWERGRID",  "Power Grid Corporation",      "NSE",    "Energy",     "INR",  312.60,  308.00,  316.00,  305.00,  310.00,  309.20,  5234000, 0.45,  17.6,  291000, 3.30, 50.1,  310.0,  306.0,  365.0,  240.0),
    ("BPCL",       "Bharat Petroleum Corp",       "NSE",    "Energy",     "INR",  312.80,  308.00,  318.00,  305.00,  310.00,  309.00,  4234000, 0.58,  10.4,  135000, 3.80, 55.6,  309.0,  305.0,  370.0,  220.0),
    ("COALINDIA",  "Coal India Limited",          "NSE",    "Energy",     "INR",  452.30,  446.00,  458.00,  442.00,  449.00,  447.50,  4823000, 1.06,   7.2,  279000, 5.20, 62.8,  447.0,  440.0,  545.0,  360.0),

    # --- NSE : Banking ---
    ("HDFCBANK",   "HDFC Bank Limited",           "NSE",    "Banking",    "INR", 1654.30, 1645.00, 1668.00, 1640.00, 1650.00, 1642.00,  3214560, 0.26,  19.8, 1250000, 1.10, 45.3, 1648.0, 1640.0, 1780.0, 1420.0),
    ("ICICIBANK",  "ICICI Bank Limited",          "NSE",    "Banking",    "INR", 1052.80, 1045.00, 1062.00, 1040.00, 1048.00, 1045.00,  5623480, 0.37,  18.5,  735000, 0.95, 52.6, 1048.0, 1040.0, 1120.0,  880.0),
    ("SBI",        "State Bank of India",         "NSE",    "Banking",    "INR",  624.50,  620.00,  632.00,  618.00,  622.00,  620.00,  8923450, 0.40,  10.2,  558000, 1.80, 47.8,  622.0,  618.0,  680.0,  520.0),
    ("AXISBANK",   "Axis Bank Limited",           "NSE",    "Banking",    "INR", 1045.60, 1038.00, 1055.00, 1032.00, 1042.00, 1040.00,  4523680, 0.54,  15.2,  321000, 0.95, 53.2, 1042.0, 1035.0, 1120.0,  890.0),
    ("KOTAKBANK",  "Kotak Mahindra Bank",         "NSE",    "Banking",    "INR", 1782.20, 1770.00, 1798.00, 1762.00, 1775.00, 1772.00,  1923000, 0.57,  21.4,  354000, 0.75, 50.9, 1774.0, 1765.0, 1980.0, 1540.0),
    ("INDUSINDBK", "IndusInd Bank Limited",       "NSE",    "Banking",    "INR", 1024.60, 1010.00, 1040.00, 1002.00, 1015.00, 1012.00,  2412300, 1.24,  12.6,   79800, 1.30, 61.5, 1012.0, 1000.0, 1690.0,  920.0),
    ("PNB",        "Punjab National Bank",        "NSE",    "Banking",    "INR",  108.40,  106.50,  110.20,  105.80,  107.60,  107.00,  9823400, 1.31,   9.8,  118000, 1.10, 59.4,  107.0,  105.0,  142.0,   85.0),
    ("BANKBARODA", "Bank of Baroda",              "NSE",    "Banking",    "INR",  242.60,  239.00,  246.00,  236.50,  240.80,  240.00,  6234000, 1.08,   7.1,  126000, 3.40, 57.9,  240.0,  236.0,  305.0,  180.0),

    # --- NSE : Auto ---
    ("TATAMOTORS", "Tata Motors Limited",         "NSE",    "Auto",       "INR",  652.40,  645.00,  660.00,  642.00,  648.00,  645.00,  9823450, 0.68,   8.2,  238000, 0.45, 65.4,  650.0,  642.0,  720.0,  480.0),
    ("MARUTI",     "Maruti Suzuki India",         "NSE",    "Auto",       "INR",10542.30,10480.00,10620.00,10450.00,10510.00,10495.00,   823450, 0.45,  25.8,  318000, 1.20, 50.4,10510.0,10490.0,11200.0, 9100.0),
    ("M&M",        "Mahindra & Mahindra",         "NSE",    "Auto",       "INR", 2856.70, 2820.00, 2880.00, 2810.00, 2840.00, 2835.00,  1523000, 0.76,  27.3,  355000, 0.65, 59.2, 2838.0, 2810.0, 3100.0, 1750.0),
    ("BAJAJ-AUTO", "Bajaj Auto Limited",          "NSE",    "Auto",       "INR", 9240.50, 9180.00, 9310.00, 9150.00, 9200.00, 9195.00,   412000, 0.49,  32.9,  260000, 1.10, 55.0, 9195.0, 9150.0, 9700.0, 4700.0),
    ("HEROMOTOCO", "Hero MotoCorp Limited",       "NSE",    "Auto",       "INR", 4820.30, 4770.00, 4860.00, 4750.00, 4800.00, 4790.00,   612000, 0.63,  20.9,   96000, 2.60, 52.3, 4795.0, 4770.0, 5900.0, 3600.0),
    ("EICHERMOT",  "Eicher Motors Limited",       "NSE",    "Auto",       "INR", 4620.80, 4560.00, 4670.00, 4540.00, 4600.00, 4590.00,   382000, 0.67,  33.4,  126000, 0.80, 56.7, 4595.0, 4560.0, 5400.0, 3600.0),

    # --- NSE : FMCG ---
    ("ITC",        "ITC Limited",                 "NSE",    "FMCG",       "INR",  442.85,  440.00,  448.00,  438.00,  441.00,  440.00, 15234560, 0.42,  26.8,  550000, 2.80, 54.2,  441.0,  438.0,  480.0,  380.0),
    ("HINDUNILVR", "Hindustan Unilever",          "NSE",    "FMCG",       "INR", 2412.30, 2395.00, 2430.00, 2385.00, 2400.00, 2398.00,  1234500, 0.60,  54.2,  567000, 1.65, 44.8, 2398.0, 2390.0, 2770.0, 2170.0),
    ("NESTLEIND",  "Nestle India Limited",        "NSE",    "FMCG",       "INR", 2385.10, 2360.00, 2400.00, 2350.00, 2370.00, 2368.00,   412300, 0.72,  62.1,  230000, 1.05, 48.5, 2366.0, 2355.0, 2770.0, 2200.0),
    ("BRITANNIA",  "Britannia Industries",        "NSE",    "FMCG",       "INR", 4980.60, 4920.00, 5020.00, 4900.00, 4960.00, 4950.00,   282000, 0.62,  46.8,  120000, 1.30, 51.2, 4955.0, 4920.0, 5900.0, 4400.0),
    ("DABUR",      "Dabur India Limited",         "NSE",    "FMCG",       "INR",  562.40,  556.00,  568.00,  552.00,  559.00,  558.00,  1823000, 0.43,  42.6,   99000, 1.20, 47.9,  558.0,  554.0,  650.0,  480.0),

    # --- NSE : Pharma ---
    ("SUNPHARMA",  "Sun Pharma Industries",       "NSE",    "Pharma",     "INR", 1205.80, 1195.00, 1218.00, 1190.00, 1200.00, 1195.00,  2823450, 0.48,  35.2,  290000, 0.85, 47.3, 1200.0, 1190.0, 1320.0,  980.0),
    ("DRREDDY",    "Dr Reddy's Laboratories",     "NSE",    "Pharma",     "INR", 6120.40, 6060.00, 6180.00, 6040.00, 6080.00, 6075.00,   612300, 0.74,  20.6,  102000, 0.65, 52.7, 6078.0, 6050.0, 7440.0, 5200.0),
    ("CIPLA",      "Cipla Limited",               "NSE",    "Pharma",     "INR", 1512.60, 1498.00, 1525.00, 1490.00, 1505.00, 1500.00,   923000, 0.83,  25.3,  122000, 0.75, 56.4, 1503.0, 1495.0, 1720.0, 1200.0),
    ("DIVISLAB",   "Divi's Laboratories",         "NSE",    "Pharma",     "INR", 4820.30, 4760.00, 4870.00, 4740.00, 4800.00, 4790.00,   382000, 0.63,  58.4,  128000, 0.55, 60.2, 4795.0, 4760.0, 6200.0, 3400.0),
    ("APOLLOHOSP", "Apollo Hospitals Enterprise", "NSE",    "Pharma",     "INR", 7120.40, 7040.00, 7190.00, 7010.00, 7080.00, 7060.00,   212000, 0.85,  68.5,  103000, 0.20, 63.8, 7075.0, 7020.0, 7800.0, 5200.0),

    # --- NSE : Metal ---
    ("TATASTEEL",  "Tata Steel Limited",          "NSE",    "Metal",      "INR",  142.35,  140.00,  145.00,  139.00,  141.00,  140.00, 25234560, 1.68,   8.5,  176000, 1.20, 68.5,  141.0,  139.0,  158.0,  105.0),
    ("HINDALCO",   "Hindalco Industries",         "NSE",    "Metal",      "INR",  652.80,  645.00,  660.00,  640.00,  648.00,  646.00,  4123000, 0.31,  10.9,  147000, 0.85, 61.2,  648.0,  640.0,  720.0,  430.0),
    ("JSWSTEEL",   "JSW Steel Limited",           "NSE",    "Metal",      "INR",  912.40,  902.00,  920.00,  895.00,  905.00,  903.00,  2823000, 1.03,  18.7,  222000, 1.05, 64.7,  905.0,  895.0,  980.0,  700.0),
    ("VEDL",       "Vedanta Limited",             "NSE",    "Metal",      "INR",  432.60,  426.00,  438.00,  422.00,  429.00,  428.00,  7234000, 0.93,  14.2,  161000, 6.50, 66.1,  428.0,  422.0,  520.0,  260.0),
    ("SAIL",       "Steel Authority of India",    "NSE",    "Metal",      "INR",  128.40,  126.00,  131.00,  124.50,  127.20,  126.80,  9823000, 1.26,  11.6,   53000, 1.60, 63.3,  127.0,  124.0,  175.0,   95.0),

    # --- NSE : Finance ---
    ("BAJFINANCE", "Bajaj Finance Limited",       "NSE",    "Finance",    "INR", 6842.50, 6800.00, 6900.00, 6780.00, 6820.00, 6810.00,  1234560, 0.47,  32.8,  412000, 0.40, 49.6, 6830.0, 6810.0, 7200.0, 5600.0),
    ("BAJAJFINSV", "Bajaj Finserv Limited",       "NSE",    "Finance",    "INR", 1642.30, 1625.00, 1655.00, 1618.00, 1630.00, 1628.00,   923000, 0.86,  28.5,  262000, 0.60, 54.3, 1629.0, 1618.0, 1780.0, 1400.0),
    ("HDFCLIFE",   "HDFC Life Insurance",         "NSE",    "Finance",    "INR",  650.40,  644.00,  658.00,  640.00,  647.00,  646.00,  2234000, 0.62,  76.4,  140000, 0.45, 46.5,  646.0,  642.0,  760.0,  520.0),
    ("SBILIFE",    "SBI Life Insurance",          "NSE",    "Finance",    "INR", 1512.60, 1495.00, 1528.00, 1488.00, 1508.00, 1502.00,   482000, 0.66,  62.8,  151000, 0.25, 52.9, 1505.0, 1495.0, 1780.0, 1200.0),
    ("MUTHOOTFIN", "Muthoot Finance Limited",     "NSE",    "Finance",    "INR", 2120.60, 2080.00, 2150.00, 2060.00, 2100.00, 2090.00,   382000, 1.44,  16.9,   85000, 1.20, 61.8, 2095.0, 2070.0, 2500.0, 1300.0),

    # --- NSE : Infra ---
    ("LT",         "Larsen & Toubro",             "NSE",    "Infra",      "INR", 3285.40, 3260.00, 3310.00, 3250.00, 3275.00, 3265.00,  1823450, 0.62,  30.5,  462000, 1.00, 56.8, 3270.0, 3250.0, 3520.0, 2780.0),
    ("ADANIPORTS", "Adani Ports & SEZ",           "NSE",    "Infra",      "INR", 1382.60, 1365.00, 1398.00, 1358.00, 1372.00, 1368.00,  2123000, 1.06,  24.6,  298000, 0.35, 63.8, 1370.0, 1358.0, 1620.0,  980.0),
    ("ULTRACEMCO", "UltraTech Cement",            "NSE",    "Infra",      "INR",11240.50,11150.00,11320.00,11100.00,11200.00,11180.00,   312000, 0.54,  34.2,  324000, 0.55, 51.4,11190.0,11150.0,12500.0, 8600.0),
    ("GRASIM",     "Grasim Industries",           "NSE",    "Infra",      "INR", 2620.40, 2590.00, 2650.00, 2580.00, 2610.00, 2600.00,   612000, 0.77,  22.6,  173000, 0.75, 55.1, 2605.0, 2585.0, 2900.0, 1900.0),
    ("AMBUJACEM",  "Ambuja Cements Limited",      "NSE",    "Infra",      "INR",  588.30,  580.00,  595.00,  575.00,  584.00,  582.00,  2823000, 1.03,  28.4,  116000, 0.60, 58.7,  583.0,  578.0,  680.0,  420.0),

    # --- NSE : Telecom ---
    ("BHARTIARTL", "Bharti Airtel Limited",       "NSE",    "Telecom",    "INR", 1582.40, 1568.00, 1595.00, 1560.00, 1575.00, 1572.00,  3123000, 0.66,  42.1,  945000, 0.45, 58.9, 1573.0, 1560.0, 1750.0, 1150.0),
    ("IDEA",       "Vodafone Idea Limited",       "NSE",    "Telecom",    "INR",   12.85,   12.50,   13.20,   12.30,   12.70,   12.60, 98234500, 1.98,   0.0,   90000, 0.00, 69.4,   12.6,   12.2,   19.2,    6.5),

    # --- NSE : Consumer ---
    ("ASIANPAINT", "Asian Paints Limited",        "NSE",    "Consumer",   "INR", 2842.60, 2815.00, 2865.00, 2805.00, 2825.00, 2820.00,   812300, 0.80,  48.5,  272000, 0.90, 46.2, 2822.0, 2810.0, 3420.0, 2650.0),
    ("TITAN",      "Titan Company Limited",       "NSE",    "Consumer",   "INR", 3412.80, 3380.00, 3440.00, 3365.00, 3395.00, 3390.00,   923000, 0.65,  62.4,  303000, 0.30, 54.1, 3392.0, 3370.0, 3900.0, 3020.0),
    ("TRENT",      "Trent Limited",               "NSE",    "Consumer",   "INR", 5620.40, 5540.00, 5680.00, 5510.00, 5600.00, 5580.00,   382000, 0.72,  92.5,  199000, 0.10, 66.9, 5595.0, 5540.0, 8300.0, 3800.0),
    ("DMART",      "Avenue Supermarts (DMart)",   "NSE",    "Consumer",   "INR", 3920.60, 3870.00, 3960.00, 3850.00, 3900.00, 3890.00,   282000, 0.79,  78.2,  254000, 0.00, 49.3, 3895.0, 3865.0, 5500.0, 3200.0),

    # --- NSE : Realty / Media ---
    ("DLF",        "DLF Limited",                 "NSE",    "Realty",     "INR",  812.40,  800.00,  822.00,  794.00,  808.00,  805.00,  4234000, 0.87,  38.6,  201000, 0.65, 61.4,  807.0,  800.0,  930.0,  600.0),
    ("ZEEL",       "Zee Entertainment Enterprises","NSE",   "Media",      "INR",  128.60,  125.00,  132.00,  123.50,  127.00,  126.20,  9234000, 1.90,  22.4,   12300, 0.00, 57.8,  126.5,  123.0,  185.0,   95.0),

    # --- NASDAQ / NYSE : Technology ---
    ("AAPL",       "Apple Inc.",                  "NASDAQ", "Technology", "USD",  192.53,  191.00,  194.50,  190.20,  191.80,  191.20, 54236000, 0.37,  31.2, 2980000, 0.55, 58.3,  191.5,  190.5,  210.0,  152.0),
    ("GOOGL",      "Alphabet Inc.",               "NASDAQ", "Technology", "USD",  176.42,  175.00,  178.50,  174.10,  175.80,  175.20, 28456000, 0.35,  26.8, 2180000, 0.00, 52.1,  175.5,  174.5,  185.0,  138.0),
    ("MSFT",       "Microsoft Corporation",       "NASDAQ", "Technology", "USD",  421.35,  418.00,  425.00,  416.80,  419.50,  418.80, 22145000, 0.44,  37.5, 3128000, 0.72, 46.8,  419.0,  417.5,  445.0,  350.0),
    ("NVDA",       "NVIDIA Corporation",          "NASDAQ", "Technology", "USD",  882.56,  875.00,  892.00,  870.50,  878.20,  875.80, 42236000, 0.48,  72.8, 2170000, 0.02, 64.5,  878.0,  873.5,  920.0,  450.0),
    ("AMZN",       "Amazon.com Inc.",             "NASDAQ", "Technology", "USD",  185.25,  183.00,  187.50,  182.30,  184.50,  183.80, 35124000, 0.79,  45.2, 1940000, 0.00, 61.3,  184.0,  182.5,  198.0,  148.0),
    ("META",       "Meta Platforms Inc.",         "NASDAQ", "Technology", "USD",  521.84,  518.00,  526.50,  515.80,  520.40,  519.20, 18923000, 0.51,  28.4, 1320000, 0.00, 55.7,  520.0,  517.5,  545.0,  420.0),
    ("NFLX",       "Netflix Inc.",                "NASDAQ", "Technology", "USD",  682.40,  675.00,  690.00,  670.00,  678.00,  676.50,  8234000, 0.85,  44.6,  295000, 0.00, 60.1,  677.0,  670.0,  720.0,  480.0),
    ("AMD",        "Advanced Micro Devices",      "NASDAQ", "Technology", "USD",  158.60,  156.00,  161.00,  154.50,  157.00,  156.80, 41234000, 1.15, 102.3,  256000, 0.00, 66.8,  156.5,  153.0,  180.0,   93.0),
    ("INTC",       "Intel Corporation",           "NASDAQ", "Technology", "USD",   32.80,   32.20,   33.30,   31.90,   32.50,   32.40, 62234000, 0.93,  21.4,  140000, 1.60, 41.5,   32.4,   32.0,   45.0,   19.0),
    ("ADBE",       "Adobe Inc.",                  "NASDAQ", "Technology", "USD",  512.40,  505.00,  518.00,  502.00,  508.00,  506.50, 3234000, 0.37,  38.6,  228000, 0.00, 47.9,  507.0,  504.0,  640.0,  420.0),
    ("CRM",        "Salesforce Inc.",             "NYSE",   "Technology", "USD",  268.40,  264.00,  272.00,  262.00,  266.00,  265.00, 5234000, 0.53,  42.1,  256000, 0.60, 52.6,  265.5,  262.0,  318.0,  195.0),
    ("ORCL",       "Oracle Corporation",          "NYSE",   "Technology", "USD",  142.60,  140.00,  144.50,  138.80,  141.00,  140.20, 9234000, 1.71,  33.6,  392000, 1.30, 63.2,  140.8,  138.5,  175.0,  100.0),
    ("IBM",        "International Business Machines","NYSE","Technology", "USD",  198.40,  195.00,  201.00,  193.50,  196.50,  195.80, 4234000, 1.33,  24.8,  180000, 3.20, 57.4,  196.0,  194.0,  230.0,  145.0),
    ("PYPL",       "PayPal Holdings Inc.",        "NASDAQ", "Technology", "USD",   74.60,   73.20,   75.80,   72.50,   73.90,   73.50, 12234000, 1.50,  17.9,   78000, 0.00, 54.8,   73.7,   72.8,   92.0,   55.0),
    ("TSLA",       "Tesla Inc.",                  "NASDAQ", "Auto",       "USD",  248.42,  245.00,  252.00,  243.50,  246.80,  245.20, 95236000, 1.04,  78.5,  790000, 0.00, 68.2,  246.5,  244.0,  280.0,  180.0),

    # --- NYSE : Banking / Finance ---
    ("JPM",        "JPMorgan Chase & Co.",        "NYSE",   "Banking",    "USD",  212.40,  210.00,  214.50,  208.50,  211.00,  210.80,  9234000, 0.76,  13.2,  608000, 2.20, 55.6,  210.5,  208.0,  225.0,  158.0),
    ("V",          "Visa Inc.",                   "NYSE",   "Finance",    "USD",  288.60,  285.00,  291.00,  283.50,  286.50,  286.00,  6123000, 0.91,  31.5,  595000, 0.75, 57.2,  286.0,  283.5,  310.0,  248.0),
    ("MA",         "Mastercard Incorporated",     "NYSE",   "Finance",    "USD",  478.60,  472.00,  484.00,  468.50,  475.00,  474.20, 3234000, 0.93,  36.8,  445000, 0.55, 58.6,  475.5,  471.0,  520.0,  400.0),
    ("BAC",        "Bank of America Corp",        "NYSE",   "Banking",    "USD",   39.40,   38.80,   39.90,   38.40,   39.00,   38.90, 42234000, 1.29,  12.4,  312000, 2.40, 52.3,   39.0,   38.6,   45.0,   28.0),

    # --- NYSE : Consumer / Pharma / Energy ---
    ("KO",         "The Coca-Cola Company",       "NYSE",   "Consumer",   "USD",   63.40,   62.80,   63.90,   62.40,   63.00,   62.90,  12234000, 0.79,  25.6,  273000, 2.90, 49.8,   62.9,   62.5,   68.0,   54.0),
    ("PEP",        "PepsiCo Inc.",                "NASDAQ", "Consumer",   "USD",  172.40,  170.20,  174.50,  169.00,  171.50,  170.80,  5234000, 0.94,  23.4,  236000, 2.90, 51.6,  171.0,  169.5,  195.0,  155.0),
    ("WMT",        "Walmart Inc.",                "NYSE",   "Consumer",   "USD",   68.40,   67.60,   69.10,   67.10,   68.00,   67.80,  16234000, 0.88,  32.8,  550000, 1.00, 60.4,   67.9,   67.3,   78.0,   52.0),
    ("MCD",        "McDonald's Corporation",      "NYSE",   "Consumer",   "USD",  296.40,  292.00,  299.50,  290.50,  294.50,  293.80,  3234000, 0.89,  24.6,  213000, 2.30, 48.2,  294.0,  291.5,  320.0,  255.0),
    ("NKE",        "Nike Inc.",                   "NYSE",   "Consumer",   "USD",   78.60,   77.20,   79.80,   76.50,   78.00,   77.60, 8234000, 1.29,  26.4,  118000, 2.00, 44.5,   77.9,   77.0,  105.0,   68.0),
    ("DIS",        "The Walt Disney Company",     "NYSE",   "Consumer",   "USD",  108.60,  107.00,  109.80,  106.20,  107.80,  107.40, 10234000, 1.12,  22.9,  198000, 0.90, 62.4,  107.5,  106.0,  125.0,   83.0),
    ("JNJ",        "Johnson & Johnson",           "NYSE",   "Pharma",     "USD",  158.40,  156.80,  159.80,  155.90,  157.50,  157.00,  6234000, 0.89,  16.2,  382000, 3.10, 47.6,  157.2,  156.0,  170.0,  140.0),
    ("PFE",        "Pfizer Inc.",                 "NYSE",   "Pharma",     "USD",   28.60,   28.10,   29.00,   27.80,   28.30,   28.20,  32234000, 1.42,  14.6,  162000, 6.30, 43.8,   28.2,   28.0,   32.0,   24.0),
    ("XOM",        "Exxon Mobil Corporation",     "NYSE",   "Energy",     "USD",  118.40,  116.80,  119.80,  115.90,  117.50,  117.00,  14234000, 0.85,  13.4,  468000, 3.30, 52.9,  117.2,  116.0,  128.0,   98.0),
    ("CVX",        "Chevron Corporation",         "NYSE",   "Energy",     "USD",  158.40,  156.20,  160.00,  155.00,  157.20,  156.80,  8234000, 1.02,  14.8,  292000, 4.10, 54.6,  157.0,  155.5,  172.0,  140.0),
    ("BA",         "The Boeing Company",          "NYSE",   "Industrial", "USD",  178.60,  175.00,  181.00,  173.50,  177.00,  176.20,  9234000, 1.36,   0.0,  108000, 0.00, 58.9,  176.5,  174.0,  210.0,  130.0),
]


def _rand_walk_history(seed_price, days=760):
    """Generate a deterministic-ish random walk of daily OHLCV candles ending
    roughly at seed_price, used to seed price_history for chart data."""
    data = []
    price = seed_price * random.uniform(0.65, 0.85)
    start = datetime.now() - timedelta(days=days)
    for i in range(days):
        date = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        drift = (seed_price - price) / max(1, (days - i)) * 0.35
        noise = price * random.uniform(-0.018, 0.018)
        close = max(0.5, price + drift + noise)
        o = price * random.uniform(0.996, 1.004)
        h = max(o, close) * random.uniform(1.0, 1.012)
        l = min(o, close) * random.uniform(0.988, 1.0)
        vol = int(random.uniform(0.5, 1.5) * 1_000_000)
        data.append((date, round(o, 2), round(h, 2), round(l, 2), round(close, 2), vol))
        price = close
    return data


def seed_price_history(conn):
    """Populate price_history with ~2 years of daily candles for every stock,
    only if that symbol has no history yet (idempotent, cheap to re-run)."""
    stocks = conn.execute("SELECT symbol, ltp, volume FROM stocks").fetchall()
    for s in stocks:
        has = conn.execute("SELECT COUNT(*) c FROM price_history WHERE symbol=?", (s["symbol"],)).fetchone()["c"]
        if has > 0:
            continue
        rows = _rand_walk_history(s["ltp"])
        for (date, o, h, l, c, vol) in rows:
            conn.execute(
                "INSERT OR IGNORE INTO price_history (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
                (s["symbol"], date, o, h, l, c, vol)
            )
    conn.commit()


def sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        out[i] = sum(window) / period
    return out


def ema(values, period):
    out = [None] * len(values)
    k = 2 / (period + 1)
    prev = None
    for i, v in enumerate(values):
        if i < period - 1:
            continue
        if prev is None:
            prev = sum(values[i - period + 1:i + 1]) / period
        else:
            prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values, period=14):
    out = [None] * len(values)
    if len(values) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains) + 1):
        if i > period:
            g = gains[i - 1]
            l = losses[i - 1]
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + l) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        val = 100 - (100 / (1 + rs)) if avg_loss != 0 else 100
        out[i] = round(val, 2)
    return out


def get_history_with_indicators(conn, symbol, days=180):
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM price_history WHERE symbol=? ORDER BY date ASC",
        (symbol,)
    ).fetchall()
    if not rows:
        return None
    rows = rows[-days:]
    closes = [r["close"] for r in rows]
    sma20 = sma(closes, 20)
    ema50 = ema(closes, 50)
    rsi14 = rsi(closes, 14)
    out = []
    for i, r in enumerate(rows):
        out.append({
            "date": r["date"], "open": r["open"], "high": r["high"], "low": r["low"],
            "close": r["close"], "volume": r["volume"],
            "sma20": round(sma20[i], 2) if sma20[i] is not None else None,
            "ema50": round(ema50[i], 2) if ema50[i] is not None else None,
            "rsi": rsi14[i],
        })
    return out


def update_today_candle(conn, symbol, ltp, volume):
    """Called by the live price simulator — updates (or creates) today's
    candle so the chart / RSI / SMA / EMA reflect the live price."""
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute("SELECT * FROM price_history WHERE symbol=? AND date=?", (symbol, today)).fetchone()
    if row:
        new_high = max(row["high"], ltp)
        new_low = min(row["low"], ltp) if row["low"] else ltp
        conn.execute(
            "UPDATE price_history SET close=?, high=?, low=?, volume=? WHERE id=?",
            (ltp, new_high, new_low, volume, row["id"])
        )
    else:
        conn.execute(
            "INSERT INTO price_history (symbol,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
            (symbol, today, ltp, ltp, ltp, ltp, volume)
        )


def seed_db():
    """
    Insert seed data into a freshly created database.
    Skips if users already exist (idempotent).
    """
    conn = get_db()

    # Skip if already seeded
    existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing > 0:
        # Still make sure new stocks / history exist if the schema/seed grew
        _ensure_stocks(conn)
        seed_price_history(conn)
        conn.close()
        print("[DB] Already seeded — ensured stocks/history are up to date.")
        return

    print("[DB] Seeding database…")

    # --- Admin user ---
    admin_hash = generate_password_hash("admin123")
    conn.execute(
        "INSERT INTO users (name, email, phone, password_hash, is_admin, balance, available_balance) VALUES (?,?,?,?,?,?,?)",
        ("Admin User", "admin@equitypro.com", "9000000000", admin_hash, 1, 10000000, 10000000)
    )
    admin_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO folders (user_id, name) VALUES (?, 'Default')", (admin_id,))

    # --- Demo user ---
    user_hash = generate_password_hash("password123")
    conn.execute(
        "INSERT INTO users (name, email, phone, password_hash, is_admin, balance, available_balance, "
        "kyc_status, pan, dob, address, occupation, annual_income, id_proof_type, id_proof_number, kyc_submitted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("Leena Sharma", "leena@gmail.com", "9876543210", user_hash, 0, 250000, 180450,
         "VERIFIED", "ABCDE1234F", "1994-06-12", "MG Road, Bengaluru, Karnataka",
         "Software Professional", "12,00,000 - 25,00,000", "Aadhaar", "XXXX-XXXX-4821",
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO folders (user_id, name) VALUES (?, 'Default')", (user_id,))
    conn.execute("INSERT INTO folders (user_id, name) VALUES (?, 'Tech Stocks')", (user_id,))
    folder_id = conn.execute("SELECT id FROM folders WHERE user_id=? AND name='Default'", (user_id,)).fetchone()[0]
    folder2_id = conn.execute("SELECT id FROM folders WHERE user_id=? AND name='Tech Stocks'", (user_id,)).fetchone()[0]

    _ensure_stocks(conn)

    # --- Demo holdings (Leena already owns some shares) ---
    demo_holdings = [
        (user_id, "INFY",      10,  1520.50),
        (user_id, "TCS",        5,  3650.00),
        (user_id, "RELIANCE",   3,  2480.00),
        (user_id, "AAPL",       8,   178.40),
        (user_id, "NVDA",       2,   680.00),
    ]
    for uid, sym, qty, avg in demo_holdings:
        conn.execute(
            "INSERT OR IGNORE INTO holdings (user_id, symbol, quantity, avg_price) VALUES (?,?,?,?)",
            (uid, sym, qty, avg)
        )

    # --- Demo orders ---
    demo_orders = [
        ("ORD-0001", user_id, "INFY",      "BUY",  "Market", 10, 1520.50, "DAY", "EXECUTED"),
        ("ORD-0002", user_id, "TCS",       "BUY",  "Limit",   5, 3680.00, "DAY", "EXECUTED"),
        ("ORD-0003", user_id, "RELIANCE",  "BUY",  "Market",  3, 2480.00, "DAY", "EXECUTED"),
        ("ORD-0004", user_id, "AAPL",      "BUY",  "Market",  8,  178.40, "DAY", "EXECUTED"),
        ("ORD-0005", user_id, "NVDA",      "BUY",  "Limit",   2,  680.00, "DAY", "EXECUTED"),
        ("ORD-0006", user_id, "WIPRO",     "BUY",  "Limit",   5,  460.00, "DAY", "NEW"),
        ("ORD-0007", user_id, "HDFCBANK",  "SELL", "Market",  2, 1645.00, "DAY", "CANCELLED"),
    ]
    for o in demo_orders:
        conn.execute("""
            INSERT OR IGNORE INTO orders
            (order_id, user_id, symbol, order_type, order_mode, quantity, price, duration, status)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, o)

    # --- Demo transactions (the fills that created the holdings) ---
    demo_tx = [
        (user_id, "BUY",  "INFY",     10, 1520.50, 15205.00),
        (user_id, "BUY",  "TCS",       5, 3650.00, 18250.00),
        (user_id, "BUY",  "RELIANCE",  3, 2480.00,  7440.00),
        (user_id, "BUY",  "AAPL",      8,  178.40,  1427.20),
        (user_id, "BUY",  "NVDA",      2,  680.00,  1360.00),
    ]
    for t in demo_tx:
        uid, ttype, sym, qty, price, amt = t
        charges = calc_charges(amt)
        conn.execute("""
            INSERT INTO transactions
            (user_id, type, symbol, quantity, price, amount, commission, exchange_fee, gst, total_charges)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (uid, ttype, sym, qty, price, amt,
              charges["commission"], charges["exchange_fee"],
              charges["gst"], charges["total_charges"]))

    # --- Demo watchlist ---
    wl_items = [
        (user_id, folder_id,  "TCS"),
        (user_id, folder_id,  "INFY"),
        (user_id, folder_id,  "WIPRO"),
        (user_id, folder2_id, "AAPL"),
        (user_id, folder2_id, "MSFT"),
        (user_id, folder2_id, "NVDA"),
    ]
    for uid, fid, sym in wl_items:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, folder_id, symbol) VALUES (?,?,?)",
            (uid, fid, sym)
        )

    # --- Demo alerts ---
    conn.execute(
        "INSERT INTO alerts (user_id, symbol, field, op, target_price) VALUES (?,?,?,?,?)",
        (user_id, "INFY", "price", "gte", 1650.00)
    )
    conn.execute(
        "INSERT INTO alerts (user_id, symbol, field, op, target_price) VALUES (?,?,?,?,?)",
        (user_id, "TCS", "price", "gte", 4000.00)
    )

    # --- Welcome notification ---
    create_notification(conn, user_id,
                        "Welcome to Equity Trade Pro! 🎉",
                        "Your account is set up with ₹2,50,000 balance. Start trading!",
                        "success", "🎉")
    create_notification(conn, user_id,
                        "Demo Holdings Added",
                        "You have INFY (×10), TCS (×5), RELIANCE (×3), AAPL (×8), NVDA (×2) in your portfolio.",
                        "info", "💼")

    conn.commit()
    seed_price_history(conn)
    conn.close()
    print("[DB] Seed complete.")


def _ensure_stocks(conn):
    for s in SEED_STOCKS:
        conn.execute("""
            INSERT OR IGNORE INTO stocks
            (symbol, name, exchange, sector, currency, ltp, open, high, low, close, prev_close,
             volume, change_pct, pe, market_cap, div_yield, rsi, sma20, ema50, high52, low52)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, s)
    conn.commit()


# ---------------------------------------------------------------------------
# Helper functions used by app.py
# ---------------------------------------------------------------------------

def next_order_id(conn):
    """Generate the next sequential order ID (ORD-XXXX)."""
    row = conn.execute("SELECT COUNT(*) FROM orders").fetchone()
    return f"ORD-{row[0] + 1:04d}"


def calc_charges(amount: float) -> dict:
    """
    Simplified Indian brokerage charge model:
      • Commission   : 0.1 % of trade value  (min ₹20)
      • Exchange fee : 0.005 % of trade value
      • GST          : 18 % on (commission + exchange_fee)
    """
    commission   = max(20.0, amount * 0.001)
    exchange_fee = amount * 0.00005
    gst          = (commission + exchange_fee) * 0.18
    total        = commission + exchange_fee + gst
    return {
        "commission":    round(commission, 2),
        "exchange_fee":  round(exchange_fee, 2),
        "gst":           round(gst, 2),
        "total_charges": round(total, 2),
    }


def create_notification(conn, user_id: int, title: str, message: str,
                         ntype: str = "info", icon: str = "🔔"):
    conn.execute(
        "INSERT INTO notifications (user_id, title, message, type, icon) VALUES (?,?,?,?,?)",
        (user_id, title, message, ntype, icon)
    )


def log_activity(conn, user_id, action: str, details: str = "", ip: str = ""):
    conn.execute(
        "INSERT INTO activity_logs (user_id, action, details, ip) VALUES (?,?,?,?)",
        (user_id, action, details, ip)
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if os.path.exists(DB_PATH):
        resp = input(f"'{DB_PATH}' already exists. Delete and recreate? [y/N]: ").strip().lower()
        if resp == "y":
            os.remove(DB_PATH)
            print(f"[DB] Deleted existing '{DB_PATH}'.")
        else:
            print("[DB] Aborted.")
            exit(0)

    conn = get_db()
    _create_tables(conn)
    conn.commit()
    conn.close()
    seed_db()
    print(f"[DB] '{DB_PATH}' ready.")
    print()
    print("Demo accounts:")
    print("  👤  leena@gmail.com   / password123")
    print("  🔐  admin@equitypro.com / admin123")
