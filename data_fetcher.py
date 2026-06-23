# -*- coding: utf-8 -*-
"""数据获取层——热门板块动量轮动（纯日线，T日收盘选股，T+1开盘执行）。

数据源：baostock 日线（免费、稳定，含 isST/tradestatus/turn 字段）。
- 日线面板缓存到本地 SQLite（data/panel.db），可断点续传。
- 股票池：沪深300 + 中证500 + 中证1000 成分股并集（约 1800 只）。
- 板块分组：新浪概念板块（gn_*），跨行业短线题材（机器人/低空经济/算力/华为等），
  比证监会行业分类更贴合 A 股炒作逻辑；缓存到 concept_map 表，每日收盘后刷新。
  概念获取失败时退化为 baostock query_stock_industry() 行业分类（不影响主流程）。

与 trading-sim 完全独立（独立的 panel.db）。不需要实时快照——本策略不做盘中，
每日收盘后用完整日线评估，次日开盘价执行。
"""
import warnings
warnings.filterwarnings("ignore")

import os
import time
import json
import sqlite3
import urllib.request
from contextlib import contextmanager

import baostock as bs
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_DB = os.path.join(BASE_DIR, "data", "panel.db")

INDEX_CODE = "sh.000001"   # 上证指数（大盘基准）

# baostock 日线字段
K_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,isST"
NUM_COLS = ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg"]


# --------------------------------------------------------------------------
# 代码工具
# --------------------------------------------------------------------------
def limit_pct(code):
    """当日涨跌停幅度阈值。主板10%，创业板(30)/科创板(68)20%，北交所30%(不纳入)。"""
    c = code.split(".")[-1] if "." in code else code
    if c.startswith("68") or c.startswith("30"):
        return 0.20
    if c.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def to_bs_code(code):
    """六位代码 -> baostock 格式 sh./sz.。"""
    if "." in code:
        return code
    if code.startswith(("6", "9")):
        return "sh." + code
    if code.startswith(("0", "3", "2")):
        return "sz." + code
    if code.startswith(("8", "4")):
        return "bj." + code
    return "sz." + code


# --------------------------------------------------------------------------
# baostock 会话
# --------------------------------------------------------------------------
@contextmanager
def bs_session():
    lg = bs.login()
    try:
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        yield
    finally:
        bs.logout()


def _kdata(code, start, end, adjust="3"):
    """单只日线（adjust: 1后复权 2前复权 3不复权）。动量计算用不复权贴合真实涨跌。"""
    rs = bs.query_history_k_data_plus(
        code, K_FIELDS, start_date=start, end_date=end, frequency="d", adjustflag=adjust
    )
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame(columns=K_FIELDS.split(","))
    df = pd.DataFrame(rows, columns=rs.fields)
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    return df


def get_bars(code, start, end, adjust="3"):
    return _kdata(to_bs_code(code), start, end, adjust=adjust)


def get_index(start, end, code=INDEX_CODE):
    return _kdata(code, start, end, adjust="3")


def get_trade_dates(start, end):
    rs = bs.query_trade_dates(start_date=start, end_date=end)
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    if df.empty:
        return []
    return df[df["is_trading_day"] == "1"]["calendar_date"].tolist()


def get_all_basics():
    """全市场证券基础信息（含 ipoDate / type / status）。"""
    rs = bs.query_stock_basic()
    rows = []
    while (rs.error_code == "0") and rs.next():
        rows.append(rs.get_row_data())
    return pd.DataFrame(rows, columns=rs.fields)


def get_stock_industry():
    """全市场行业分类。返回 {baostock代码: 行业名}。用于板块热度分组。"""
    rs = bs.query_stock_industry()
    out = {}
    while (rs.error_code == "0") and rs.next():
        r = rs.get_row_data()
        # 字段: updateDate, code, code_name, industry, industryClassification
        code, ind = r[1], (r[3] or "").strip()
        if code and ind:
            out[code] = ind
    return out


def _bs_index_codes(fn):
    """调用 baostock 指数成分函数，返回 baostock 代码集合(sh./sz.)。"""
    rs = getattr(bs, fn)()
    codes = set()
    while (rs.error_code == "0") and rs.next():
        row = rs.get_row_data()
        for v in row:
            if isinstance(v, str) and (v.startswith("sh.") or v.startswith("sz.")):
                codes.add(v)
                break
    return codes


