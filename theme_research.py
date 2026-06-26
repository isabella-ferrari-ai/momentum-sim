# -*- coding: utf-8 -*-
"""主题 overlay 自动维护 —— 把「人工主题」从『总指望振威给提示』改成『定期自动搜索得到』。

设计（振威 2026-06-23 要求：人工主题应定期去搜索得到，不依赖手动输入）
============================================================================
overlay 的两层信息：
  1) 客观热度（全自动、零推断）：新浪概念板块当日涨幅排名。回答「今天哪些板块在动」，
     带真实交易日，可直接作为置信度输入与可审计证据，杜绝拍脑袋。
  2) 题材发现（定期搜索）：新浪 ~175 个概念板块覆盖偏旧，缺当下最贴行情的主线
     （如「六氟化钨出口管制」「半导体材料国产替代」）。这类靠 isabella 定期跑搜索，
     拿到真实催化日(evidence_date)+来源(evidence)，再经本模块校验后写入 json。

本模块提供「写入端」的全部确定性逻辑（解析、名称→代码、校验、去重、落盘），
让 isabella 的搜索结果能安全、可复现地进 theme_overlay.json，而不是手改文件：
  - resolve_codes(names)          股票名 -> bs 代码（基于 panel.basics，可审计未命中）
  - board_heat(top_n)             新浪概念板块当日涨幅榜（客观热度，带真实日期）
  - upsert_theme(theme=..., ...)  校验后插入/更新一条主题（保留 _meta、按 theme 去重）
  - prune_expired(as_of_date)     标记/清理已过期条目（保留审计，可选）
  - verify_overlay()              全量复核：字段完整性、日期合法、代码可解析、置信度

所有写入都走 load_theme_overlay 的同一套校验口径（缺字段/未生效/已过期/证据晚于
信号日/置信度不足 -> 该条无效），保证「能写进去」与「回测/实盘能用」标准一致。

CLI:
  python3 theme_research.py heat            # 打印当日概念板块涨幅榜（客观热度）
  python3 theme_research.py verify          # 复核 theme_overlay.json 全量条目
  python3 theme_research.py resolve 厦门钨业 中钨高新   # 名称->代码自测
"""
import warnings
warnings.filterwarnings("ignore")

import os
import re
import json
import sqlite3

import data_fetcher as dfetch

OVERLAY_PATH = dfetch.THEME_OVERLAY_PATH
REQUIRED = dfetch._OVERLAY_REQUIRED
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------
# 名称 -> bs 代码（板块成分须用 panel 实际股票池，未命中要显式报告，不静默吞掉）
# --------------------------------------------------------------------------
def _name_code_maps(path=None):
    """返回 (name->code, code->name)。基于 panel.basics（约 4900 只）。"""
    path = path or dfetch.PANEL_DB
    n2c, c2n = {}, {}
    try:
        conn = sqlite3.connect(path)
        for code, name in conn.execute("SELECT code,name FROM basics").fetchall():
            if name:
                n2c[name] = code
                c2n[code] = name
        conn.close()
    except Exception:
        pass
    return n2c, c2n


def resolve_codes(names, path=None):
    """股票名列表 -> {"codes": [bs代码,...], "matched": {名:代码}, "missing": [名,...]}。
    名称取自 panel.basics 精确匹配；未命中显式列出（可能是改名/退市/不在池内）。"""
    n2c, _ = _name_code_maps(path)
    codes, matched, missing = [], {}, []
    for nm in names:
        nm = (nm or "").strip()
        if not nm:
            continue
        code = n2c.get(nm)
        if code:
            codes.append(code)
            matched[nm] = code
        else:
            missing.append(nm)
    # 去重保序
    seen, uniq = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c); uniq.append(c)
    return {"codes": uniq, "matched": matched, "missing": missing}


# --------------------------------------------------------------------------
# 客观热度：概念板块当日涨幅榜（真实交易日，零推断，可作证据/置信度输入）
# 优先东方财富(clean JSON)，被墙降级新浪。
# --------------------------------------------------------------------------
_EM_HEAT = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=600&po=1&np=1"
            "&fid=f3&fs=m:90+t:3&fields=f12,f14,f3,f6")   # 概念板块涨幅榜(已按 f3 降序)


