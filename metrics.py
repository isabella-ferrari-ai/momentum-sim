# -*- coding: utf-8 -*-
"""绩效指标模块——把净值/交易序列汇总成研究闭环所需的全套指标。

输入
====
- equity: list[dict]，每项含 trade_date, total_equity（按日，升序）。
- trades: list[dict]，每项含 side, execute_date, pnl, pnl_pct, amount, code, open_date（卖出时还原持仓天数）。
- bench: dict[name -> list[dict(trade_date,total)]]（可选），用于超额收益。

输出
====
compute_all() 返回一个 dict，覆盖振威要求的：
  年化收益 / 分年度收益 / 最大回撤 / Calmar / Sharpe / 胜率 / 盈亏比 /
  平均持仓天数 / 年换手 / 月度收益热力图 / 相对基准超额 / 牛熊震荡分段表现。

无 numpy 依赖（纯 Python），便于在受限环境运行。
"""
import warnings
warnings.filterwarnings("ignore")

import math
from datetime import datetime

TRADING_DAYS = 244.0   # A股年均交易日


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def _daily_returns(equity):
    """从净值序列算逐日收益率 list[(date, ret)]。"""
    out = []
    for i in range(1, len(equity)):
        p0 = equity[i - 1]["total_equity"]
        p1 = equity[i]["total_equity"]
        if p0 and p0 > 0:
            out.append((equity[i]["trade_date"], p1 / p0 - 1))
    return out


def _max_drawdown(equity):
    """最大回撤（负数，如 -0.23）及其区间。"""
    peak = None
    mdd = 0.0
    peak_date = trough_date = None
    cur_peak_date = None
    for e in equity:
        v = e["total_equity"]
        if peak is None or v > peak:
            peak = v
            cur_peak_date = e["trade_date"]
        if peak and peak > 0:
            dd = v / peak - 1
            if dd < mdd:
                mdd = dd
                peak_date = cur_peak_date
                trough_date = e["trade_date"]
    return mdd, peak_date, trough_date


def _cagr(equity):
    """年化收益（几何）。"""
    if len(equity) < 2:
        return 0.0
    v0 = equity[0]["total_equity"]
    v1 = equity[-1]["total_equity"]
    if v0 <= 0:
        return 0.0
    days = (_d(equity[-1]["trade_date"]) - _d(equity[0]["trade_date"])).days
    yrs = days / 365.25
    if yrs <= 0:
        return v1 / v0 - 1
    return (v1 / v0) ** (1 / yrs) - 1


def _sharpe(rets, rf=0.0):
    """年化夏普（按日收益）。rf 为年化无风险利率。"""
    if len(rets) < 2:
        return 0.0
    xs = [r for _, r in rets]
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    daily_rf = rf / TRADING_DAYS
    return (mean - daily_rf) / sd * math.sqrt(TRADING_DAYS)


def _sortino(rets, rf=0.0):
    if len(rets) < 2:
        return 0.0
    xs = [r for _, r in rets]
    mean = sum(xs) / len(xs)
    downs = [min(x, 0.0) ** 2 for x in xs]
    dd = math.sqrt(sum(downs) / len(xs))
    if dd == 0:
        return 0.0
    daily_rf = rf / TRADING_DAYS
    return (mean - daily_rf) / dd * math.sqrt(TRADING_DAYS)


# --------------------------------------------------------------------------
# 分年度 / 月度
# --------------------------------------------------------------------------
def _period_returns(equity, keyfn):
    """按 keyfn(date_str)->bucket 把净值分桶，算各桶首末净值收益。
    返回 dict[bucket -> ret]（按桶内首末，跨桶连续）。"""
    if len(equity) < 2:
        return {}
    buckets = {}
    for e in equity:
        b = keyfn(e["trade_date"])
        buckets.setdefault(b, []).append(e)
    out = {}
    for b, es in buckets.items():
        v0 = es[0]["total_equity"]
        v1 = es[-1]["total_equity"]
        # 用桶内首日的「前一日」做基准更精确；这里用桶首日净值近似
        if v0 > 0:
            out[b] = v1 / v0 - 1
    return out


