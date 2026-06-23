# -*- coding: utf-8 -*-
"""策略引擎——热门概念板块成分股动量轮动（T日收盘选股，T+1集合竞价买入，符合条件随时卖出）。

板块分组用「概念板块」（新浪 gn_*：机器人/低空经济/算力/华为汽车等跨行业短线题材），
比证监会行业分类更贴合 A 股炒作逻辑。一只股票可属于多个概念，归到其「最热概念」分组。
概念数据获取失败时退化为行业分类（每股一个分组），主流程不变。

选股流程（每日收盘）
====================
1. 板块热度：把每只股票计入其所属的每个概念，算各概念过去 5/20 日平均涨幅，取 Top 30% 热门概念；
2. 个股归组：一只股票可属多个热门概念，取其中「最热概念」作为该股分组；
3. 个股动量评分（仅在热门概念内）：
     MOM_5      20%   过去 5 日涨幅
     MOM_20     30%   过去 20 日涨幅（去掉最近 1 日）
     量价共振   25%   近 5 日量能扩张 × 上涨（放量上涨）
     相对板块强度 25% 个股 MOM_20 - 所在概念平均 MOM_20
   各因子在概念内排名归一化到 0-1，加权求综合分；
4. 选股：全市场综合分 Top 20 候选，过滤停牌/ST/成交额<1亿/跌停，同一概念最多 3 只，
   同一股票去重（按最高分保留）；
5. 卖出：成本止损-8% / 固定止盈+30% / 高点回撤-12%(浮盈+8%后启用) / 概念退出 /
   个股排名跌出全市场 Top30% / 持有>15自然日。

执行：买入 T 日收盘生成候选 -> T+1 集合竞价(≈开盘价)成交（严格 T+1）；
卖出快信号(成本止损-8%/固定止盈+30%/高点回撤-12%)盘中实时随时成交，
慢信号(概念退出/排名退出/到期)收盘决策、次日开盘卖。
"""
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime
import data_fetcher as dfetch

# ----------------------------- 资金/持仓约束 -----------------------------
MAX_POSITIONS = 10           # 同时最多持有
POSITION_PCT = 0.10          # 单票等权 10%
MAX_PER_GROUP = 3            # 同一概念最多持有/候选数（分散）

# ----------------------------- 选股参数 -----------------------------
TOP_SECTOR_PCT = 0.30        # 热门概念比例（Top 30%）
TOP_N_CANDIDATES = 20        # 候选池规模
MIN_AMOUNT = 1e8             # 日成交额 > 1 亿（流动性）
MIN_BARS = 25                # 计算动量所需最少历史日数
SECTOR_MIN_MEMBERS = 5       # 概念至少 N 只成分才参与热度排名（概念股池更大，门槛抬高去噪）

# 因子权重（综合分 = 各因子概念内归一化排名 × 权重）
W_MOM5 = 0.20
W_MOM20 = 0.30
W_VOLPRICE = 0.25
W_RELSTR = 0.25

# ----------------------------- 卖出/风控 -----------------------------
# 即时风控（盘中实时价触发，符合条件随时卖出，不等次日开盘）
STOP_LOSS = -0.08            # 成本硬止损：相对买入成本回撤 ≤ -8%
TRAIL_STOP = -0.12           # 高点回撤止盈：自持仓最高价回撤 ≤ -12%（保护利润/追踪止损）
TRAIL_ARM_PROFIT = 0.08      # 浮盈达 +8% 后才启用高点回撤止盈（避免一进场就被小回撤打掉）
TAKE_PROFIT = 0.30           # 固定止盈：浮盈 ≥ +30% 落袋

