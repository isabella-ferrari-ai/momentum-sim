# -*- coding: utf-8 -*-
"""调度器——热门板块动量轮动，每日收盘后结算（纯T+1，不做盘中）。

运行模型：
- 盘中不交易（本策略只用日线）。每个交易日收盘后（约 15:05 起）用 baostock 完整
  日线增量刷新面板，跑 engine.process_day 完成：板块热度 → T+1开盘执行(用昨日决策/候选)
  → 收盘卖出决策(标记pending) → 选股 → 净值。
- baostock 当日 bar 约 15:30 后才更新，未就绪则稍后重试。

数据：baostock 日线（panel.db），启动时与每日收盘后增量刷新到最近交易日。
"""
import warnings
warnings.filterwarnings("ignore")

import os
import time
from datetime import datetime, timedelta

import database as db
import data_fetcher as dfetch
import engine
import strategy as st
import trend as tr

CHECK_INTERVAL = 10 * 60   # 每10分钟检查一次（收盘后等日线就绪）
SIM_START = os.environ.get("SIM_START", "2026-06-23")
PANEL_LOOKBACK_DAYS = 40   # 动量计算所需近端历史

# 市场择时（趋势总闸）——回测择优: 沪深300 MA200 迟滞±3%
TREND_BENCH = "sh.000300"
TREND_PARAMS = dict(mode="ma_hysteresis", win=200, band=0.03)
TREND_LOOKBACK_DAYS = 420   # MA200 需 ~200 交易日，留足日历缓冲


def _now():
    return datetime.now()


def _today():
    return _now().strftime("%Y-%m-%d")


_panel_cache = {"date": None, "panel": None, "names": None, "industry": None}


def _panel_is_fresh(td, max_age_days=5):
    try:
        pdates = dfetch.panel_dates()
        if not pdates:
            return False
        last = datetime.strptime(pdates[-1], "%Y-%m-%d")
        return (datetime.strptime(td, "%Y-%m-%d") - last).days <= max_age_days
    except Exception:
        return False


def _ensure_recent_panel(td, force=False):
    """确保面板库含最近交易日数据（动量计算用）。"""
    if not force and _panel_is_fresh(td):
        return
    start = (datetime.strptime(td, "%Y-%m-%d") - timedelta(days=PANEL_LOOKBACK_DAYS * 2)).strftime("%Y-%m-%d")
    try:
        dfetch.build_panel(start, td, lookback_days=PANEL_LOOKBACK_DAYS)
    except Exception as e:
        db.log_scan("面板刷新异常", f"{repr(e)[:120]}", trade_date=td)


def _ensure_concept_map(td):
    """每日收盘后刷新概念板块映射（新浪源）。失败则保留旧缓存、退化为行业分类，不影响主流程。"""
    try:
        if dfetch.concept_map_date() == td:
            return
        uni = set(dfetch.universe_codes())
        n = dfetch.refresh_concept_map(td, only_codes=uni or None)
        if n:
            db.log_scan("概念刷新", f"{td} 概念板块映射已更新 {n} 只(新浪源)", trade_date=td)
        else:
            db.log_scan("概念降级", f"{td} 概念抓取失败/被墙，沿用旧缓存或退化为行业分类", trade_date=td)
    except Exception as e:
        db.log_scan("概念异常", f"{repr(e)[:120]}", trade_date=td)


