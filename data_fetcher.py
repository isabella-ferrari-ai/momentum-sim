# -*- coding: utf-8 -*-
"""数据获取层——热门板块动量轮动（日线选股+腾讯实时风控：T日收盘选股，T+1集合竞价买入，盘中随时卖出）。

数据源：baostock 日线（免费、稳定，含 isST/tradestatus/turn 字段）。
- 日线面板缓存到本地 SQLite（data/panel.db），可断点续传。
- 股票池：沪深300 + 中证500 + 中证1000 成分股并集（约 1800 只）。
- 板块分组：概念板块（跨行业短线题材，机器人/低空经济/算力/华为等），比证监会行业
  分类更贴合 A 股炒作逻辑；缓存到 concept_map 表，每日收盘后刷新。
  概念源优先东方财富(clean JSON)，被墙降级新浪 gn_*；两者均失败再退化为 baostock
  query_stock_industry() 行业分类（不影响主流程）。

与 trading-sim 完全独立（独立的 panel.db）。日线用于收盘选股/集合竞价买入决策；
另提供腾讯实时快照(tx_spot)供盘中实时风控随时卖出。
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

# 参考情绪的 A 股宽基指数（看板指数条用，腾讯实时源）
# (腾讯代码, 显示名, baostock代码)
INDEX_BASKET = [
    ("sh000001", "上证指数", "sh.000001"),
    ("sz399001", "深证成指", "sz.399001"),
    ("sz399006", "创业板指", "sz.399006"),
    ("sh000300", "沪深300", "sh.000300"),
    ("sh000905", "中证500", "sh.000905"),
    ("sh000852", "中证1000", "sh.000852"),
    ("sh000688", "科创50", "sh.000688"),
]

# 净值曲线对比用：振威指定的六大宽基指数（展示名 -> baostock 代码）
# baostock 无科创50(sh.000688)指数日线(返回空)，回退新浪指数日线(akshare stock_zh_index_daily)。
BENCH_INDICES = [
    ("上证指数", "sh.000001"),
    ("沪深300", "sh.000300"),
    ("中证500", "sh.000905"),
    ("中证1000", "sh.000852"),
    ("科创50", "sh.000688"),
    ("创业板指", "sz.399006"),
]
BENCH_HISTORY_PATH = os.path.join(BASE_DIR, "data", "index_history.json")

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


def _ak_index_daily(symbol, start, end):
    """新浪指数日线回退（baostock 无该指数日线时，如 科创50）。symbol 如 sh000688。
    数据源：akshare stock_zh_index_daily（新浪），真实收盘价。"""
    import akshare as ak
    k = ak.stock_zh_index_daily(symbol=symbol)
    k["date"] = k["date"].astype(str)
    k = k[(k["date"] >= start) & (k["date"] <= end)]
    return [{"date": str(r["date"]), "close": float(r["close"])}
            for _, r in k.iterrows() if pd.notna(r["close"])]


def fetch_bench_history(start, end):
    """拉六大宽基指数日线收盘价（需已在 bs_session 内）。
    返回 (indices, sources)：indices={展示名:[{date,close}]}, sources={展示名:'baostock'|'sina'}。
    数据诚实：全部真实收盘价(不复权)；baostock 为主，返回空则回退新浪(akshare)，
    仍失败则该指数留空不编造。"""
    out, srcs = {}, {}
    for nm, code in BENCH_INDICES:
        rows = []
        try:
            df = get_index(start, end, code=code)
            if df is not None and not df.empty:
                rows = [{"date": str(r["date"]), "close": float(r["close"])}
                        for _, r in df.iterrows() if pd.notna(r["close"])]
                if rows:
                    srcs[nm] = "baostock"
        except Exception as e:
            print("[bench] baostock", nm, "err", e)
        if not rows:  # baostock 空(科创50) -> 新浪指数日线回退
            try:
                rows = _ak_index_daily(code.replace(".", ""), start, end)
                if rows:
                    srcs[nm] = "sina"
            except Exception as e:
                print("[bench] sina", nm, "err", e)
        if rows:
            out[nm] = rows
    return out, srcs


def refresh_bench_history(start, end, td=None, path=None):
    """拉六大指数日线并写 index_history.json（自管 bs_session；供调度器/CLI 调用）。
    返回 (指数数量, 数据截止日)。前端读该文件把指数按净值起始日归一化叠加到净值曲线。"""
    path = path or BENCH_HISTORY_PATH
    with bs_session():
        indices, srcs = fetch_bench_history(start, end)
    last = ""
    for rows in indices.values():
        if rows:
            last = max(last, rows[-1]["date"])
    sina = [nm for nm, s in srcs.items() if s == "sina"]
    source = "baostock 指数日线收盘价(不复权)"
    if sina:
        source += "；" + "/".join(sina) + " 用新浪指数日线"
    payload = {"updated": last or (td or ""), "source": source,
               "sources": srcs, "indices": indices}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return len(indices), last


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
# 概念板块（新浪源）——跨行业短线题材；在 fetch_concept_map_best 中作东财的降级源
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


# --------------------------------------------------------------------------
# 概念板块（东方财富源）——首选：clist JSON 接口，板块覆盖最全、字段干净
# 注：东方财富 push2 接口在本环境被 GFW 墙（RemoteDisconnected），任何失败都
# 安全降级（返回 {}）由 fetch_concept_map_best 转用新浪；换无墙主机/网络恢复时自动接管。
# --------------------------------------------------------------------------
_EM_REF = "https://quote.eastmoney.com/"
_EM_LIST = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=600&po=1&np=1"
            "&fid=f3&fs=m:90+t:3&fields=f12,f14")          # 概念板块列表(m:90 t:3)
_EM_CONS = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1500&po=1&np=1"
            "&fid=f3&fs=b:{bk}+f:!50&fields=f12,f13")       # 板块成分(b:BKxxxx)


def _em_get(url, tries=3, timeout=12):
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": _EM_REF})
            txt = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
            return json.loads(txt)
        except Exception:
            time.sleep(0.5)
    return None


def fetch_concept_map_eastmoney(only_codes=None):
    """抓取 {bs代码: [概念名,...]} 概念板块映射（东方财富源）。
    任何网络异常安全降级（失败的板块跳过，整体不抛异常）；被墙时返回 {}。"""
    js = _em_get(_EM_LIST)
    try:
        boards = (js or {}).get("data", {}).get("diff", []) or []
    except Exception:
        boards = []
    cmap = {}
    for b in boards:
        bk = b.get("f12"); name = b.get("f14")
        if not bk or not name:
            continue
        cjs = _em_get(_EM_CONS.format(bk=bk))
        try:
            members = (cjs or {}).get("data", {}).get("diff", []) or []
        except Exception:
            members = []
        for m in members:
            code = m.get("f12"); mkt = m.get("f13")
            if not code or mkt not in (0, 1):
                continue
            bs_code = ("sh." if mkt == 1 else "sz.") + code   # f13: 1=沪 0=深
            if only_codes is not None and bs_code not in only_codes:
                continue
            cmap.setdefault(bs_code, []).append(name)
        time.sleep(0.03)
    return cmap


def fetch_concept_map_best(only_codes=None):
    """按优先级抓概念映射：东方财富(首选) -> 新浪(降级)。返回 (cmap, source)。
    source ∈ {'eastmoney','sina',''}（'' 表示全部失败/被墙，调用方保留旧缓存）。"""
    cmap = fetch_concept_map_eastmoney(only_codes=only_codes)
    if cmap:
        return cmap, "eastmoney"
    cmap = fetch_concept_map(only_codes=only_codes)   # 新浪降级
    if cmap:
        return cmap, "sina"
    return {}, ""


def refresh_concept_map(fetched_date, only_codes=None, path=PANEL_DB):
    """抓取并落库概念映射到 concept_map 表（每天收盘后一次）。
    优先东方财富，被墙降级新浪。返回 (写入条数, 源名)；全失败返回 (0, '') 并保留旧缓存。"""
    cmap, source = fetch_concept_map_best(only_codes=only_codes)
    if not cmap:
        return 0, ""
    conn = _panel_conn(path)
    _panel_init(conn)
    conn.execute("DELETE FROM concept_map")
    conn.executemany(
        "INSERT OR REPLACE INTO concept_map(code,concepts,fetched_date) VALUES(?,?,?)",
        [(code, json.dumps(cs, ensure_ascii=False), fetched_date) for code, cs in cmap.items()],
    )
    conn.commit()
    conn.close()
    return len(cmap), source


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
# 当下热点概念人工 overlay —— 带时间边界的数据输入（非硬编码当前认知）
# ==========================================================================
# 自动概念源（新浪 gn_*）只有 ~173 个偏旧的概念，缺失当下最贴近行情的题材
# （如「六氟化钨出口管制」「半导体材料国产替代」），导致厦门钨业等被错误归类。
# 人工 overlay 用来补这类主线，但它是「当前认知」，直接硬编码会前视污染回测：
# 把 2026 年的主题套到 2020 年的回测上属于未来函数。
#
# 因此 overlay 改为外部数据文件 data/theme_overlay.json，每条带时间边界与证据：
#   - effective_date  生效日（< 此日不归组）
#   - expiry_date     失效日（> 此日不归组）
#   - evidence_date   证据/事件日（晚于信号日则视为未来信息，跳过）
#   - confidence      置信度（低于阈值跳过）
#   - evidence        证据描述（可读，便于审计）
# 回测默认禁用（BT_USE_THEME_OVERLAY=0）；若启用，必须每日按 td 加载（见 backtest.py）。
# 实盘可用 use_overlay=True，但必须传入 as_of_date=today。
THEME_OVERLAY_PATH = os.path.join(BASE_DIR, "data", "theme_overlay.json")
THEME_CONFIDENCE_MIN = 0.6   # 默认置信度阈值（可被文件 _meta.confidence_threshold 覆盖）
_OVERLAY_REQUIRED = ("theme", "codes", "effective_date", "expiry_date",
                     "evidence_date", "confidence")


def load_theme_overlay(as_of_date, path=None, only_codes=None):
    """按信号日 as_of_date 加载有效的人工主题 overlay，返回 {bs代码: [主题,...]}。

    as_of_date: 信号日（"YYYY-MM-DD"）。所有时间边界都相对它判定，杜绝前视。
    逐条校验，任一不满足即【跳过该条】（不抛异常，安全降级）：
      - 缺必填字段（theme/codes/effective_date/expiry_date/evidence_date/confidence）；
      - 未生效（effective_date > as_of_date）；
      - 已过期（expiry_date < as_of_date）；
      - 证据日期晚于信号日（evidence_date > as_of_date，未来信息）；
      - 置信度低于阈值（confidence < threshold）。
    文件缺失/解析失败返回 {}。"""
    if not as_of_date:
        raise ValueError("load_theme_overlay 需传入 as_of_date（信号日），不可一次性加载当前 overlay")
    path = path or THEME_OVERLAY_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return {}
    meta = doc.get("_meta", {}) if isinstance(doc, dict) else {}
    threshold = meta.get("confidence_threshold", THEME_CONFIDENCE_MIN)
    entries = doc.get("themes", []) if isinstance(doc, dict) else []
    out = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        if any(e.get(k) in (None, "", []) for k in _OVERLAY_REQUIRED):
            continue  # 缺字段
        try:
            conf = float(e["confidence"])
        except Exception:
            continue
        if conf < threshold:
            continue  # 置信度不足
        if e["effective_date"] > as_of_date:
            continue  # 未生效
        if e["expiry_date"] < as_of_date:
            continue  # 已过期
        if e["evidence_date"] > as_of_date:
            continue  # 证据晚于信号日（未来信息）
        theme = e["theme"]
        for code in e["codes"]:
            if only_codes is not None and code not in only_codes:
                continue
            lst = out.setdefault(code, [])
            if theme not in lst:
                lst.append(theme)
    return out


def merge_overlay(base_map, overlay):
    """把人工 overlay 主题并入基础分组（不修改入参，返回新 dict）。
    overlay 主题置前（优先归组），与基础概念去重保序合并。
    base_map: {code: [概念,...]}；overlay: {code: [主题,...]}。"""
    merged = {code: list(themes) for code, themes in base_map.items()}
    for code, themes in overlay.items():
        existing = merged.get(code, [])
        merged[code] = themes + [c for c in existing if c not in themes]
    return merged


def get_group_map(path=PANEL_DB, as_of_date=None, use_overlay=False):
    """板块分组映射：优先用概念板块（{code: [概念,...]}），无概念缓存时退化为
    行业分类（{code: [行业名]} 单元素列表）。返回 (group_map, source)。
    source ∈ {'concept','industry'}（启用 overlay 时追加 '+overlay'）。

    use_overlay: 是否叠加人工主题 overlay（实盘 True / 回测默认 False）。
    as_of_date:  信号日；use_overlay=True 时【必须】传入，按当日时间边界加载 overlay。"""
    cmap = load_concept_map(path)
    if cmap:
        base, src = cmap, "concept"
    else:
        base = {code: [v] for code, v in load_industry(path).items() if v}
        src = "industry"
    if use_overlay:
        if not as_of_date:
            raise ValueError("get_group_map(use_overlay=True) 必须传入 as_of_date（实盘传 today）")
        overlay = load_theme_overlay(as_of_date, only_codes=set(base) or None)
        if overlay:
            base = merge_overlay(base, overlay)
            src += "+overlay"
    return base, src


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


def realtime_spot(codes):
    """统一实时快照：优先专业 A 股数据工具(financial-tool)，失败/缺项回退腾讯源。
    返回 {bs代码: {price,preclose,high,low,pct,...}}。供盘中风控(engine.process_intraday)用。

    振威 2026-06-23 要求策略程序用专业数据工具取数：实时快照改以 financial-tool 为主源，
    腾讯源作为兜底（单源被墙/抖动时仍可用），保证盘中风控不因单点失败而漏卖。"""
    codes = list(codes)
    if not codes:
        return {}
    out = {}
    try:
        import financial_api as fapi
        if fapi.available():
            out = fapi.get_snapshots(codes) or {}
    except Exception:
        out = {}
    # 主源缺失的代码用腾讯补齐（或主源整体失败时全量回退）
    missing = [c for c in codes if c not in out or not out[c].get("price")]
    if missing:
        try:
            tx = tx_spot(missing)
            for c, q in tx.items():
                out[c] = q
        except Exception:
            pass
    return out


def index_spot(basket=None):
    """腾讯实时宽基指数快照（看板「参考情绪」指数条用）。
    返回 [{code,name,price,pct,preclose,open,high,low,ts}, ...]，按 basket 顺序。失败返回 []。"""
    basket = basket or INDEX_BASKET
    raw = _tx_fetch_batch([tx for tx, _, _ in basket])
    if not raw:
        return []
    parsed = {}
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

        parsed[p[2]] = {
            "name": p[1], "price": _f(3), "preclose": _f(4), "open": _f(5),
            "high": _f(33), "low": _f(34), "pct": _f(32),
            "ts": p[30] if len(p) > 30 else None,
        }
    out = []
    for tx, name, bs in basket:
        six = tx[2:]  # 去掉 sh/sz 前缀，对应腾讯返回的 p[2]
        q = parsed.get(six)
        if not q:
            continue
        out.append({"code": bs, "name": name, "price": q["price"], "pct": q["pct"],
                    "preclose": q["preclose"], "open": q["open"],
                    "high": q["high"], "low": q["low"], "ts": q["ts"]})
    return out


def panel_dates(path=PANEL_DB):
    conn = _panel_conn(path)
    rows = conn.execute("SELECT DISTINCT date FROM bars ORDER BY date").fetchall()
    conn.close()
    return [r[0] for r in rows]


def clear_fetch_meta(end, path=PANEL_DB):
    """清除指定 end 日的断点续传标记，强制下次 build_panel 对该 end 全量重抓。
    解决：收盘后当日日线尚未发布时的首次抓取，会把全市场标记为 (code,start,end) 已抓，
    导致 baostock 稍后发布当日日线后，重建仍命中缓存跳过、当日 bar 永远进不来。"""
    conn = _panel_conn(path)
    conn.execute("DELETE FROM fetch_meta WHERE end=?", (end,))
    conn.commit()
    conn.close()


def remote_has_date(td, ref_code=INDEX_CODE):
    """廉价探测 baostock 是否已发布 td 当日日线（单一参考标的，避免无谓全量重抓）。"""
    try:
        with bs_session():
            df = _kdata(ref_code, td, td, adjust="3")
    except Exception:
        return False
    return (df is not None) and (not df.empty) and (td in set(df["date"].astype(str)))


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
        n, source = refresh_concept_map(d, only_codes=uni or None)
        print(f"[concept] {n} 只股票概念映射已缓存 源={source or '无(全失败)'} (date={concept_map_date()})")
    elif len(sys.argv) >= 2 and sys.argv[1] == "bench":
        s = sys.argv[2] if len(sys.argv) > 2 else "2026-06-22"
        e = sys.argv[3] if len(sys.argv) > 3 else pd.Timestamp("now").strftime("%Y-%m-%d")
        n, last = refresh_bench_history(s, e)
        print(f"[bench] {n} 只指数日线已写入 {BENCH_HISTORY_PATH} (截止 {last})")
    else:
        with bs_session():
            print("trade dates sample:", get_trade_dates("2026-06-01", "2026-06-22"))
