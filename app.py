# -*- coding: utf-8 -*-
"""Flask Web 服务：动量轮动 Dashboard + JSON API。生产用 waitress。

支持 ?db=backtest 查看回测库，默认实时库 data/trading.db。
"""
import warnings
warnings.filterwarnings("ignore")

import os
from datetime import datetime
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

import database as db
import strategy as st

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_DB = os.path.join(BASE_DIR, "data", "trading.db")
BACKTEST_DB = os.path.join(BASE_DIR, "data", "backtest.db")

app = Flask(__name__)
CORS(app)


def _select_db():
    which = request.args.get("db", "live")
    db.set_db_path(BACKTEST_DB if which == "backtest" else LIVE_DB)
    db.init_db()
    return which


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/overview")
def api_overview():
    which = _select_db()
    acct = db.get_account()
    positions = db.get_positions()
    mv = st.position_value(positions)
    total = acct["cash"] + mv
    init = acct["initial_capital"]
    curve = db.get_equity_curve()
    today_ret = curve[-1]["daily_return"] if curve else 0
    sells = [t for t in db.get_trades(99999) if t["side"] == "SELL" and t["status"] == "FILLED"]
    realized = sum(t["pnl"] for t in sells)
    floating = sum(((p.get("last_price") or p["avg_cost"]) - p["avg_cost"]) * p["shares"] for p in positions)
    wins = [t for t in sells if t["pnl"] > 0]
    win_rate = (len(wins) / len(sells) * 100) if sells else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    losses = [t for t in sells if t["pnl"] <= 0]
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    peak = init; mdd = 0
    for e in curve:
        peak = max(peak, e["total_equity"])
        mdd = min(mdd, e["total_equity"] / peak - 1)
    _scan = db.get_scan_log(1)
    last_scan = _scan[0]["ts"] if _scan else None
    last_scan_msg = _scan[0].get("message") if _scan else None
    return jsonify({
        "db": which,
        "cash": round(acct["cash"], 2),
        "market_value": round(mv, 2),
        "total_equity": round(total, 2),
        "initial_capital": init,
        "cum_return": round((total / init - 1) * 100, 2),
        "today_return": round(today_ret, 2),
        "realized_pnl": round(realized, 2),
        "floating_pnl": round(floating, 2),
        "position_count": len(positions),
        "max_positions": st.MAX_POSITIONS,
        "win_rate": round(win_rate, 1),
        "trade_count": len(sells),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "max_drawdown": round(mdd * 100, 2),
        "updated_at": acct.get("updated_at"),
        "last_scan": last_scan,
        "last_scan_msg": last_scan_msg,
        "trend": db.get_meta("trend"),
    })


@app.route("/api/equity")
def api_equity():
    _select_db()
    return jsonify(db.get_equity_curve())


@app.route("/api/bench")
def api_bench():
    """净值曲线的六大指数同期对比数据（真实收盘价，前端按净值起始日归一化叠加）。
    由调度器每日收盘后刷新到 data/index_history.json；与 live/backtest 无关。"""
    import json as _json
    path = os.path.join(BASE_DIR, "data", "index_history.json")
    if not os.path.exists(path):
        return jsonify({"indices": {}, "source": "", "updated": ""})
    with open(path, encoding="utf-8") as f:
        return jsonify(_json.load(f))


@app.route("/api/positions")
def api_positions():
    _select_db()
    out = []
    today = datetime.now().strftime("%Y-%m-%d")
    for p in db.get_positions():
        last = p.get("last_price") or p["avg_cost"]
        cost_val = p["avg_cost"] * p["shares"]
        mkt_val = last * p["shares"]
        out.append({
            **p,
            "market_value": round(mkt_val, 2),
            "cost_value": round(cost_val, 2),
            "float_pnl": round(mkt_val - cost_val, 2),
            "float_pnl_pct": round((last / p["avg_cost"] - 1) * 100, 2),
            "days_held": st._days_held(p["open_date"], today),
            "can_sell": p["open_date"] != today,
        })
    return jsonify(out)