def _compute_trend(td):
    """拉沪深300长周期收盘，算 MA200 迟滞趋势状态，返回 {date: bool}。失败返回 None。
    需在 baostock 会话内调用（复用 settle_close 的 session）。"""
    try:
        start = (datetime.strptime(td, "%Y-%m-%d")
                 - timedelta(days=TREND_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        df = dfetch.get_index(start, td, code=TREND_BENCH)
        if df is None or df.empty:
            return None
        closes = {r["date"]: float(r["close"]) for _, r in df.iterrows()}
        return tr.compute_states(closes, **TREND_PARAMS)
    except Exception as e:
        db.log_scan("趋势异常", f"{repr(e)[:120]}", trade_date=td)
        return None


def _is_trade_day(td):
    try:
        with dfetch.bs_session():
            tds = dfetch.get_trade_dates(
                (datetime.strptime(td, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d"), td)
        return td in tds
    except Exception:
        return datetime.strptime(td, "%Y-%m-%d").weekday() < 5


def settle_close(td):
    """收盘后用 baostock 完整日线跑 process_day（决策+T+1执行+选股+净值）。"""
    if td < SIM_START:
        db.log_scan("等待建仓", f"{td} 模拟起始{SIM_START}前，不结算", trade_date=td)
        return
    # 已结算过则跳过
    logs = db.get_scan_log(30)
    if any(l["phase"] == "收盘处理" and l["trade_date"] == td for l in logs):
        return
    if not _is_trade_day(td):
        db.log_scan("非交易日", f"{td} 非交易日，今日不结算", trade_date=td)
        return
    _ensure_recent_panel(td, force=True)
    if td not in set(dfetch.panel_dates()):
        db.log_scan("等待数据", f"{td} 日线未就绪(收盘后约15:30更新)，稍后重试", trade_date=td)
        return
    _ensure_concept_map(td)
    panel, names = dfetch.load_panel()
    group_map, src = dfetch.get_group_map()
    with dfetch.bs_session():
        index_df = dfetch.get_index(SIM_START, td)
        dates = [d for d in dfetch.get_trade_dates(SIM_START, td) if d in set(dfetch.panel_dates())]
        trend_states = _compute_trend(td)
    if td not in dates:
        # SIM_START 当日可能不在区间，补进
        if td not in dates:
            dates = sorted(set(dates) | {td})

    # 趋势总闸：D 收盘状态 + 上一交易日收盘状态（控开仓/退守）
    trend_on = trend_on_prev = None
    if trend_states:
        prev_td = dates[dates.index(td) - 1] if td in dates and dates.index(td) > 0 else None
        trend_on = tr.state_on(trend_states, td)
        trend_on_prev = tr.state_on(trend_states, prev_td) if prev_td else trend_on
        db.set_meta("trend", {
            "date": td, "on": bool(trend_on),
            "bench": "沪深300", "rule": "MA200迟滞±3%",
            "on_pct_recent": tr.describe(trend_states)["on_pct"],
        })
        db.log_scan("趋势择时",
                    f"{td} 沪深300 MA200迟滞±3% -> {'ON可交易' if trend_on else 'OFF退守现金'}",
                    trade_date=td)

    res = engine.process_day(panel, names, group_map, index_df, dates, td, log=True,
                             trend_on=trend_on, trend_on_prev=trend_on_prev)
    db.log_scan("结算完成",
                f"{td} 热门板块{len(res['hot_sectors'])} "
                f"买{len(res['buys'])}卖{len(res['sells'])} "
                f"持仓{len(db.get_positions())}/{st.MAX_POSITIONS} 候选{len(res['candidates'])}"
                f"{'' if trend_on is None else (' 趋势ON' if trend_on else ' 趋势OFF退守')}",
                trade_date=td)


def scan_once():
    now = _now()
    td = _today()
    hm = now.hour * 100 + now.minute
    # 收盘后(15:05之后)做日线结算
    if now.weekday() < 5 and 1505 <= hm <= 2359:
        settle_close(td)
    elif now.weekday() < 5 and 900 <= hm < 1505:
        if now.minute < 10:
            db.log_scan("盘中观望", f"{td} 动量轮动为收盘后结算模型，盘中不交易", trade_date=td)


def main():
    db.init_db()
    db.log_scan("启动",
                f"调度器启动：动量轮动，每日收盘后日线结算(T日选股/T+1开盘执行)，"
                f"模拟起始{SIM_START}", trade_date=_today())
    print("momentum scheduler started")
    while True:
        try:
            scan_once()
        except Exception as e:
            try:
                db.log_scan("异常", f"{repr(e)[:160]}", trade_date=_today())
            except Exception:
                pass
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
