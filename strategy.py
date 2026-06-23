# -*- coding: utf-8 -*-
"""策略引擎——热门板块成分股动量轮动（T日收盘选股，T+1开盘执行）。

选股流程（每日收盘）
====================
1. 板块热度：用申万/证监会行业分组，算各行业过去 5/20 日平均涨幅，取 Top 30% 热门板块；
2. 个股动量评分（仅在热门板块内）：
     MOM_5      20%   过去 5 日涨幅
     MOM_20     30%   过去 20 日涨幅（去掉最近 1 日）
     量价共振   25%   近 5 日量能扩张 × 上涨（放量上涨）
     相对板块强度 25% 个股 MOM_20 - 所在板块平均 MOM_20
   各因子在板块内排名归一化到 0-1，加权求综合分；
3. 选股：全市场综合分 Top 20 候选，过滤停牌/ST/成交额<1亿/跌停，同一行业最多 3 只；
4. 卖出：板块跌出热门 Top30% / 个股排名跌出全市场 Top30% / 回撤>-8% / 持有>15自然日。

执行：T 日收盘生成候选 -> T+1 开盘价买入（严格 T+1，不做盘中）。
"""
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime
import data_fetcher as dfetch

# ----------------------------- 资金/持仓约束 -----------------------------
MAX_POSITIONS = 10           # 同时最多持有
POSITION_PCT = 0.10          # 单票等权 10%
MAX_PER_INDUSTRY = 3         # 同一行业最多持有/候选数（分散）

# ----------------------------- 选股参数 -----------------------------
TOP_SECTOR_PCT = 0.30        # 热门板块比例（Top 30%）
TOP_N_CANDIDATES = 20        # 候选池规模
MIN_AMOUNT = 1e8             # 日成交额 > 1 亿（流动性）
MIN_BARS = 25                # 计算动量所需最少历史日数
SECTOR_MIN_MEMBERS = 3       # 板块至少 N 只成分才参与热度排名

# 因子权重（综合分 = 各因子板块内归一化排名 × 权重）
W_MOM5 = 0.20
W_MOM20 = 0.30
W_VOLPRICE = 0.25
W_RELSTR = 0.25

# ----------------------------- 卖出/风控 -----------------------------
STOP_LOSS = -0.08            # 持仓回撤 > -8%（收盘触发，次日开盘执行）
HOLD_MAX_DAYS = 15           # 最长持有 15 自然日
RANK_EXIT_PCT = 0.30         # 个股综合分跌出全市场 Top30% 则卖


# ==========================================================================
# 因子计算
# ==========================================================================
def _bars_upto(df, trade_date):
    """取 df 中 date<=trade_date 的部分（升序）。"""
    sub = df[df["date"] <= trade_date]
    return sub.reset_index(drop=True)


def compute_factors(df, trade_date):
    """对单只股票计算动量因子。返回 dict 或 None（数据不足/当日无bar/停牌）。
    因子: mom5, mom20, vol_price, （rel_str 需板块均值，稍后填）以及当日行情快照。"""
    sub = _bars_upto(df, trade_date)
    if len(sub) < MIN_BARS:
        return None
    last = sub.iloc[-1]
    if last["date"] != trade_date:
        return None  # 当日无 bar（停牌/未上市）
    if int(last.get("tradestatus", 1)) != 1:
        return None  # 停牌
    closes = sub["close"].values
    vols = sub["volume"].values
    i = len(sub) - 1

    mom5 = closes[i] / closes[i - 5] - 1 if closes[i - 5] > 0 else 0.0
    # MOM_20：过去20日涨幅，去掉最近1日（截至昨日）
    mom20 = closes[i - 1] / closes[i - 21] - 1 if closes[i - 21] > 0 else 0.0
    # 量价共振：近5日均量 / 前20日均量（量能扩张），再乘上涨因子（放量上涨）
    v_recent = vols[i - 4:i + 1].mean()
    v_base = vols[i - 24:i - 4].mean()
    vol_ratio5 = (v_recent / v_base) if v_base > 0 else 1.0
    vol_price = vol_ratio5 * (1.0 + max(mom5, 0.0))

    return {
        "mom5": float(mom5),
        "mom20": float(mom20),
        "vol_ratio5": float(vol_ratio5),
        "vol_price": float(vol_price),
        "close": float(last["close"]),
        "open": float(last["open"]),
        "pctChg": float(last.get("pctChg") or 0),
        "amount": float(last.get("amount") or 0),
        "turn": float(last.get("turn") or 0),
        "isST": int(last.get("isST") or 0),
    }


