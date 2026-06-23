# -*- coding: utf-8 -*-
"""SQLite 持久化层——热门板块动量轮动模拟盘（与 trading-sim 完全独立）。

表：account / positions / trades / candidate_pool / equity_curve / sector_heat / scan_log。
默认库 data/trading.db；回测库 data/backtest.db（通过 set_db_path 或 TRADING_DB 切换）。
初始资金 50 万，最多 10 只等权。
"""
import os
import sqlite3
import json
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "trading.db")
DB_PATH = os.environ.get("TRADING_DB", DEFAULT_DB)

INITIAL_CAPITAL = 500_000.0


def set_db_path(path):
    global DB_PATH
    DB_PATH = path


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash REAL NOT NULL,
            initial_capital REAL NOT NULL,
            updated_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            code TEXT PRIMARY KEY,
            name TEXT,
            theme TEXT,                      -- 所属行业（板块退出判定用）
            shares INTEGER NOT NULL,
            avg_cost REAL NOT NULL,
            open_date TEXT NOT NULL,         -- 买入成交日(T+1)，T+1判定基准
            signal_date TEXT,                -- 产生买入信号的日(T)
            last_price REAL,
            high_since_open REAL,
            score REAL,                      -- 入选时综合分
            pending_sell INTEGER DEFAULT 0,  -- T收盘判定待次日开盘卖出
            pending_reason TEXT              -- 待卖原因
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            signal_date TEXT,
            execute_date TEXT NOT NULL,
            trade_date TEXT,
            code TEXT NOT NULL,
            name TEXT,
            theme TEXT,
            side TEXT NOT NULL,              -- BUY / SELL
            price REAL NOT NULL,
            shares INTEGER NOT NULL,
            amount REAL NOT NULL,
            pnl REAL DEFAULT 0,
            pnl_pct REAL DEFAULT 0,
            status TEXT DEFAULT 'FILLED',
            reason TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS equity_curve (
            trade_date TEXT PRIMARY KEY,
            cash REAL NOT NULL,
            market_value REAL NOT NULL,
            total_equity REAL NOT NULL,
            daily_return REAL DEFAULT 0,
            cum_return REAL DEFAULT 0
        )
    """)
    # 每日候选池（含动量因子分解）
    c.execute("""
        CREATE TABLE IF NOT EXISTS candidate_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_date TEXT NOT NULL,
            rank INTEGER,
            code TEXT NOT NULL,
            name TEXT,
            industry TEXT,
            score REAL,
            mom5 REAL,
            mom20 REAL,
            vol_ratio5 REAL,
            rel_str REAL,
            pct REAL,
            amount REAL,
            turn REAL,
            reason TEXT,
            UNIQUE(signal_date, code)
        )
    """)
    # 每日板块热度（Top30% 热门板块快照）
    c.execute("""
        CREATE TABLE IF NOT EXISTS sector_heat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            industry TEXT NOT NULL,
            mom5 REAL,
            mom20 REAL,
            member_count INTEGER,
            hot INTEGER DEFAULT 0,
            UNIQUE(trade_date, industry)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            trade_date TEXT,
            phase TEXT,
            message TEXT,
            signals TEXT
        )
    """)
    # 通用键值（趋势择时状态等）
    c.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    row = c.execute("SELECT * FROM account WHERE id=1").fetchone()
    if row is None:
        c.execute(
            "INSERT INTO account (id, cash, initial_capital, updated_at) VALUES (1, ?, ?, ?)",
            (INITIAL_CAPITAL, INITIAL_CAPITAL, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    conn.close()


# --------------------------- 账户 ---------------------------
def get_account():
    conn = get_conn()
    row = conn.execute("SELECT * FROM account WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else None


def set_cash(cash):
    conn = get_conn()
    conn.execute("UPDATE account SET cash=?, updated_at=? WHERE id=1",
                 (cash, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


# --------------------------- 持仓 ---------------------------
def get_positions():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM positions ORDER BY open_date").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_position(code):
    conn = get_conn()
    row = conn.execute("SELECT * FROM positions WHERE code=?", (code,)).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_position(pos):
    conn = get_conn()
    conn.execute("""
        INSERT INTO positions (code,name,theme,shares,avg_cost,open_date,signal_date,last_price,high_since_open,score)
        VALUES (:code,:name,:theme,:shares,:avg_cost,:open_date,:signal_date,:last_price,:high_since_open,:score)
        ON CONFLICT(code) DO UPDATE SET
            shares=excluded.shares, avg_cost=excluded.avg_cost, last_price=excluded.last_price,
            high_since_open=excluded.high_since_open, score=excluded.score, theme=excluded.theme
    """, {
        "code": pos["code"], "name": pos.get("name"), "theme": pos.get("theme"),
        "shares": pos["shares"], "avg_cost": pos["avg_cost"], "open_date": pos["open_date"],
        "signal_date": pos.get("signal_date"),
        "last_price": pos.get("last_price"), "high_since_open": pos.get("high_since_open"),
        "score": pos.get("score"),
    })
    conn.commit()
    conn.close()


def update_position_price(code, last_price, high_since_open=None):
    conn = get_conn()
    if high_since_open is not None:
        conn.execute("UPDATE positions SET last_price=?, high_since_open=? WHERE code=?",
                     (last_price, high_since_open, code))
    else:
        conn.execute("UPDATE positions SET last_price=? WHERE code=?", (last_price, code))
    conn.commit()
    conn.close()


def remove_position(code):
    conn = get_conn()
    conn.execute("DELETE FROM positions WHERE code=?", (code,))
    conn.commit()
    conn.close()


def set_pending_sell(code, pending, reason=None):
    conn = get_conn()
    conn.execute("UPDATE positions SET pending_sell=?, pending_reason=? WHERE code=?",
                 (1 if pending else 0, reason, code))
    conn.commit()
    conn.close()


# --------------------------- 交易 ---------------------------
def record_trade(t):
    conn = get_conn()
    conn.execute("""
        INSERT INTO trades (ts,signal_date,execute_date,trade_date,code,name,theme,side,price,shares,amount,pnl,pnl_pct,status,reason)
        VALUES (:ts,:signal_date,:execute_date,:trade_date,:code,:name,:theme,:side,:price,:shares,:amount,:pnl,:pnl_pct,:status,:reason)
    """, {
        "ts": t.get("ts", datetime.now().isoformat(timespec="seconds")),
        "signal_date": t.get("signal_date"),
        "execute_date": t["execute_date"],
        "trade_date": t.get("trade_date", t["execute_date"]),
        "code": t["code"], "name": t.get("name"), "theme": t.get("theme"),
        "side": t["side"], "price": t["price"], "shares": t["shares"], "amount": t["amount"],
        "pnl": t.get("pnl", 0), "pnl_pct": t.get("pnl_pct", 0),
        "status": t.get("status", "FILLED"), "reason": t.get("reason", ""),
    })
    conn.commit()
    conn.close()


def get_trades(limit=200):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM trades ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --------------------------- 净值 ---------------------------
def upsert_equity(rec):
    conn = get_conn()
    conn.execute("""
        INSERT INTO equity_curve (trade_date,cash,market_value,total_equity,daily_return,cum_return)
        VALUES (:trade_date,:cash,:market_value,:total_equity,:daily_return,:cum_return)
        ON CONFLICT(trade_date) DO UPDATE SET
            cash=excluded.cash, market_value=excluded.market_value,
            total_equity=excluded.total_equity, daily_return=excluded.daily_return,
            cum_return=excluded.cum_return
    """, rec)
    conn.commit()
    conn.close()


def get_equity_curve():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM equity_curve ORDER BY trade_date").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def last_equity():
    conn = get_conn()
    row = conn.execute("SELECT * FROM equity_curve ORDER BY trade_date DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


# --------------------------- 候选池 ---------------------------
def save_candidates(signal_date, cands):
    conn = get_conn()
    conn.execute("DELETE FROM candidate_pool WHERE signal_date=?", (signal_date,))
    for i, c in enumerate(cands):
        conn.execute("""
            INSERT OR REPLACE INTO candidate_pool
                (signal_date,rank,code,name,industry,score,mom5,mom20,vol_ratio5,rel_str,pct,amount,turn,reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (signal_date, i + 1, c["code"], c.get("name"), c.get("industry"),
              c.get("score"), c.get("mom5"), c.get("mom20"), c.get("vol_ratio5"),
              c.get("rel_str"), c.get("pctChg"), c.get("amount"), c.get("turn"), c.get("reason")))
    conn.commit()
    conn.close()