@app.route("/api/trades")
def api_trades():
    _select_db()
    return jsonify(db.get_trades(300))


@app.route("/api/closed")
def api_closed():
    """清仓清单：每笔已成交卖出即一次完整平仓（建仓→清仓），逐笔列出盈亏与胜败。
    动量策略等权满仓买入、卖出一次性清空该标的，故每个 SELL 对应一个完整回合。"""
    _select_db()
    sells = [t for t in db.get_trades(99999)
             if t["side"] == "SELL" and t["status"] == "FILLED"]
    rounds = []
    for t in sells:
        amt = t.get("amount") or (t.get("price", 0) * t.get("shares", 0))
        cost = amt - (t.get("pnl") or 0)   # 卖出金额 - 盈亏 ≈ 含费成本
        rounds.append({
            "code": t["code"], "name": t.get("name"), "theme": t.get("theme"),
            "shares": t.get("shares"), "sell_price": t.get("price"),
            "sell_amount": round(amt, 2),
            "open_date": t.get("open_date"), "signal_date": t.get("signal_date"),
            "close_date": t.get("execute_date") or t.get("trade_date"),
            "close_ts": t.get("ts"),
            "pnl": round(t.get("pnl") or 0, 2),
            "pnl_pct": round(t.get("pnl_pct") or 0, 2),
            "win": (t.get("pnl") or 0) > 0,
            "reason": t.get("reason", ""),
        })
    wins = [r for r in rounds if r["win"]]
    losses = [r for r in rounds if not r["win"]]
    total_pnl = sum(r["pnl"] for r in rounds)
    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = -sum(r["pnl"] for r in losses)
    return jsonify({
        "rounds": rounds,
        "summary": {
            "count": len(rounds),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(len(wins) / len(rounds) * 100, 1) if rounds else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_win_pct": round(sum(r["pnl_pct"] for r in wins) / len(wins), 2) if wins else 0,
            "avg_loss_pct": round(sum(r["pnl_pct"] for r in losses) / len(losses), 2) if losses else 0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        },
    })


@app.route("/api/candidates")
def api_candidates():
    _select_db()
    sd = request.args.get("date")
    items = db.get_candidates(sd, limit=20)
    actual_sd = sd or (items[0]["signal_date"] if items else None)
    return jsonify({"signal_date": actual_sd, "items": items})


@app.route("/api/sectors")
def api_sectors():
    _select_db()
    td = request.args.get("date")
    return jsonify(db.get_sector_heat(td, only_hot=False, limit=40))


@app.route("/api/scan_log")
def api_scan_log():
    _select_db()
    return jsonify(db.get_scan_log(40))


@app.route("/api/indices")
def api_indices():
    """参考情绪的 A 股宽基指数实时快照（腾讯源）。"""
    import data_fetcher as dfetch
    try:
        items = dfetch.index_spot()
    except Exception:
        items = []
    return jsonify({"items": items, "as_of": datetime.now().isoformat(timespec="seconds")})


@app.route("/api/trend")
def api_trend():
    """趋势总闸可视化：沪深300收盘 + MA200 + 迟滞带 + 每日 ON/OFF 状态（尾段 + 今日实时点）。"""
    import json as _json
    import trend as tr
    import data_fetcher as dfetch
    path = os.path.join(BASE_DIR, "data", "hs300_close.json")
    closes = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            closes = _json.load(f)
    # 追加今日实时点（盘中），让趋势线连到当前
    live_pct = None
    try:
        for ix in dfetch.index_spot():
            if ix["code"] == "sh.000300" and ix.get("price"):
                today = datetime.now().strftime("%Y-%m-%d")
                closes[today] = ix["price"]
                live_pct = ix.get("pct")
                break
    except Exception:
        pass
    if not closes:
        return jsonify({"available": False})
    win, band = 200, 0.03
    states = tr.compute_states(closes, mode="ma_hysteresis", win=win, band=band)
    dates = sorted(closes)
    # MA200 序列
    vals = [closes[d] for d in dates]
    ma = []
    for i in range(len(vals)):
        ma.append(round(sum(vals[i - win + 1:i + 1]) / win, 2) if i + 1 >= win else None)
    tail = 250
    sl = slice(-tail, None)
    ds = dates[sl]
    rows = [{"date": d, "close": round(closes[d], 2), "ma": ma[i],
             "on": bool(states.get(d))} for i, d in list(enumerate(dates))[sl]]
    cur = rows[-1] if rows else None
    return jsonify({
        "available": True, "bench": "沪深300", "win": win, "band": band,
        "rows": rows, "live_pct": live_pct,
        "on": cur["on"] if cur else None,
        "on_pct_recent": tr.describe({d: states[d] for d in ds})["on_pct"],
    })