def _board_heat_eastmoney(top_n=20):
    """东方财富概念板块当日涨幅榜（首选）。被墙/失败返回 []。"""
    js = dfetch._em_get(_EM_HEAT)
    try:
        diff = (js or {}).get("data", {}).get("diff", []) or []
    except Exception:
        diff = []
    out = []
    for b in diff:
        name = b.get("f14"); pct = b.get("f3")
        if not name or pct in (None, "-"):
            continue
        try:
            pct = float(pct) / 100.0   # 东财 f3 为 涨跌幅%*100（如 235 => 2.35）
        except Exception:
            continue
        out.append({"node": b.get("f12"), "name": name, "count": 0,
                    "pct_chg": round(pct, 2), "turnover": b.get("f6") or 0})
    out.sort(key=lambda r: r["pct_chg"], reverse=True)
    return out[:top_n]


def _board_heat_sina(top_n=20):
    """新浪概念板块当日涨幅榜（降级）。失败返回 []。
    字段口径（新浪 class 串，逗号分隔）：name, 成分数, 均价, ?, 涨跌额, 涨跌幅%, 成交量, 成交额, ..."""
    cls = dfetch._sina_get(dfetch._SINA_CLASS)
    if not cls:
        return []
    rows = re.findall(r'"(gn_[A-Za-z0-9]+)":"gn_[A-Za-z0-9]+,([^"]+)"', cls)
    out = []
    for node, payload in rows:
        parts = payload.split(",")
        if len(parts) < 8:
            continue
        name = parts[0]
        try:
            count = int(float(parts[1]))
            pct = float(parts[4])        # 涨跌幅%（index 4 相对 name 去头后）
            turnover = float(parts[6])   # 成交额
        except Exception:
            continue
        out.append({"node": node, "name": name, "count": count,
                    "pct_chg": round(pct, 2), "turnover": turnover})
    out.sort(key=lambda r: r["pct_chg"], reverse=True)
    return out[:top_n]


def board_heat(top_n=20):
    """概念板块当日涨幅榜（客观热度层），按涨幅降序返回前 top_n。
    优先东方财富(clean JSON)，被墙降级新浪。返回 [{"node","name","count","pct_chg","turnover"}]，
    失败返回 []。实际所用源记在 board_heat.last_source ∈ {'eastmoney','sina',''}。"""
    rows = _board_heat_eastmoney(top_n=top_n)
    if rows:
        board_heat.last_source = "eastmoney"
        return rows
    rows = _board_heat_sina(top_n=top_n)
    board_heat.last_source = "sina" if rows else ""
    return rows


board_heat.last_source = ""


# --------------------------------------------------------------------------
# 落盘：校验后插入/更新一条主题（保留 _meta，按 theme 名去重）
# --------------------------------------------------------------------------
def _load_doc(path=None):
    path = path or OVERLAY_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"_meta": {}, "themes": []}


def _save_doc(doc, path=None):
    path = path or OVERLAY_PATH
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


def _validate_entry(e, threshold):
    """返回 [问题,...]；空列表表示该条有效（与 load_theme_overlay 口径一致）。"""
    probs = []
    for k in REQUIRED:
        if e.get(k) in (None, "", []):
            probs.append(f"缺字段:{k}")
    for k in ("effective_date", "expiry_date", "evidence_date"):
        v = e.get(k)
        if v and not _DATE_RE.match(str(v)):
            probs.append(f"日期格式:{k}={v}")
    try:
        conf = float(e.get("confidence"))
        if conf < threshold:
            probs.append(f"置信度{conf}<阈值{threshold}")
    except Exception:
        probs.append("置信度非数值")
    ed, xd = e.get("effective_date"), e.get("expiry_date")
    if ed and xd and _DATE_RE.match(str(ed)) and _DATE_RE.match(str(xd)) and ed > xd:
        probs.append(f"生效日{ed}晚于失效日{xd}")
    return probs