def get_candidates(signal_date=None, limit=20):
    conn = get_conn()
    if signal_date is None:
        row = conn.execute("SELECT signal_date FROM candidate_pool ORDER BY signal_date DESC LIMIT 1").fetchone()
        if not row:
            conn.close()
            return []
        signal_date = row["signal_date"]
    rows = conn.execute(
        "SELECT * FROM candidate_pool WHERE signal_date=? ORDER BY rank LIMIT ?", (signal_date, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --------------------------- 板块热度 ---------------------------
def save_sector_heat(trade_date, sector_stats):
    conn = get_conn()
    conn.execute("DELETE FROM sector_heat WHERE trade_date=?", (trade_date,))
    for ind, s in sector_stats.items():
        conn.execute("""
            INSERT OR REPLACE INTO sector_heat (trade_date,industry,mom5,mom20,member_count,hot)
            VALUES (?,?,?,?,?,?)
        """, (trade_date, ind, round(s["mom5"] * 100, 2), round(s["mom20"] * 100, 2),
              s["count"], 1 if s.get("hot") else 0))
    conn.commit()
    conn.close()


def get_sector_heat(trade_date=None, only_hot=False, limit=40):
    conn = get_conn()
    if trade_date is None:
        row = conn.execute("SELECT trade_date FROM sector_heat ORDER BY trade_date DESC LIMIT 1").fetchone()
        if not row:
            conn.close()
            return {"trade_date": None, "items": []}
        trade_date = row["trade_date"]
    q = "SELECT * FROM sector_heat WHERE trade_date=?"
    if only_hot:
        q += " AND hot=1"
    q += " ORDER BY mom5 DESC LIMIT ?"
    rows = conn.execute(q, (trade_date, limit)).fetchall()
    conn.close()
    return {"trade_date": trade_date, "items": [dict(r) for r in rows]}


# --------------------------- 通用键值 ---------------------------
def set_meta(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO meta (key,value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False),
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def get_meta(key, default=None):
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except Exception:
        row = None
    conn.close()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


# --------------------------- 扫描日志 ---------------------------
def log_scan(phase, message, signals=None, trade_date=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO scan_log (ts,trade_date,phase,message,signals) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), trade_date, phase, message,
         json.dumps(signals, ensure_ascii=False) if signals else None),
    )
    conn.commit()
    conn.close()


def get_scan_log(limit=50):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM scan_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reset_all():
    conn = get_conn()
    for t in ["positions", "trades", "equity_curve", "candidate_pool", "sector_heat", "scan_log"]:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("UPDATE account SET cash=?, initial_capital=? WHERE id=1",
                 (INITIAL_CAPITAL, INITIAL_CAPITAL))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("DB initialized at", DB_PATH)
    print("account:", get_account())