def index_universe():
    """沪深300 + 中证500 + 中证1000 成分股并集（baostock 代码）。约 1800 只。
    HS300/ZZ500 用 baostock；CSI1000 用 akshare index_stock_cons_csindex('000852')。"""
    codes = set()
    try:
        codes |= _bs_index_codes("query_hs300_stocks")
    except Exception as e:
        print(f"[universe] hs300 ERR {repr(e)[:80]}")
    try:
        codes |= _bs_index_codes("query_zz500_stocks")
    except Exception as e:
        print(f"[universe] zz500 ERR {repr(e)[:80]}")
    try:
        import akshare as ak
        df1000 = ak.index_stock_cons_csindex(symbol="000852")
        for c in df1000["成分券代码"].astype(str):
            codes.add(to_bs_code(c.zfill(6)))
    except Exception as e:
        print(f"[universe] csi1000 ERR {repr(e)[:80]}")
    return codes


def tradable_universe(basics, as_of_date, min_listed_days=180):
    """筛出可交易股票池（剔除指数/B股/北交所/新股/退市）。
    返回 list[dict(code,name,ipoDate)]。ST 在日线层用名称/isST 过滤。"""
    out = []
    cutoff = pd.Timestamp(as_of_date) - pd.Timedelta(days=min_listed_days)
    for _, r in basics.iterrows():
        if str(r.get("type")) != "1":
            continue
        if str(r.get("status")) != "1":
            continue
        code = r["code"]
        c = code.split(".")[-1]
        if code.startswith("bj.") or c.startswith(("8", "4", "92")):
            continue
        if not c.startswith(("60", "00", "30", "68")):
            continue
        ipo = str(r.get("ipoDate") or "")
        if ipo:
            try:
                if pd.Timestamp(ipo) > cutoff:
                    continue
            except Exception:
                pass
        out.append({"code": code, "name": r.get("code_name"), "ipoDate": ipo})
    return out


