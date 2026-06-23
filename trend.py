# -*- coding: utf-8 -*-
"""市场择时（趋势过滤）——只在大盘趋势成立时让动量策略出手，趋势破坏时退守现金。

回测已证明：纯追涨动量在 A 股震荡市(-68%)与熊市(-29%)长期失血，只在趋势市(+75%)赚钱。
本模块用一个稳健的量化趋势指标判断「趋势是否还在」，给主策略加一道总闸：
  - 趋势 ON ：照常选股/持仓；
  - 趋势 OFF：不开新仓，且清掉存量持仓（退守现金），把震荡/熊市的回撤摁下去。

判定基准默认沪深300（A 股核心宽基），决策只用 ≤D 的收盘，无未来函数。

提供多套规则供回测对比择优：
  ma           : close > MA(win) 即 ON（最朴素）
  ma_slope     : close > MA(win) 且 MA 上行（去掉走平段假信号）
  dual_ma      : 快线MA > 慢线MA（金叉/死叉）
  ma_hysteresis: 迟滞——close 上穿 MA*(1+band) 才 ON，下穿 MA*(1-band) 才 OFF（抗来回打脸）

统一入口 compute_states(closes_by_date, mode=..., **params) -> {date: bool}。
"""


def _sma(vals, i, win):
    """vals[i-win+1 .. i] 的简单均值；样本不足返回 None。"""
    if i + 1 < win:
        return None
    s = 0.0
    for k in range(i - win + 1, i + 1):
        s += vals[k]
    return s / win


def _series(closes_by_date):
    """{date: close} 或 [(date, close)] -> (dates 升序, closes 对齐)。"""
    if isinstance(closes_by_date, dict):
        items = sorted(closes_by_date.items())
    else:
        items = sorted(closes_by_date)
    dates = [d for d, _ in items]
    closes = [float(c) for _, c in items]
    return dates, closes


def compute_states(closes_by_date, mode="ma_hysteresis", win=200, fast=50,
                   slow=200, band=0.0, slope_win=20, warmup_on=True):
    """计算每个交易日的趋势状态（True=ON 可交易, False=OFF 退守现金）。

    参数:
      mode        : ma | ma_slope | dual_ma | ma_hysteresis
      win         : 单均线窗口（ma / ma_slope / ma_hysteresis）
      fast, slow  : 双均线窗口（dual_ma）
      band        : 迟滞带宽（ma_hysteresis），如 0.02 表示 ±2%
      slope_win   : 斜率判定回看天数（ma_slope）
      warmup_on   : 均线样本不足的预热期是否视为 ON（默认 True，避免回测早段被全砍）

    返回 {date: bool}，长度与输入交易日一致。
    """
    dates, closes = _series(closes_by_date)
    n = len(closes)
    states = {}
    prev = True  # 迟滞用：上一日状态

    for i in range(n):
        d = dates[i]
        if mode == "dual_ma":
            mf = _sma(closes, i, fast)
            ms = _sma(closes, i, slow)
            if mf is None or ms is None:
                states[d] = warmup_on
                prev = warmup_on
                continue
            st = mf > ms

        elif mode == "ma_slope":
            ma = _sma(closes, i, win)
            ma_prev = _sma(closes, i - slope_win, win) if i - slope_win >= 0 else None
            if ma is None:
                states[d] = warmup_on
                prev = warmup_on
                continue
            up = (ma_prev is None) or (ma > ma_prev)
            st = (closes[i] > ma) and up

        elif mode == "ma_hysteresis":
            ma = _sma(closes, i, win)
            if ma is None:
                states[d] = warmup_on
                prev = warmup_on
                continue
            hi = ma * (1.0 + band)
            lo = ma * (1.0 - band)
            if prev:                       # 当前 ON：跌破下轨才转 OFF
                st = closes[i] >= lo
            else:                          # 当前 OFF：站上上轨才转 ON
                st = closes[i] > hi

        else:  # "ma"
            ma = _sma(closes, i, win)
            if ma is None:
                states[d] = warmup_on
                prev = warmup_on
                continue
            st = closes[i] > ma

        states[d] = st
        prev = st

    return states


def state_on(states, date, default=True):
    """查某日趋势状态，缺失时回退到该日之前最近一个已知状态（再不行用 default）。"""
    if date in states:
        return states[date]
    prior = [d for d in states if d <= date]
    if prior:
        return states[max(prior)]
    return default


def describe(states):
    """统计 ON/OFF 天数占比，便于回测日志。"""
    if not states:
        return {"n": 0, "on": 0, "off": 0, "on_pct": 0.0}
    on = sum(1 for v in states.values() if v)
    n = len(states)
    return {"n": n, "on": on, "off": n - on, "on_pct": round(on / n * 100, 1)}