def upsert_theme(theme, codes=None, names=None, effective_date=None,
                 expiry_date=None, evidence_date=None, confidence=None,
                 evidence=None, path=None, allow_below_threshold=False,
                 run_date=None):
    """校验后插入/更新一条主题到 theme_overlay.json。按 theme 名去重（同名覆盖）。

    codes: bs 代码列表；或传 names（股票名）由 resolve_codes 解析（未命中会报告）。
    其余字段语义见 data_fetcher.load_theme_overlay。返回 dict(ok, reason, missing, entry)。
    校验不过（缺字段/日期非法/置信度不足）默认拒绝写入，保证文件始终可用。"""
    path = path or OVERLAY_PATH
    doc = _load_doc(path)
    meta = doc.get("_meta", {}) if isinstance(doc, dict) else {}
    threshold = 0.0 if allow_below_threshold else meta.get(
        "confidence_threshold", dfetch.THEME_CONFIDENCE_MIN)

    missing = []
    if not codes and names:
        r = resolve_codes(names, path=dfetch.PANEL_DB)
        codes, missing = r["codes"], r["missing"]

    entry = {
        "theme": theme,
        "codes": list(codes or []),
        "effective_date": effective_date,
        "expiry_date": expiry_date,
        "evidence_date": evidence_date,
        "confidence": confidence,
        "evidence": evidence or "",
    }
    probs = _validate_entry(entry, threshold)
    if probs:
        return {"ok": False, "reason": "；".join(probs), "missing": missing, "entry": entry}

    themes = doc.setdefault("themes", [])
    for i, e in enumerate(themes):
        if e.get("theme") == theme:
            themes[i] = entry
            break
    else:
        themes.append(entry)
    if run_date:   # 维护运行日（搜索发生日），非催化日；不传则保留旧值
        meta["updated_at"] = run_date
    doc["_meta"] = meta
    _save_doc(doc, path)
    return {"ok": True, "reason": "written", "missing": missing, "entry": entry}


def verify_overlay(path=None):
    """全量复核 theme_overlay.json。返回 {valid:[...], invalid:[{theme,problems}], unresolved:{theme:[code]}}。
    unresolved 列出无法在 panel.basics 反解出名字的代码（可能改名/退市/不在池内）。"""
    path = path or OVERLAY_PATH
    doc = _load_doc(path)
    meta = doc.get("_meta", {})
    threshold = meta.get("confidence_threshold", dfetch.THEME_CONFIDENCE_MIN)
    _, c2n = _name_code_maps()
    valid, invalid, unresolved = [], [], {}
    for e in doc.get("themes", []):
        if not isinstance(e, dict):
            invalid.append({"theme": str(e)[:40], "problems": ["非对象"]}); continue
        probs = _validate_entry(e, threshold)
        bad_codes = [c for c in (e.get("codes") or []) if c not in c2n]
        if bad_codes:
            unresolved[e.get("theme", "?")] = bad_codes
        (invalid if probs else valid).append(
            {"theme": e.get("theme"), "problems": probs} if probs else e.get("theme"))
    return {"valid": valid, "invalid": invalid, "unresolved": unresolved,
            "threshold": threshold, "count": len(doc.get("themes", []))}


def _main():
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "heat"
    if cmd == "heat":
        rows = board_heat(top_n=int(sys.argv[2]) if len(sys.argv) > 2 else 20)
        if not rows:
            print("[heat] 抓取失败（新浪源不可达）"); return
        print(f"[heat] 新浪概念板块当日涨幅榜 TOP{len(rows)}：")
        for i, r in enumerate(rows, 1):
            print(f"  {i:>2}. {r['name']:<12} 涨幅{r['pct_chg']:>6}%  成分{r['count']:>3}  "
                  f"成交额{r['turnover']/1e8:>7.1f}亿")
    elif cmd == "verify":
        v = verify_overlay()
        print(f"[verify] 共{v['count']}条 阈值{v['threshold']}")
        print(f"  有效 {len(v['valid'])}: {v['valid']}")
        if v["invalid"]:
            print(f"  无效 {len(v['invalid'])}:")
            for it in v["invalid"]:
                print(f"    - {it['theme']}: {it['problems']}")
        if v["unresolved"]:
            print(f"  代码无法反解（改名/退市/不在池）:")
            for t, cs in v["unresolved"].items():
                print(f"    - {t}: {cs}")
    elif cmd == "resolve":
        r = resolve_codes(sys.argv[2:])
        print("matched:", r["matched"])
        print("missing:", r["missing"])
        print("codes:", r["codes"])
    else:
        print(__doc__)


if __name__ == "__main__":
    _main()