def _yearly(equity):
    """分年度收益：用每年最后一个净值 / 上年最后一个净值（连续）。"""
    if len(equity) < 2:
        return {}
    last_by_year = {}
    for e in equity:
        y = e["trade_date"][:4]
        last_by_year[y] = e["total_equity"]
    years = sorted(last_by_year)
    out = {}
    prev = equity[0]["total_equity"]
    for y in years:
        v = last_by_year[y]
        if prev > 0:
            out[y] = v / prev - 1
        prev = v
    return out


def _monthly_heatmap(equity):
    """月度收益热力图数据：{year: {month(1-12): ret}}，连续口径。"""
    if len(equity) < 2:
        return {}
    last_by_ym = {}
    order = []
    for e in equity:
        ym = e["trade_date"][:7]
        if ym not in last_by_ym:
            order.append(ym)
        last_by_ym[ym] = e["total_equity"]
    out = {}
    prev = equity[0]["total_equity"]
    for ym in order:
        v = last_by_ym[ym]
        y, m = ym.split("-")
        if prev > 0:
            out.setdefault(y, {})[int(m)] = round((v / prev - 1) * 100, 2)
        prev = v
    return out


# --------------------------------------------------------------------------
# 交易统计
# --------------------------------------------------------------------------
def _trade_stats(trades):
    """胜率 / 盈亏比 / 平均盈亏 / 平均持仓天数（基于已平仓 SELL）。"""
    sells = [t for t in trades if t.get("side") == "SELL"]
    n = len(sells)
    if n == 0:
        return {"trade_count": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "avg_hold_days": 0.0,
                "expectancy_pct": 0.0}
    wins = [t for t in sells if (t.get("pnl") or 0) > 0]
    losses = [t for t in sells if (t.get("pnl") or 0) <= 0]
    gross_win = sum(t.get("pnl") or 0 for t in wins)
    gross_loss = -sum(t.get("pnl") or 0 for t in losses)
    win_rate = len(wins) / n * 100
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_win = (sum(t.get("pnl_pct") or 0 for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.get("pnl_pct") or 0 for t in losses) / len(losses)) if losses else 0.0
    # 持仓天数
    holds = []
    for t in sells:
        od, xd = t.get("open_date"), t.get("execute_date")
        if od and xd:
            try:
                holds.append((_d(xd) - _d(od)).days)
            except Exception:
                pass
    avg_hold = sum(holds) / len(holds) if holds else 0.0
    expectancy = sum(t.get("pnl_pct") or 0 for t in sells) / n
    return {
        "trade_count": n,
        "win_rate": round(win_rate, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else 999.0,
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "avg_hold_days": round(avg_hold, 1),
        "expectancy_pct": round(expectancy, 2),
    }


def _annual_turnover(trades, equity):
    """年换手率 ≈ 年均买入额 / 平均净值。粗口径，反映交易频繁度。"""
    if len(equity) < 2:
        return 0.0
    buys = [t for t in trades if t.get("side") == "BUY"]
    total_buy = sum(t.get("amount") or 0 for t in buys)
    days = (_d(equity[-1]["trade_date"]) - _d(equity[0]["trade_date"])).days or 1
    yrs = days / 365.25
    avg_equity = sum(e["total_equity"] for e in equity) / len(equity)
    if avg_equity <= 0 or yrs <= 0:
        return 0.0
    return round(total_buy / avg_equity / yrs, 2)


# --------------------------------------------------------------------------
# 基准超额 + 牛熊震荡分段
# --------------------------------------------------------------------------
def _align(equity, bench):
    """把策略与基准按公共日期对齐，返回 (dates, strat_vals, bench_vals)。"""
    bmap = {b["trade_date"]: b["total"] for b in bench}
    dates, sv, bv = [], [], []
    for e in equity:
        d = e["trade_date"]
        if d in bmap:
            dates.append(d); sv.append(e["total_equity"]); bv.append(bmap[d])
    return dates, sv, bv


def _excess(equity, bench):
    """相对基准超额：策略年化 - 基准年化，以及全程累计超额。"""
    dates, sv, bv = _align(equity, bench)
    if len(dates) < 2 or sv[0] <= 0 or bv[0] <= 0:
        return None
    strat_cum = sv[-1] / sv[0] - 1
    bench_cum = bv[-1] / bv[0] - 1
    eq_s = [{"trade_date": d, "total_equity": v} for d, v in zip(dates, sv)]
    eq_b = [{"trade_date": d, "total_equity": v} for d, v in zip(dates, bv)]
    return {
        "strat_cum_pct": round(strat_cum * 100, 2),
        "bench_cum_pct": round(bench_cum * 100, 2),
        "excess_cum_pct": round((strat_cum - bench_cum) * 100, 2),
        "strat_cagr_pct": round(_cagr(eq_s) * 100, 2),
        "bench_cagr_pct": round(_cagr(eq_b) * 100, 2),
        "excess_cagr_pct": round((_cagr(eq_s) - _cagr(eq_b)) * 100, 2),
    }


def classify_regimes(bench_series, ma_window=20):
    """用基准指数把时间分成 牛/熊/震荡 三段。
    bench_series: list[dict(trade_date,total)]（升序，作大盘代表，如沪深300）。
    简单规则（按月聚合趋势）：60日累计涨幅 > +10% 牛；< -10% 熊；其余震荡。
    返回 {date: regime}。"""
    n = len(bench_series)
    out = {}
    win = 60
    for i in range(n):
        d = bench_series[i]["trade_date"]
        if i < win:
            out[d] = "震荡"
            continue
        v0 = bench_series[i - win]["total"]
        v1 = bench_series[i]["total"]
        chg = (v1 / v0 - 1) if v0 > 0 else 0
        out[d] = "牛" if chg > 0.10 else ("熊" if chg < -0.10 else "震荡")
    return out


def regime_performance(equity, bench_series):
    """牛熊震荡分段表现：各 regime 下策略的日收益累计与日均。"""
    regimes = classify_regimes(bench_series)
    rets = _daily_returns(equity)
    buckets = {"牛": [], "熊": [], "震荡": []}
    for d, r in rets:
        reg = regimes.get(d)
        if reg in buckets:
            buckets[reg].append(r)
    out = {}
    for reg, xs in buckets.items():
        if not xs:
            out[reg] = {"days": 0, "cum_pct": 0.0, "avg_daily_pct": 0.0}
            continue
        cum = 1.0
        for r in xs:
            cum *= (1 + r)
        out[reg] = {
            "days": len(xs),
            "cum_pct": round((cum - 1) * 100, 2),
            "avg_daily_pct": round(sum(xs) / len(xs) * 100, 3),
        }
    return out


# --------------------------------------------------------------------------
# 汇总入口
# --------------------------------------------------------------------------
def compute_all(equity, trades, benches=None, regime_bench=None, rf=0.03):
    """汇总全套指标。
    equity: list[dict(trade_date,total_equity)] 升序。
    trades: list[dict]。
    benches: dict[name -> list[dict(trade_date,total)]]（超额收益用）。
    regime_bench: list[dict(trade_date,total)]（牛熊分段用，通常沪深300）。
    """
    equity = sorted(equity, key=lambda e: e["trade_date"])
    rets = _daily_returns(equity)
    mdd, mdd_peak, mdd_trough = _max_drawdown(equity)
    cagr = _cagr(equity)
    calmar = (cagr / abs(mdd)) if mdd < 0 else (float("inf") if cagr > 0 else 0.0)
    v0 = equity[0]["total_equity"] if equity else 0
    v1 = equity[-1]["total_equity"] if equity else 0

    res = {
        "start": equity[0]["trade_date"] if equity else None,
        "end": equity[-1]["trade_date"] if equity else None,
        "days": len(equity),
        "total_return_pct": round((v1 / v0 - 1) * 100, 2) if v0 else 0.0,
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "mdd_peak": mdd_peak, "mdd_trough": mdd_trough,
        "calmar": round(calmar, 2) if calmar != float("inf") else 999.0,
        "sharpe": round(_sharpe(rets, rf), 2),
        "sortino": round(_sortino(rets, rf), 2),
        "yearly": {k: round(v * 100, 2) for k, v in _yearly(equity).items()},
        "monthly_heatmap": _monthly_heatmap(equity),
        "annual_turnover": _annual_turnover(trades, equity),
    }
    res.update(_trade_stats(trades))

    if benches:
        res["excess"] = {name: _excess(equity, b) for name, b in benches.items() if b}
    if regime_bench:
        res["regimes"] = regime_performance(equity, regime_bench)
    return res
