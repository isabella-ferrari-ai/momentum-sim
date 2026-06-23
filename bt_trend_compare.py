# -*- coding: utf-8 -*-
"""对比「不择时」与多套趋势过滤规则在 7.5 年回测上的表现，择优。

用法: python3 bt_trend_compare.py [start] [end]
  默认区间取 backtest_panel.db 全集（2019~2026）。
沪深300 收盘缓存到 data/hs300_close.json，避免反复 baostock 登录。
"""
import warnings
warnings.filterwarnings("ignore")

import os
import sys
import json

import data_fetcher as dfetch
import backtest as bt
import metrics
import trend as tr

HS300 = "sh.000300"
CACHE = os.path.join(dfetch.BASE_DIR, "data", "hs300_close.json")


def load_hs300(start, end):
    """{date: close}，优先缓存。"""
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if d and min(d) <= start and max(d) >= end:
            return {k: v for k, v in d.items() if start <= k <= end}
    with dfetch.bs_session():
        df = dfetch.get_index(start, end, code=HS300)
    out = {r["date"]: float(r["close"]) for _, r in df.iterrows()}
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


VARIANTS = [
    ("无择时(基线)", None),
    ("MA200", dict(mode="ma", win=200)),
    ("MA120", dict(mode="ma", win=120)),
    ("MA200+斜率", dict(mode="ma_slope", win=200, slope_win=20)),
    ("双均线50/200", dict(mode="dual_ma", fast=50, slow=200)),
    ("MA200迟滞±3%", dict(mode="ma_hysteresis", win=200, band=0.03)),
    ("MA120迟滞±3%", dict(mode="ma_hysteresis", win=120, band=0.03)),
]


def main():
    panel_path = os.path.join(dfetch.BASE_DIR, "data", "backtest_panel.db")
    panel, names = dfetch.load_panel(panel_path)
    gmap = {c: [v] for c, v in dfetch.load_industry(panel_path).items() if v}
    dates = dfetch.panel_dates(panel_path)
    start = sys.argv[1] if len(sys.argv) > 1 else dates[0]
    end = sys.argv[2] if len(sys.argv) > 2 else dates[-1]
    print(f"[cmp] panel={len(panel)} dates={len(dates)} range={start}~{end}", flush=True)

    hs = load_hs300(start, end)
    print(f"[cmp] hs300 closes={len(hs)} ({min(hs)}~{max(hs)})", flush=True)

    # 预计算一次因子，所有变体共用（择时只改买卖闸门，不改因子）
    print("[cmp] precomputing factors once ...", flush=True)
    facts_by_date, rows_by_date = bt.precompute_factors(panel)
    bt.precompute_factors = lambda _p: (facts_by_date, rows_by_date)  # 复用，跳过重算

    rows = []
    for nm, params in VARIANTS:
        states = None
        on_pct = 100.0
        if params:
            states = tr.compute_states(hs, **params)
            on_pct = tr.describe(states)["on_pct"]
        res = bt.run(panel, names, gmap, dates, start=start, end=end,
                     log=False, trend_states=states)
        m = metrics.compute_all(res["equity"], res["trades"])
        rows.append((nm, on_pct, m, res))
        print(f"  {nm:<16} 持仓时间{on_pct:>5}% | 总收益{m['total_return_pct']:>7}% "
              f"CAGR{m['cagr_pct']:>6}% MaxDD{m['max_drawdown_pct']:>6}% "
              f"Sharpe{m['sharpe']:>5} Calmar{m['calmar']:>5} "
              f"胜率{m['win_rate']}% 盈亏比{m['profit_factor']} 笔{m['trade_count']}", flush=True)

    print("\n===== 分年度收益对比 =====")
    for nm, _, m, _ in rows:
        yr = m.get("yearly", {})
        print(f"  {nm:<16}", {k: yr[k] for k in sorted(yr)})

    # 选最佳：在正收益里取 Calmar 最高（回撤控制优先），全负则取 MaxDD 最小
    pos = [r for r in rows if r[2]["total_return_pct"] > 0 and r[1] < 100]
    if pos:
        best = max(pos, key=lambda r: r[2]["calmar"])
    else:
        best = min([r for r in rows if r[1] < 100],
                   key=lambda r: r[2]["max_drawdown_pct"])
    print(f"\n[cmp] 推荐变体: {best[0]} (Calmar={best[2]['calmar']} "
          f"MaxDD={best[2]['max_drawdown_pct']}% 总收益{best[2]['total_return_pct']}%)")


if __name__ == "__main__":
    main()