# ==========================================================================
# 板块热度
# ==========================================================================
def compute_sector_heat(facts, industry_map):
    """用各股因子按行业聚合，算板块过去 5/20 日平均涨幅。
    facts: {code: factor_dict}。返回 (sector_stats, hot_sectors)。
    sector_stats: {industry: {mom5, mom20, count}}；hot_sectors: 热门(Top30%)行业集合。"""
    buckets = {}
    for code, f in facts.items():
        ind = industry_map.get(code)
        if not ind:
            continue
        buckets.setdefault(ind, []).append(f)
    stats = {}
    for ind, fs in buckets.items():
        if len(fs) < SECTOR_MIN_MEMBERS:
            continue
        m5 = sum(x["mom5"] for x in fs) / len(fs)
        m20 = sum(x["mom20"] for x in fs) / len(fs)
        stats[ind] = {"mom5": m5, "mom20": m20, "count": len(fs)}
    if not stats:
        return {}, set()
    # 热度 = 5日与20日均涨幅的综合（5日权重高些，捕捉近期发酵）
    ranked = sorted(stats.items(), key=lambda kv: kv[1]["mom5"] * 0.6 + kv[1]["mom20"] * 0.4, reverse=True)
    n_hot = max(1, int(round(len(ranked) * TOP_SECTOR_PCT)))
    hot = {ind for ind, _ in ranked[:n_hot]}
    for ind, _ in ranked[:n_hot]:
        stats[ind]["hot"] = True
    return stats, hot


# ==========================================================================
# 个股评分（板块内归一化排名）
# ==========================================================================
def _rank_norm(values):
    """把一组数值转为 0-1 的百分位排名（值越大排名越高）。返回与输入等长 list。"""
    n = len(values)
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda k: values[k])
    norm = [0.0] * n
    for rank, idx in enumerate(order):
        norm[idx] = rank / (n - 1)
    return norm


def score_universe(facts, industry_map, sector_stats, hot_sectors):
    """对热门板块内个股计算综合动量分。返回 list[dict]（含 code/industry/score/各因子）。
    各因子在所属板块内归一化排名(0-1)后加权。相对板块强度 = 个股 MOM_20 - 板块均 MOM_20。"""
    # 按热门板块分桶
    by_sector = {}
    for code, f in facts.items():
        ind = industry_map.get(code)
        if not ind or ind not in hot_sectors:
            continue
        by_sector.setdefault(ind, []).append((code, f))

    scored = []
    for ind, items in by_sector.items():
        sec_mom20 = sector_stats[ind]["mom20"]
        codes = [c for c, _ in items]
        fs = [f for _, f in items]
        rel = [f["mom20"] - sec_mom20 for f in fs]
        r_m5 = _rank_norm([f["mom5"] for f in fs])
        r_m20 = _rank_norm([f["mom20"] for f in fs])
        r_vp = _rank_norm([f["vol_price"] for f in fs])
        r_rel = _rank_norm(rel)
        for k, code in enumerate(codes):
            score = (W_MOM5 * r_m5[k] + W_MOM20 * r_m20[k]
                     + W_VOLPRICE * r_vp[k] + W_RELSTR * r_rel[k])
            f = fs[k]
            scored.append({
                "code": code, "industry": ind,
                "score": round(score * 100, 2),
                "mom5": round(f["mom5"] * 100, 2),
                "mom20": round(f["mom20"] * 100, 2),
                "vol_ratio5": round(f["vol_ratio5"], 2),
                "rel_str": round(rel[k] * 100, 2),
                "close": f["close"], "open": f["open"],
                "pctChg": round(f["pctChg"], 2),
                "amount": f["amount"], "turn": round(f["turn"], 2),
                "isST": f["isST"],
            })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ==========================================================================
