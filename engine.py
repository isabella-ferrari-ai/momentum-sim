# -*- coding: utf-8 -*-
"""执行引擎——热门板块动量轮动，T日收盘决策，T+1开盘执行（严格T+1，不做盘中）。

每个交易日 D 的处理顺序（process_day）：
1. 用 D 日全市场日线算因子 -> 板块热度(Top30%) -> 个股综合分（落 sector_heat）；
2. 卖出执行：先执行【上一交易日 prev 收盘标记的 pending_sell】在 D 开盘价卖出（T+1）；
3. 卖出决策：用 D 收盘对现有持仓判定（止损/到期/板块退出/排名退出），命中则标记
   pending_sell，待下一交易日开盘执行；
4. 买入执行：取 prev 生成的候选池，在 D 开盘价买入（等权 10%，最多 10 只）；
5. 选股：用 D 收盘生成新候选池（signal_date=D），供下一交易日开盘执行；
6. 更新净值。

成交价：买入=D开盘价；卖出=D开盘价（前一日收盘已决策）。
费用：佣金万3、卖出印花税万5、最低5元。
"""
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime
import database as db
import strategy as st
import data_fetcher as dfetch

COMMISSION = 0.0003     # 佣金万3
STAMP_TAX = 0.0005      # 卖出印花税万5
MIN_COMMISSION = 5.0


def _buy_cost(amount):
    return max(amount * COMMISSION, MIN_COMMISSION)


def _sell_cost(amount):
    return max(amount * COMMISSION, MIN_COMMISSION) + amount * STAMP_TAX


def _row_at(panel, code, date):
    """取 code 在 date 的日线行(dict)；含 code 字段。无则 None。"""
    df = panel.get(code)
    if df is None:
        return None
    sub = df[df["date"] == date]
    if sub.empty:
        return None
    r = sub.iloc[0].to_dict()
    r["code"] = code
    return r


def _prev_date(dates, date):
    try:
        i = dates.index(date)
    except ValueError:
        return None
    return dates[i - 1] if i > 0 else None


# ==========================================================================
# 撮合
# ==========================================================================
def execute_buy(code, name, industry, price, signal_date, execute_date, score=None, reason=""):
    """等权买入：单票目标市值 = 初始资金 × 10%（受可用现金约束）。"""
    acct = db.get_account()
    cash = acct["cash"]
    budget = min(acct["initial_capital"] * st.POSITION_PCT, cash * 0.98)
    if price <= 0:
        return None
    shares = st.lots(budget / price)
    if shares < 100:
        return None
    amount = shares * price
    fee = _buy_cost(amount)
    if amount + fee > cash:
        shares = st.lots((cash * 0.98) / price)
        if shares < 100:
            return None
        amount = shares * price
        fee = _buy_cost(amount)
    db.set_cash(cash - amount - fee)
    db.upsert_position({
        "code": code, "name": name, "theme": industry, "shares": shares,
        "avg_cost": price, "open_date": execute_date, "signal_date": signal_date,
        "last_price": price, "high_since_open": price, "score": score,
    })
    t = {
        "ts": execute_date + "T09:30:00", "signal_date": signal_date,
        "execute_date": execute_date, "trade_date": execute_date,
        "code": code, "name": name, "theme": industry, "side": "BUY",
        "price": round(price, 3), "shares": shares, "amount": round(amount, 2),
        "pnl": 0, "pnl_pct": 0, "status": "FILLED", "reason": reason,
    }
    db.record_trade(t)
    return t


def execute_sell(pos, price, execute_date, reason=""):
    acct = db.get_account()
    shares = pos["shares"]
    amount = shares * price
    fee = _sell_cost(amount)
    cost_amount = shares * pos["avg_cost"]
    pnl = amount - cost_amount - fee
    pnl_pct = (price / pos["avg_cost"] - 1) * 100
    db.set_cash(acct["cash"] + amount - fee)
    db.remove_position(pos["code"])
    t = {
        "ts": execute_date + "T09:30:00", "signal_date": pos.get("signal_date"),
        "execute_date": execute_date, "trade_date": execute_date,
        "code": pos["code"], "name": pos.get("name"), "theme": pos.get("theme"),
        "side": "SELL", "price": round(price, 3), "shares": shares,
        "amount": round(amount, 2), "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
        "status": "FILLED", "reason": reason,
    }
    db.record_trade(t)
    return t


def update_equity(trade_date):
    acct = db.get_account()
    positions = db.get_positions()
    mv = st.position_value(positions)
    total = acct["cash"] + mv
    prev = db.last_equity()
    prev_total = prev["total_equity"] if prev else acct["initial_capital"]
    daily_ret = (total / prev_total - 1) * 100 if prev_total else 0
    cum_ret = (total / acct["initial_capital"] - 1) * 100
    db.upsert_equity({
        "trade_date": trade_date, "cash": round(acct["cash"], 2),
        "market_value": round(mv, 2), "total_equity": round(total, 2),
        "daily_return": round(daily_ret, 3), "cum_return": round(cum_ret, 3),
    })