# 慢信号（收盘判定，次日集合竞价卖出）
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
    因子: mom5, mom20, vol_price, （rel_str 需概念均值，稍后填）以及当日行情快照。"""
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
# 概念板块热度
# ==========================================================================
def _concepts_of(group_map, code):
    """取 code 的概念列表（兼容 group_map 值为 list 或单个字符串）。"""
    v = group_map.get(code)
    if not v:
        return []
    return v if isinstance(v, list) else [v]


def compute_sector_heat(facts, group_map):
    """把每只股票计入其所属的每个概念，算各概念过去 5/20 日平均涨幅。
    facts: {code: factor_dict}；group_map: {code: [概念,...]}。
    返回 (sector_stats, hot_sectors)。
    sector_stats: {概念: {mom5, mom20, count, heat, hot}}；hot_sectors: 热门(Top30%)概念集合。"""
    buckets = {}
    for code, f in facts.items():
        for cpt in _concepts_of(group_map, code):
            buckets.setdefault(cpt, []).append(f)
    stats = {}
    for cpt, fs in buckets.items():
        if len(fs) < SECTOR_MIN_MEMBERS:
            continue
        m5 = sum(x["mom5"] for x in fs) / len(fs)
        m20 = sum(x["mom20"] for x in fs) / len(fs)
        # 热度 = 5日与20日均涨幅的综合（5日权重高些，捕捉近期发酵）
        stats[cpt] = {"mom5": m5, "mom20": m20, "count": len(fs),
                      "heat": m5 * 0.6 + m20 * 0.4}
    if not stats:
        return {}, set()
    ranked = sorted(stats.items(), key=lambda kv: kv[1]["heat"], reverse=True)
    n_hot = max(1, int(round(len(ranked) * TOP_SECTOR_PCT)))
    hot = {cpt for cpt, _ in ranked[:n_hot]}
    for cpt in hot:
        stats[cpt]["hot"] = True
    return stats, hot


def assign_group(group_map, code, sector_stats, hot_sectors):
    """给 code 归组：在其所属【热门】概念中取最热（heat 最高）的那个。
    若它不属于任何热门概念，返回 None。"""
    best, best_heat = None, None
    for cpt in _concepts_of(group_map, code):
        if cpt in hot_sectors:
            h = sector_stats[cpt]["heat"]
            if best_heat is None or h > best_heat:
                best, best_heat = cpt, h
    return best


# ==========================================================================
# 个股评分（概念内归一化排名）
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


def score_universe(facts, group_map, sector_stats, hot_sectors):
    """对热门概念内个股计算综合动量分。每只股票归到其最热概念（去重），返回 list[dict]。
    各因子在所属概念内归一化排名(0-1)后加权。相对板块强度 = 个股 MOM_20 - 概念均 MOM_20。"""
    # 每只股票归到唯一的最热概念
    by_group = {}
    for code, f in facts.items():
        grp = assign_group(group_map, code, sector_stats, hot_sectors)
        if grp is None:
            continue
        by_group.setdefault(grp, []).append((code, f))

    scored = []
    for grp, items in by_group.items():
        sec_mom20 = sector_stats[grp]["mom20"]
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
                "code": code, "industry": grp,   # industry 字段沿用：此处为概念名
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
    # 概念内排名归一化会让每个概念头部都拿满分（小概念尤甚），
    # 故并列时以原始 MOM_20 为次序键，保证候选排序有意义。
    scored.sort(key=lambda x: (x["score"], x["mom20"]), reverse=True)
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
def select_momentum_candidates(panel, names, group_map, trade_date,
                               top_n=TOP_N_CANDIDATES, return_context=False, facts=None):
    """每日收盘选股主函数。
    1. 全市场算因子 -> 2. 概念热度(Top30%) -> 3. 个股归最热概念并评分 -> 4. 过滤+同概念≤3 -> Top N。
    group_map: {code: [概念,...]}（概念分组）或 {code: 行业名}（退化）。
    return_context=True 时额外返回 (hot_sectors, sector_stats, full_scored) 供卖出判定与展示。
    facts: 预计算好的 {code: factor_dict}（回测加速用，传入则跳过逐股计算）。"""
    if facts is None:
        facts = {}
        for code, df in panel.items():
            f = compute_factors(df, trade_date)
            if f is not None:
                facts[code] = f

    sector_stats, hot_sectors = compute_sector_heat(facts, group_map)
    scored = score_universe(facts, group_map, sector_stats, hot_sectors)

    # 过滤 + 同概念最多 3 只 + 同股去重 + Top N
    cands = []
    per_grp = {}
    seen = set()
    for c in scored:
        if c["code"] in seen:          # 同股去重（scored 已按分降序，保留最高分那条）
            continue
        name = names.get(c["code"])
        ok, why = _passes_filters(c, name)
        if not ok:
            continue
        grp = c["industry"]
        if per_grp.get(grp, 0) >= MAX_PER_GROUP:
            continue
        per_grp[grp] = per_grp.get(grp, 0) + 1
        seen.add(c["code"])
        cands.append({
            **c, "name": name, "strategy_type": "动量轮动",
            "reason": f"{grp}热门概念 动量分{c['score']} "
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


def evaluate_risk_exit(avg_cost, last_price, high_since_open):
    """即时风控判定（成本止损 / 高点回撤止盈 / 固定止盈）。
    与时间无关，盘中实时价或收盘价都可调用。返回 (do_sell, reason) 或 (False, "")。
    注意：调用方需自行保证「严格 T+1：买入当日不卖」。"""
    if not avg_cost or avg_cost <= 0 or not last_price or last_price <= 0:
        return False, ""
    ret = last_price / avg_cost - 1
    # 1) 成本硬止损
    if ret <= STOP_LOSS:
        return True, f"成本止损 回撤{ret*100:.1f}%(≤{STOP_LOSS*100:.0f}%)"
    # 2) 固定止盈
    if ret >= TAKE_PROFIT:
        return True, f"止盈 浮盈+{ret*100:.1f}%(≥{TAKE_PROFIT*100:.0f}%)"
    # 3) 高点回撤止盈（浮盈达标后启用追踪止损）
    high = high_since_open or avg_cost
    if high > 0 and (high / avg_cost - 1) >= TRAIL_ARM_PROFIT:
        draw = last_price / high - 1
        if draw <= TRAIL_STOP:
            return True, f"高点回撤止盈 自高点{draw*100:.1f}%(≤{TRAIL_STOP*100:.0f}%)"
    return False, ""


def evaluate_intraday_sell(pos, last_price, current_date):
    """盘中实时卖出判定——仅即时风控（止损/止盈/回撤），符合条件任意时段触发。
    慢信号（概念退出/排名退出/到期）留给收盘 evaluate_sell。
    严格 T+1：买入当日不卖。返回 (do_sell, reason)。"""
    if pos["open_date"] == current_date:
        return False, "T+1当日不可卖"
    high = max(pos.get("high_since_open") or pos["avg_cost"], last_price)
    return evaluate_risk_exit(pos["avg_cost"], last_price, high)


def evaluate_sell(pos, row, current_date, hot_sectors, top30_codes):
    """决定持仓 pos 是否卖出。基于 T 日（current_date）收盘判定。
    即时风控（止损/止盈/回撤）命中则即时执行；慢信号（到期/概念退出/排名退出）
    标记次日集合竞价卖出。返回 (do_sell, reason)。
    严格 T+1：买入当日不卖。
    row: 该股 current_date 日线行（dict），用于止损/回撤判定。"""
    if pos["open_date"] == current_date:
        return False, "T+1当日不可卖"
    days = _days_held(pos["open_date"], current_date)
    close = (row.get("close") if row else None) or pos.get("last_price") or pos["avg_cost"]
    high = max(pos.get("high_since_open") or pos["avg_cost"],
               (row.get("high") if row else None) or 0, close)

    # 1) 即时风控（成本止损/固定止盈/高点回撤止盈）
    do_sell, reason = evaluate_risk_exit(pos["avg_cost"], close, high)
    if do_sell:
        return True, reason
    # 2) 最长持有到期
    if days >= HOLD_MAX_DAYS:
        return True, f"持有{days}天到期(≥{HOLD_MAX_DAYS})清仓"
    # 3) 所属概念跌出热门 Top30%
    grp = pos.get("theme")
    if grp and hot_sectors and grp not in hot_sectors:
        return True, f"概念[{grp}]跌出热门Top30%"
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
