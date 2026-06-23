# -*- coding: utf-8 -*-
"""向量化回测引擎——在内存里跑「热门板块成分股动量轮动」，产出研究闭环全套指标。

与 engine.process_day 完全同构的 T+1 撮合（决策在 D 收盘，执行在 D+1 开盘），
但不落 SQLite、纯内存，便于跑多年、几千个交易日。

无未来函数
==========
- 候选/卖出决策只用 ≤D 的日线；
- 执行价用 D+1 开盘价；涨停开盘不买、跌停开盘不卖（顺延）。

数据诚实性声明（重要）
======================
- group_map（行业或概念）是「当前」快照，回测多年存在分组前视/幸存者偏差；
  概念无历史，故多年回测建议用行业分组，并把结论标注为「当前universe近似」。
- universe 取 panel 里实际有日线的股票（当前成分），多年回测有幸存者偏差。
结论解读需结合上述口径，详见 README「研究闭环」。
"""
import warnings
warnings.filterwarnings("ignore")

import os
import sys
import json

import strategy as st
import data_fetcher as dfetch

COMMISSION = 0.0003
STAMP_TAX = 0.0005
MIN_COMMISSION = 5.0
INITIAL_CAPITAL = 500_000.0


def _buy_cost(amount):
    return max(amount * COMMISSION, MIN_COMMISSION)


def _sell_cost(amount):
    return max(amount * COMMISSION, MIN_COMMISSION) + amount * STAMP_TAX


def _row_at(panel, code, date):
    df = panel.get(code)
    if df is None:
        return None
    sub = df[df["date"] == date]
    if sub.empty:
        return None
    r = sub.iloc[0].to_dict()
    r["code"] = code
    return r


def precompute_factors(panel):
    """一次性向量化预计算每只股票每个交易日的动量因子，避免回测中按日重切 DataFrame。
    返回:
      facts_by_date: {date: {code: factor_dict}}（与 strategy.compute_factors 输出同构）
      rows_by_date:  {date: {code: row_dict}}（撮合用 open/high/close/preclose）
    口径与 strategy.compute_factors 完全一致：MIN_BARS、去最近1日的MOM20、量比窗口、停牌过滤。"""
    import numpy as np
    facts_by_date = {}
    rows_by_date = {}
    MIN_BARS = st.MIN_BARS
    for code, df in panel.items():
        n = len(df)
        if n == 0:
            continue
        dates = df["date"].values
        o = df["open"].values.astype(float)
        h = df["high"].values.astype(float)
        c = df["close"].values.astype(float)
        pre = df["preclose"].values.astype(float)
        vol = df["volume"].values.astype(float)
        amt = df["amount"].values.astype(float)
        turn = df["turn"].values.astype(float) if "turn" in df else np.zeros(n)
        pct = df["pctChg"].values.astype(float) if "pctChg" in df else np.zeros(n)
        tstat = df["tradestatus"].values if "tradestatus" in df else np.ones(n)
        isst = df["isST"].values if "isST" in df else np.zeros(n)
        for i in range(n):
            d = dates[i]
            # 撮合行（任何交易日都需要，用于买卖执行）
            rows_by_date.setdefault(d, {})[code] = {
                "code": code, "open": o[i], "high": h[i], "low": float(df["low"].values[i]),
                "close": c[i], "preclose": pre[i],
            }
            if i < MIN_BARS:
                continue
            try:
                if int(float(tstat[i])) != 1:
                    continue
            except Exception:
                pass
            if c[i - 5] <= 0 or c[i - 21] <= 0:
                mom5 = 0.0 if c[i - 5] <= 0 else c[i] / c[i - 5] - 1
                mom20 = 0.0 if c[i - 21] <= 0 else c[i - 1] / c[i - 21] - 1
            else:
                mom5 = c[i] / c[i - 5] - 1
                mom20 = c[i - 1] / c[i - 21] - 1
            v_recent = vol[i - 4:i + 1].mean()
            v_base = vol[i - 24:i - 4].mean()
            vol_ratio5 = (v_recent / v_base) if v_base > 0 else 1.0
            vol_price = vol_ratio5 * (1.0 + max(mom5, 0.0))
            facts_by_date.setdefault(d, {})[code] = {
                "mom5": float(mom5), "mom20": float(mom20),
                "vol_ratio5": float(vol_ratio5), "vol_price": float(vol_price),
                "close": float(c[i]), "open": float(o[i]),
                "pctChg": float(pct[i]), "amount": float(amt[i]),
                "turn": float(turn[i]),
                "isST": int(float(isst[i])) if str(isst[i]).strip() not in ("", "nan") else 0,
            }
    return facts_by_date, rows_by_date