# ==========================================================================
# 单交易日处理（回测/收盘结算共用）
# ==========================================================================
def process_day(panel, names, group_map, index_df, dates, trade_date, log=True,
                trend_on=None, trend_on_prev=None):
    """处理交易日 trade_date(=D)。决策在 D 收盘，执行在 D 开盘（用 prev 的决策）。
    group_map: {code: [概念,...]}（概念分组）或 {code: 行业名}（退化）。
    trend_on:      D 收盘大盘趋势状态（None=不择时）。OFF 时本日收盘标记全部持仓退守。
    trend_on_prev: 上一交易日收盘趋势状态。OFF 时本日开盘不开新仓（与候选 T+1 同源）。"""
    prev = _prev_date(dates, trade_date)

    # 1) D 日因子/概念热度/评分（供本日卖出决策、选股、展示）
    cands, hot_sectors, sector_stats, scored = st.select_momentum_candidates(
        panel, names, group_map, trade_date, return_context=True)
    db.save_sector_heat(trade_date, sector_stats)
    top30 = st.top_rank_codes(scored)

    sells, buys = [], []

    # 2) 卖出执行：prev 收盘标记的 pending_sell 在 D 开盘价卖出（T+1）
    for pos in db.get_positions():
        if not pos.get("pending_sell"):
            continue
        row = _row_at(panel, pos["code"], trade_date)
        if row is None:
            continue
        px = row["open"]
        # 跌停开盘无法卖出则顺延（保留 pending）
        limit = dfetch.limit_pct(pos["code"])
        preclose = row.get("preclose") or pos["avg_cost"]
        if preclose > 0 and (px / preclose - 1) <= -(limit - 0.005):
            continue
        t = execute_sell(pos, px, trade_date, reason=pos.get("pending_reason") or "次日开盘卖出")
        if t:
            sells.append(t)

    # 3) 买入执行：prev 候选池在 D 开盘价买入（等权，最多 10 只）
    #    择时：上一交易日收盘趋势 OFF 则今日开盘不开新仓（退守现金）
    allow_buy = (trend_on_prev is None) or bool(trend_on_prev)
    if prev and allow_buy:
        cand_prev = db.get_candidates(prev)
        held = {p["code"] for p in db.get_positions()}
        for c in cand_prev:
            if len(db.get_positions()) >= st.MAX_POSITIONS:
                break
            code = c["code"]
            if code in held:
                continue
            row = _row_at(panel, code, trade_date)
            if row is None:
                continue
            # 涨停开盘无法买入则跳过
            limit = dfetch.limit_pct(code)
            preclose = row.get("preclose") or 0
            if preclose > 0 and (row["open"] / preclose - 1) >= (limit - 0.005):
                continue
            t = execute_buy(code, c.get("name"), c.get("industry"), row["open"],
                            prev, trade_date, score=c.get("score"),
                            reason=f"动量候选#{c.get('rank')} {c.get('industry')}")
            if t:
                buys.append(t)
                held.add(code)

    # 4) 卖出决策：用 D 收盘价更新持仓状态 + 判定，命中则标记 pending（次日开盘执行）
    #    择时：D 收盘趋势 OFF 则全部持仓退守（次日开盘清仓），优先于个股卖出逻辑
    trend_off = (trend_on is not None) and (not trend_on)
    for pos in db.get_positions():
        row = _row_at(panel, pos["code"], trade_date)
        if row is not None:
            high_since = max(pos.get("high_since_open") or pos["avg_cost"], row["high"])
            db.update_position_price(pos["code"], row["close"], high_since)
            pos["last_price"] = row["close"]
        if pos["open_date"] == trade_date:
            continue  # T+1 当日不决策卖出
        if trend_off:
            db.set_pending_sell(pos["code"], True, "大盘趋势破坏-退守现金")
            continue
        do_sell, reason = st.evaluate_sell(pos, row, trade_date, hot_sectors, top30)
        if do_sell:
            db.set_pending_sell(pos["code"], True, reason)

    # 5) 选股：D 收盘新候选池（signal_date=D），供下一交易日开盘执行
    db.save_candidates(trade_date, cands)

    # 6) 净值
    update_equity(trade_date)

    if log:
        tflag = "" if trend_on is None else (" 趋势ON" if trend_on else " 趋势OFF退守")
        db.log_scan("收盘处理",
                    f"{trade_date} 热门板块{len(hot_sectors)} 买{len(buys)}卖{len(sells)} "
                    f"持仓{len(db.get_positions())}/{st.MAX_POSITIONS} 新候选{len(cands)}{tflag}",
                    signals={"buys": [b["code"] for b in buys], "sells": [s["code"] for s in sells],
                             "hot_sectors": sorted(hot_sectors)},
                    trade_date=trade_date)
    return {"buys": buys, "sells": sells, "candidates": cands,
            "hot_sectors": hot_sectors, "sector_stats": sector_stats,
            "trend_on": trend_on}