# --------------------------------------------------------------------------
# 日线面板缓存（断点续传）
# --------------------------------------------------------------------------
def _panel_conn(path=PANEL_DB):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _panel_init(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bars (
            code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            preclose REAL, volume REAL, amount REAL, turn REAL,
            tradestatus INTEGER, pctChg REAL, isST INTEGER,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS basics (
            code TEXT PRIMARY KEY, name TEXT, ipoDate TEXT, industry TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_meta (
            code TEXT, start TEXT, end TEXT, fetched_at TEXT,
            PRIMARY KEY (code, start, end)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS concept_map (
            code TEXT PRIMARY KEY, concepts TEXT, fetched_date TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bars_date ON bars(date)")
    conn.commit()


def build_panel(start, end, lookback_days=40, path=PANEL_DB, progress_every=200):
    """抓取指数成分股日线到本地面板库（沪深300+中证500+中证1000）。
    lookback_days 为动量回溯（MOM_20 等）所需的前置历史。
    断点续传：已抓取过相同 (code,start,end) 的股票跳过。"""
    fetch_start = (pd.Timestamp(start) - pd.Timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
    conn = _panel_conn(path)
    _panel_init(conn)
    with bs_session():
        basics = get_all_basics()
        uni = tradable_universe(basics, start)
        idx_codes = index_universe()
        if idx_codes:
            uni = [u for u in uni if u["code"] in idx_codes]
            print(f"[panel] 沪深300+中证500+中证1000 共{len(idx_codes)}只 -> 可交易{len(uni)}只")
        try:
            ind_map = get_stock_industry()
        except Exception as e:
            print(f"[panel] industry ERR {repr(e)[:80]}")
            ind_map = {}
        conn.executemany(
            "INSERT OR REPLACE INTO basics(code,name,ipoDate,industry) VALUES(?,?,?,?)",
            [(u["code"], u["name"], u["ipoDate"], ind_map.get(u["code"], "")) for u in uni],
        )
        conn.commit()
        done = {r[0] for r in conn.execute(
            "SELECT code FROM fetch_meta WHERE start=? AND end=?", (fetch_start, end)
        ).fetchall()}
        todo = [u for u in uni if u["code"] not in done]
        print(f"[panel] universe={len(uni)} done={len(done)} todo={len(todo)} range={fetch_start}~{end}")
        n = 0
        for u in todo:
            code = u["code"]
            try:
                df = _kdata(code, fetch_start, end, adjust="3")
            except Exception as e:
                print(f"[panel] {code} ERR {repr(e)[:80]}")
                time.sleep(0.5)
                continue
            if not df.empty:
                recs = [
                    (code, r["date"], r["open"], r["high"], r["low"], r["close"],
                     r["preclose"], r["volume"], r["amount"], r["turn"],
                     int(float(r["tradestatus"])) if str(r["tradestatus"]).strip() not in ("", "nan") else 1,
                     r["pctChg"],
                     int(float(r["isST"])) if str(r["isST"]).strip() not in ("", "nan") else 0)
                    for _, r in df.iterrows()
                ]
                conn.executemany(
                    "INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", recs
                )
            conn.execute(
                "INSERT OR REPLACE INTO fetch_meta(code,start,end,fetched_at) VALUES(?,?,?,?)",
                (code, fetch_start, end, pd.Timestamp("now").isoformat()),
            )
            n += 1
            if n % progress_every == 0:
                conn.commit()
                print(f"[panel] {n}/{len(todo)} fetched ...")
        conn.commit()
    conn.close()
    print(f"[panel] done, fetched {n} new stocks")


def load_panel(path=PANEL_DB):
    """读出面板库为 {code: DataFrame(按date升序)} 与 {code: name}。"""
    conn = _panel_conn(path)
    bars = pd.read_sql_query("SELECT * FROM bars ORDER BY code,date", conn)
    basics = pd.read_sql_query("SELECT * FROM basics", conn)
    conn.close()
    bmap = {c: g.reset_index(drop=True) for c, g in bars.groupby("code")}
    nmap = {r["code"]: r["name"] for _, r in basics.iterrows()}
    return bmap, nmap


def load_industry(path=PANEL_DB):
    """读出 {baostock代码: 行业名}（板块热度分组用）。无则空 dict。"""
    try:
        conn = _panel_conn(path)
        rows = conn.execute(
            "SELECT code, industry FROM basics WHERE industry IS NOT NULL AND industry!=''"
        ).fetchall()
        conn.close()
    except Exception:
        return {}
    return {code: ind for code, ind in rows}


def get_industry_map(path=PANEL_DB):
    """别名：返回 {bs_code: industry_name}。供 strategy/engine 调用。"""
    return load_industry(path)


# --------------------------------------------------------------------------
# 概念板块（新浪源）——跨行业短线题材，板块热度分组的首选
# --------------------------------------------------------------------------
_SINA_REF = "https://finance.sina.com.cn"
_SINA_CLASS = "https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=class"
_SINA_NODE = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "Market_Center.getHQNodeData?page=1&num=1000&sort=symbol&asc=0&node={node}&symbol=&_s_r_a=page")


def _sina_get(url, tries=3, timeout=15):
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": _SINA_REF})
            return urllib.request.urlopen(req, timeout=timeout, context=ctx).read().decode("gbk", "ignore")
        except Exception:
            time.sleep(0.5)
    return None


def fetch_concept_map(only_codes=None):
    """抓取 {bs代码: [概念名,...]} 概念板块映射（新浪源）。

    新浪概念板块（gn_*）覆盖跨行业短线题材（机器人/低空经济/算力/华为等），
    比证监会行业分类更贴合 A 股炒作逻辑。
    only_codes: 限定输出在该股票池内（bs 代码集合）以减小体积；None 则全量。
    任何网络异常都安全降级（失败的板块跳过，整体不抛异常）。
    返回 dict 可能为空（首次/被墙时），调用方退化为行业分类。"""
    import re
    cls = _sina_get(_SINA_CLASS)
    if not cls:
        return {}
    # 解析: "gn_xxx":"gn_xxx,概念名,成分数,..."
    boards = re.findall(r'"(gn_[A-Za-z0-9]+)":"gn_[A-Za-z0-9]+,([^,]+),', cls)
    cmap = {}
    for node, name in boards:
        js = _sina_get(_SINA_NODE.format(node=node))
        if not js:
            continue
        for sym in re.findall(r'"symbol":"(s[hz]\d{6})"', js):
            bs_code = sym[:2] + "." + sym[2:]   # shXXXXXX -> sh.XXXXXX
            if only_codes is not None and bs_code not in only_codes:
                continue
            cmap.setdefault(bs_code, []).append(name)
        time.sleep(0.03)
    return cmap


def refresh_concept_map(fetched_date, only_codes=None, path=PANEL_DB):
    """抓取并落库概念映射到 concept_map 表（每天收盘后一次）。
    成功返回写入条数；失败（被墙）返回 0，保留旧缓存不动。"""
    cmap = fetch_concept_map(only_codes=only_codes)
    if not cmap:
        return 0
    conn = _panel_conn(path)
    _panel_init(conn)
    conn.execute("DELETE FROM concept_map")
    conn.executemany(
        "INSERT OR REPLACE INTO concept_map(code,concepts,fetched_date) VALUES(?,?,?)",
        [(code, json.dumps(cs, ensure_ascii=False), fetched_date) for code, cs in cmap.items()],
    )
    conn.commit()
    conn.close()
    return len(cmap)


def load_concept_map(path=PANEL_DB):
    """读出 {baostock代码: [概念,...]}（板块热度分组用）。无缓存时返回 {}。"""
    try:
        conn = _panel_conn(path)
        rows = conn.execute("SELECT code, concepts FROM concept_map").fetchall()
        conn.close()
    except Exception:
        return {}
    out = {}
    for code, cs in rows:
        try:
            lst = json.loads(cs) if cs else []
        except Exception:
            lst = []
        if lst:
            out[code] = lst
    return out


def concept_map_date(path=PANEL_DB):
    """返回 concept_map 缓存日期（最新一行），无则 None。"""
    try:
        conn = _panel_conn(path)
        row = conn.execute("SELECT fetched_date FROM concept_map LIMIT 1").fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


# ==========================================================================
# 当下热点概念人工 overlay
# ==========================================================================
# 自动概念源（新浪 gn_*）只有 ~173 个偏旧的概念，缺失当下最贴近行情的题材
# （如「六氟化钨出口管制/对日制裁」「半导体材料国产替代」「稀土永磁管制」），
# 导致厦门钨业等被错误归到「出口退税」这类无关概念。
# 这里维护一份当下热点事件→个股的人工映射，刷新概念时叠加（优先级最高，
# 排在自动概念之前），让分组与候选理由贴近当前盘面逻辑。
# 维护方式：跟踪盘面主线题材，按需增删；key=贴近行情的概念名，value=bs 代码列表。
HOT_THEME_OVERLAY = {
    "六氟化钨出口管制": ["sh.600549", "sz.000657", "sh.601958"],   # 厦门钨业/中钨高新/金钼股份
    "半导体材料": ["sh.600549", "sh.688353", "sz.300054", "sh.600378",
                "sz.000657", "sh.688268"],                      # 厦钨/华盛锂电/鼎龙/昊华/中钨/华特气体
    "稀有金属管制": ["sh.600549", "sz.000657", "sh.600111", "sh.600259",
                 "sh.601958"],                                  # 厦钨/中钨/北方稀土/中稀有色/金钼
    "对日制裁反制": ["sh.600549", "sz.000657", "sh.600111"],
}


def hot_theme_overlay(only_codes=None):
    """返回 {bs代码: [热点概念,...]}（人工 overlay，倒排自 HOT_THEME_OVERLAY）。
    only_codes: 限定在股票池内。"""
    out = {}
    for theme, codes in HOT_THEME_OVERLAY.items():
        for code in codes:
            if only_codes is not None and code not in only_codes:
                continue
            out.setdefault(code, []).append(theme)
    return out


def get_group_map(path=PANEL_DB):
    """板块分组映射：优先用概念板块（{code: [概念,...]}），无概念缓存时退化为
    行业分类（{code: [行业名]} 单元素列表）。返回 (group_map, source)。
    source ∈ {'concept','industry'}。供 strategy/engine 统一调用。
    叠加 HOT_THEME_OVERLAY 当下热点概念（排在自动概念之前，优先归组）。"""
    cmap = load_concept_map(path)
    overlay = hot_theme_overlay()
    if cmap:
        for code, themes in overlay.items():
            existing = cmap.get(code, [])
            # 热点概念置前 + 去重保序
            merged = themes + [c for c in existing if c not in themes]
            cmap[code] = merged
        return cmap, "concept"
    ind = load_industry(path)
    gmap = {code: [v] for code, v in ind.items() if v}
    for code, themes in overlay.items():
        gmap[code] = themes + [c for c in gmap.get(code, []) if c not in themes]
    return gmap, "industry"


# ==========================================================================
# 实时行情快照（盘中「随时卖出」用）——腾讯 qt.gtimg 主源（本环境验证可用）
# ==========================================================================
def _tx_code(code):
    """六位/baostock 代码 -> 腾讯代码 sh600000 / sz000001 / bj8xxxxx。"""
    c = code.split(".")[-1] if "." in code else code
    pre = code.split(".")[0] if "." in code else None
    if pre == "sh" or c.startswith(("6", "9")):
        return "sh" + c
    if pre == "bj" or c.startswith(("8", "4", "92")):
        return "bj" + c
    return "sz" + c


def _tx_fetch_batch(tx_codes, retries=3, timeout=15):
    q = ",".join(tx_codes)
    url = "https://qt.gtimg.cn/q=" + q
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
            )
            return urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "ignore")
        except Exception:
            time.sleep(0.4 * (attempt + 1))
    return ""


def tx_spot(codes, batch=60, pause=0.05):
    """腾讯批量实时快照。codes: 六位或 baostock 代码列表。
    返回 {bs代码: {price,preclose,open,high,low,pct,amount,ts}}。空则 {}。"""
    out = {}
    codes = list(codes)
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        raw = _tx_fetch_batch([_tx_code(c) for c in chunk])
        if not raw:
            continue
        for line in raw.strip().split("\n"):
            if "=" not in line or '"' not in line:
                continue
            body = line.split('"', 1)[1].rsplit('"', 1)[0]
            p = body.split("~")
            if len(p) < 35 or not p[2]:
                continue

            def _f(idx):
                try:
                    return float(p[idx])
                except Exception:
                    return None

            bs_code = to_bs_code(p[2])
            out[bs_code] = {
                "code": bs_code, "name": p[1], "price": _f(3), "preclose": _f(4),
                "open": _f(5), "high": _f(33), "low": _f(34), "pct": _f(32),
                "amount": (_f(37) * 1e4) if _f(37) is not None else None,
                "ts": p[30] if len(p) > 30 else None,
            }
        time.sleep(pause)
    return out


def panel_dates(path=PANEL_DB):
    conn = _panel_conn(path)
    rows = conn.execute("SELECT DISTINCT date FROM bars ORDER BY date").fetchall()
    conn.close()
    return [r[0] for r in rows]


def universe_codes(path=PANEL_DB):
    """股票池 baostock 代码列表（实际有日线的，约 1800 只）。"""
    try:
        conn = _panel_conn(path)
        rows = conn.execute("SELECT DISTINCT code FROM bars").fetchall()
        if not rows:
            rows = conn.execute("SELECT code FROM basics").fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def backfill_industry(path=PANEL_DB):
    """给已有 panel.db 的 basics 表补全行业分类（不重建日线）。"""
    conn = _panel_conn(path)
    _panel_init(conn)
    with bs_session():
        ind_map = get_stock_industry()
    codes = [r[0] for r in conn.execute("SELECT code FROM basics").fetchall()]
    n = 0
    for code in codes:
        ind = ind_map.get(code, "")
        if ind:
            conn.execute("UPDATE basics SET industry=? WHERE code=?", (ind, code))
            n += 1
    conn.commit()
    conn.close()
    print(f"[backfill_industry] {n}/{len(codes)} 行业已写入")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "panel":
        s = sys.argv[2] if len(sys.argv) > 2 else "2026-04-01"
        e = sys.argv[3] if len(sys.argv) > 3 else "2026-06-22"
        build_panel(s, e)
    elif len(sys.argv) >= 2 and sys.argv[1] == "industry":
        backfill_industry()
    elif len(sys.argv) >= 2 and sys.argv[1] == "concept":
        d = sys.argv[2] if len(sys.argv) > 2 else pd.Timestamp("now").strftime("%Y-%m-%d")
        uni = set(universe_codes())
        n = refresh_concept_map(d, only_codes=uni or None)
        print(f"[concept] {n} 只股票概念映射已缓存 (date={concept_map_date()})")
    else:
        with bs_session():
            print("trade dates sample:", get_trade_dates("2026-06-01", "2026-06-22"))