class Portfolio:
    def __init__(self, cash=INITIAL_CAPITAL):
        self.cash = cash
        self.initial = cash
        self.positions = {}      # code -> dict(shares, avg_cost, open_date, signal_date, theme, last_price, high)
        self.pending = {}        # code -> reason（D收盘标记，D+1开盘执行卖出）
        self.trades = []
        self.equity = []

    def market_value(self):
        return sum(p["last_price"] * p["shares"] for p in self.positions.values())

    def buy(self, code, name, theme, price, signal_date, execute_date, score, reason):
        if price <= 0:
            return False
        budget = min(self.initial * st.POSITION_PCT, self.cash * 0.98)
        shares = st.lots(budget / price)
        if shares < 100:
            return False
        amount = shares * price
        fee = _buy_cost(amount)
        if amount + fee > self.cash:
            shares = st.lots((self.cash * 0.98) / price)
            if shares < 100:
                return False
            amount = shares * price
            fee = _buy_cost(amount)
        self.cash -= amount + fee
        self.positions[code] = {
            "code": code, "name": name, "theme": theme, "shares": shares,
            "avg_cost": price, "open_date": execute_date, "signal_date": signal_date,
            "last_price": price, "high": price, "score": score,
        }
        self.trades.append({"side": "BUY", "code": code, "name": name, "theme": theme,
                            "execute_date": execute_date, "signal_date": signal_date,
                            "price": round(price, 3), "shares": shares,
                            "amount": round(amount, 2), "pnl": 0, "pnl_pct": 0,
                            "reason": reason})
        return True

    def sell(self, code, price, execute_date, reason):
        p = self.positions.get(code)
        if not p:
            return False
        amount = p["shares"] * price
        fee = _sell_cost(amount)
        pnl = amount - p["shares"] * p["avg_cost"] - fee
        pnl_pct = (price / p["avg_cost"] - 1) * 100
        self.cash += amount - fee
        self.trades.append({"side": "SELL", "code": code, "name": p.get("name"),
                            "theme": p.get("theme"), "execute_date": execute_date,
                            "open_date": p["open_date"], "signal_date": p.get("signal_date"),
                            "price": round(price, 3), "shares": p["shares"],
                            "amount": round(amount, 2), "pnl": round(pnl, 2),
                            "pnl_pct": round(pnl_pct, 2), "reason": reason})
        del self.positions[code]
        return True