@app.route("/api/strategy")
def api_strategy():
    _select_db()
    return jsonify({
        "model": "热门概念板块成分股动量轮动 — T日收盘选股，买入仅集合竞价，卖出随时触发",
        "initial_capital": db.INITIAL_CAPITAL,
        "max_positions": st.MAX_POSITIONS,
        "position_pct": st.POSITION_PCT,
        "max_per_group": st.MAX_PER_GROUP,
        "top_sector_pct": st.TOP_SECTOR_PCT,
        "top_n_candidates": st.TOP_N_CANDIDATES,
        "min_amount": st.MIN_AMOUNT,
        "stop_loss": st.STOP_LOSS,
        "take_profit": st.TAKE_PROFIT,
        "trail_stop": st.TRAIL_STOP,
        "trail_arm_profit": st.TRAIL_ARM_PROFIT,
        "hold_max_days": st.HOLD_MAX_DAYS,
        "rank_exit_pct": st.RANK_EXIT_PCT,
        "buy_rule": "仅集合竞价买入（次日09:25集合竞价≈开盘价，严格T+1）",
        "sell_rule": f"卖出随时触发：成本止损{int(st.STOP_LOSS*100)}% / 固定止盈+{int(st.TAKE_PROFIT*100)}% / "
                     f"高点回撤止盈{int(st.TRAIL_STOP*100)}%(浮盈≥+{int(st.TRAIL_ARM_PROFIT*100)}%后启用)；"
                     f"概念退出/排名退出/持有{st.HOLD_MAX_DAYS}天到期为收盘慢信号次日集合竞价卖",
        "trend_filter": "沪深300 MA200 迟滞±3%（趋势OFF不开新仓且清仓退守现金）",
        "factors": [
            {"name": "MOM_5", "weight": st.W_MOM5, "desc": "过去5日涨幅"},
            {"name": "MOM_20", "weight": st.W_MOM20, "desc": "过去20日涨幅(去最近1日)"},
            {"name": "量价共振", "weight": st.W_VOLPRICE, "desc": "近5日量能扩张×上涨"},
            {"name": "相对板块强度", "weight": st.W_RELSTR, "desc": "个股MOM20-概念均MOM20"},
        ],
    })


@app.route("/backtest")
def backtest_page():
    return render_template("backtest.html")


@app.route("/api/backtest")
def api_backtest():
    """读取 backtest.py 产出的回测结果 JSON。group=industry|concept。"""
    import json
    group = request.args.get("group", "industry")
    path = os.path.join(BASE_DIR, "data", f"backtest_{group}.json")
    if not os.path.exists(path):
        return jsonify({"available": False, "group": group})
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # equity 体积可能大，下采样到 ~400 点用于画图
    eq = data.get("equity", [])
    bench = data.get("bench_curves", {})
    step = (len(eq) // 400 + 1) if len(eq) > 400 else 1
    if step > 1:
        eq = eq[::step] + [eq[-1]]
        bench = {nm: (c[::step] + [c[-1]]) for nm, c in bench.items() if c}
    return jsonify({"available": True, "group": group,
                    "metrics": data.get("metrics", {}),
                    "equity": eq, "bench_curves": bench,
                    "trade_count": len(data.get("trades", []))})


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat(timespec="seconds")})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8889))
    db.set_db_path(LIVE_DB)
    db.init_db()
    app.run(host="0.0.0.0", port=port, debug=True)