# 过滤
# ==========================================================================
def _passes_filters(c, name):
    """停牌已在 compute_factors 排除；此处过滤 ST / 成交额 / 跌停。"""
    nm = (name or "")
    if c["isST"] or "ST" in nm.upper() or "退" in nm:
        return False, "ST/退市"
    if c["amount"] < MIN_AMOUNT:
        return False, f"成交额{c['amount']/1e8:.2f}亿<1亿"
    limit = dfetch.limit_pct(c["code"])
    if c["pctChg"] <= -(limit * 100 - 0.5):
        return False, "跌停"
    return True, ""


# ==========================================================================
# 主入口：选股
# ==========================================================================
def select_momentum_candidates(panel, names, industry_map, trade_date,
                               top_n=TOP_N_CANDIDATES, return_context=False):
    """每日收盘选股主函数。
    1. 全市场算因子 -> 2. 板块热度(Top30%) -> 3. 热门板块内评分 -> 4. 过滤+同行业≤3 -> Top N。
    return_context=True 时额外返回 (hot_sectors, sector_stats, full_scored) 供卖出判定与展示。"""
    facts = {}
    for code, df in panel.items():
        f = compute_factors(df, trade_date)
        if f is not None:
            facts[code] = f

    sector_stats, hot_sectors = compute_sector_heat(facts, industry_map)
    scored = score_universe(facts, industry_map, sector_stats, hot_sectors)

    # 过滤 + 同行业最多 3 只 + Top N
    cands = []
    per_ind = {}
    for c in scored:
        name = names.get(c["code"])
        ok, why = _passes_filters(c, name)
        if not ok:
            continue
        ind = c["industry"]
        if per_ind.get(ind, 0) >= MAX_PER_INDUSTRY:
            continue
        per_ind[ind] = per_ind.get(ind, 0) + 1
        cands.append({
            **c, "name": name, "strategy_type": "动量轮动",
            "reason": f"{ind}热门板块 动量分{c['score']} "
                      f"(M5={c['mom5']}%/M20={c['mom20']}%/量比{c['vol_ratio5']}/相对强度{c['rel_str']}%)",
        })
        if len(cands) >= top_n:
            break

    if return_context:
        return cands, hot_sectors, sector_stats, scored
    return cands


# ==========================================================================
# 卖出判定
# ==========================================================================
def _days_held(open_date, current_date):
    try:
        d0 = datetime.strptime(open_date, "%Y-%m-%d").date()
        d1 = datetime.strptime(current_date, "%Y-%m-%d").date()
        return (d1 - d0).days
    except Exception:
        return 0


def top_rank_codes(scored, pct=RANK_EXIT_PCT):
    """全市场综合分 Top pct 的代码集合（个股排名退出判定用）。"""
    if not scored:
        return set()
    n = max(1, int(round(len(scored) * pct)))
    return {c["code"] for c in scored[:n]}


def evaluate_sell(pos, row, current_date, hot_sectors, top30_codes):
    """决定持仓 pos 是否在次日开盘卖出。基于 T 日（current_date）收盘判定，
    次日开盘执行（与买入对称的 T+1）。返回 (do_sell, reason)。
    严格 T+1：买入当日不卖。
    row: 该股 current_date 日线行（dict），用于止损/回撤判定。"""
    if pos["open_date"] == current_date:
        return False, "T+1当日不可卖"
    days = _days_held(pos["open_date"], current_date)
    close = (row.get("close") if row else None) or pos.get("last_price") or pos["avg_cost"]
    ret = close / pos["avg_cost"] - 1

    # 1) 止损：回撤 > -8%
    if ret <= STOP_LOSS:
        return True, f"回撤{ret*100:.1f}%(≤-8%)止损"
    # 2) 最长持有到期
    if days >= HOLD_MAX_DAYS:
        return True, f"持有{days}天到期(≥15)清仓"
    # 3) 所在板块跌出热门 Top30%
    ind = pos.get("theme")
    if ind and hot_sectors and ind not in hot_sectors:
        return True, f"板块[{ind}]跌出热门Top30%"
    # 4) 个股综合分跌出全市场 Top30%
    if top30_codes and pos["code"] not in top30_codes:
        return True, "个股排名跌出全市场Top30%"
    return False, "继续持有"


# ==========================================================================
# 辅助
# ==========================================================================
def position_value(positions):
    return sum((p.get("last_price") or p["avg_cost"]) * p["shares"] for p in positions)


def lots(shares):
    return int(shares // 100 * 100)