def run(panel, names, group_map, dates, start=None, end=None,
        prog_every=60, log=True, trend_states=None):
    """跑回测。dates 为升序交易日全集；start/end 限定回测区间（含）。
    trend_states: {date: bool} 市场择时状态（None=不择时）。OFF 时不开新仓且清仓退守现金。
    返回 dict(equity, trades, candidates_last)。"""
    import trend as tr
    pf = Portfolio()
    if log:
        print(f"[bt] precomputing factors for {len(panel)} stocks ...", flush=True)
    facts_by_date, rows_by_date = precompute_factors(panel)
    if log:
        print(f"[bt] precompute done ({len(facts_by_date)} dated factor sets)", flush=True)

    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    prev_cands = []
    prev_date = None

    def _row(code, td):
        return rows_by_date.get(td, {}).get(code)

    for i, td in enumerate(dates):
        # 1) D日选股 + 板块热度 + 全市场评分（决策依据，用预计算因子，只含≤D数据）
        facts = facts_by_date.get(td, {})
        cands, hot, sector_stats, scored = st.select_momentum_candidates(
            panel, names, group_map, td, return_context=True, facts=facts)
        top30 = st.top_rank_codes(scored)

        # 2) 卖出执行：上一交易日收盘标记的 pending 在 D 开盘价卖出
        for code in list(pf.pending.keys()):
            row = _row(code, td)
            if row is None:
                continue
            px = row["open"]
            limit = dfetch.limit_pct(code)
            preclose = row.get("preclose") or pf.positions.get(code, {}).get("avg_cost", 0)
            if preclose and (px / preclose - 1) <= -(limit - 0.005):
                continue  # 跌停开盘卖不出，顺延
            if pf.sell(code, px, td, pf.pending[code]):
                del pf.pending[code]

        # 3) 买入执行：上一交易日候选在 D 开盘价买入（等权，最多10只）
        #    择时：仅当「上一交易日收盘」趋势 ON 才允许开新仓（与候选同源 T+1）
        trend_ok_prev = (trend_states is None) or (
            prev_date is not None and tr.state_on(trend_states, prev_date))
        if prev_date and prev_cands and trend_ok_prev:
            for c in prev_cands:
                if len(pf.positions) >= st.MAX_POSITIONS:
                    break
                code = c["code"]
                if code in pf.positions:
                    continue
                row = _row(code, td)
                if row is None:
                    continue
                limit = dfetch.limit_pct(code)
                preclose = row.get("preclose") or 0
                if preclose and (row["open"] / preclose - 1) >= (limit - 0.005):
                    continue  # 涨停开盘买不进
                pf.buy(code, c.get("name"), c.get("industry"), row["open"],
                       prev_date, td, c.get("score"),
                       f"动量候选#{c.get('rank', '')} {c.get('industry')}")

        # 4) 更新持仓现价 + 卖出决策（D收盘判定，标记 pending）
        #    择时：D 收盘趋势 OFF -> 全部持仓退守（次日开盘清仓），优先于个股卖出逻辑
        trend_off = (trend_states is not None) and (not tr.state_on(trend_states, td))
        for code, p in list(pf.positions.items()):
            row = _row(code, td)
            if row is not None:
                p["high"] = max(p["high"], row["high"])
                p["last_price"] = row["close"]
            if p["open_date"] == td:
                continue  # T+1 当日不决策
            if trend_off:
                pf.pending[code] = "大盘趋势破坏-退守现金"
                continue
            do_sell, reason = st.evaluate_sell(p, row, td, hot, top30)
            if do_sell:
                pf.pending[code] = reason

        # 5) 候选池滚动 + 净值
        prev_cands = cands
        for j, c in enumerate(cands):
            c["rank"] = j + 1
        prev_date = td
        total = pf.cash + pf.market_value()
        pf.equity.append({"trade_date": td, "cash": round(pf.cash, 2),
                          "market_value": round(pf.market_value(), 2),
                          "total_equity": round(total, 2)})
        if log and prog_every and (i % prog_every == 0):
            print(f"[bt] {td} equity={total:,.0f} pos={len(pf.positions)} "
                  f"hot={len(hot)} ({i+1}/{len(dates)})")

    return {"equity": pf.equity, "trades": pf.trades, "candidates_last": prev_cands}


def _load_benches(start, end):
    """拉基准指数日线 -> {name: [dict(trade_date,total)]}。沪深300/中证1000/上证(近似万得全A)。"""
    out = {}
    codes = {"沪深300": "sh.000300", "中证1000": "sh.000852", "上证综指": "sh.000001"}
    with dfetch.bs_session():
        for name, code in codes.items():
            try:
                df = dfetch.get_index(start, end, code=code)
                if df is not None and not df.empty:
                    out[name] = [{"trade_date": r["date"], "total": float(r["close"])}
                                 for _, r in df.iterrows()]
            except Exception as e:
                print(f"[bench] {name} ERR {repr(e)[:80]}")
    return out


def main():
    import metrics
    panel_path = os.environ.get("BT_PANEL", dfetch.PANEL_DB)
    start = sys.argv[1] if len(sys.argv) > 1 else None
    end = sys.argv[2] if len(sys.argv) > 2 else None
    group = os.environ.get("BT_GROUP", "industry")   # industry | concept

    print(f"[bt] panel={panel_path} group={group} range={start}~{end}")
    panel, names = dfetch.load_panel(panel_path)
    if group == "concept":
        gmap = dfetch.load_concept_map(panel_path) or {}
        src = "concept"
        if not gmap:
            gmap = {c: [v] for c, v in dfetch.load_industry(panel_path).items() if v}
            src = "industry(fallback)"
    else:
        gmap = {c: [v] for c, v in dfetch.load_industry(panel_path).items() if v}
        src = "industry"
    dates = dfetch.panel_dates(panel_path)
    print(f"[bt] codes={len(panel)} dates={len(dates)} group_src={src} "
          f"({dates[0]}~{dates[-1]})")

    # 趋势总闸（BT_TREND=1 启用，默认与实盘一致：沪深300 MA200迟滞±3%）
    trend_states = None
    if os.environ.get("BT_TREND") == "1":
        import trend as tr
        from datetime import datetime, timedelta
        # 多拉 ~420 日历日的前置，保证回测窗口起点 MA200 已成型（短窗口回测尤其重要）
        ws = (start or dates[0])
        lb = (datetime.strptime(ws, "%Y-%m-%d") - timedelta(days=420)).strftime("%Y-%m-%d")
        with dfetch.bs_session():
            idf = dfetch.get_index(lb, dates[-1], code="sh.000300")
        closes = {r["date"]: float(r["close"]) for _, r in idf.iterrows()}
        trend_states = tr.compute_states(closes, mode="ma_hysteresis", win=200, band=0.03)
        src += "+趋势MA200迟滞±3%"
        print(f"[bt] trend filter ON (lookback {lb}): {tr.describe(trend_states)}")

    res = run(panel, names, gmap, dates, start=start, end=end, trend_states=trend_states)
    eq = res["equity"]
    if not eq:
        print("[bt] no equity produced"); return
    bstart, bend = eq[0]["trade_date"], eq[-1]["trade_date"]
    benches = _load_benches(bstart, bend)
    regime_bench = benches.get("沪深300")
    m = metrics.compute_all(eq, res["trades"], benches=benches, regime_bench=regime_bench)
    m["group_source"] = src
    m["panel"] = panel_path

    # 基准曲线归一化到初始资金，便于和策略净值同图对比
    bench_curves = {}
    base = eq[0]["total_equity"]
    for nm, series in benches.items():
        smap = {b["trade_date"]: b["total"] for b in series}
        b0 = next((smap[e["trade_date"]] for e in eq if e["trade_date"] in smap), None)
        if not b0:
            continue
        bench_curves[nm] = [
            {"trade_date": e["trade_date"],
             "value": round(base * smap[e["trade_date"]] / b0, 2)}
            for e in eq if e["trade_date"] in smap
        ]

    out = {"metrics": m, "equity": eq, "bench_curves": bench_curves, "trades": res["trades"]}
    out_path = os.path.join(dfetch.BASE_DIR, "data", f"backtest_{group}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    # 摘要
    print("\n===== 回测摘要 ({} | {}) =====".format(src, f"{bstart}~{bend}"))
    for k in ["total_return_pct", "cagr_pct", "max_drawdown_pct", "calmar",
              "sharpe", "sortino", "win_rate", "profit_factor", "avg_win_pct",
              "avg_loss_pct", "avg_hold_days", "annual_turnover", "trade_count"]:
        print(f"  {k}: {m.get(k)}")
    print("  分年度:", m.get("yearly"))
    if m.get("excess"):
        for nm, ex in m["excess"].items():
            if ex:
                print(f"  超额[{nm}]: 策略{ex['strat_cum_pct']}% vs 基准{ex['bench_cum_pct']}% "
                      f"= 超额{ex['excess_cum_pct']}% (年化超额{ex['excess_cagr_pct']}%)")
    if m.get("regimes"):
        print("  牛熊震荡:", {k: f"{v['cum_pct']}%({v['days']}d)" for k, v in m["regimes"].items()})
    print(f"\n[bt] saved -> {out_path}")


if __name__ == "__main__":
    main()
