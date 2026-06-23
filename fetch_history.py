# -*- coding: utf-8 -*-
"""拉多年日线到独立的 backtest_panel.db（研究闭环回测用）。

复用当前 panel.db 的 universe（即当前指数成分），不重新抓 index_universe（慢且需akshare）。
注意：universe 是当前成分 -> 多年回测有幸存者偏差，结论需按此口径解读。
单只一次 query 拉整段历史，约 1794 次调用；断点续传（fetch_meta）。
"""
import warnings
warnings.filterwarnings("ignore")

import os
import sys
import sqlite3
import time

import pandas as pd
import data_fetcher as dfetch

BT_PANEL = os.path.join(dfetch.BASE_DIR, "data", "backtest_panel.db")


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "2019-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-22"

    # 1) universe + basics 从现有 panel.db 复制（仅取实际有日线的股票，约1794只，
    #    避免 basics 里的历史残留把抓取量放大到近5000）
    src = sqlite3.connect(dfetch.PANEL_DB)
    bar_codes = {r[0] for r in src.execute("SELECT DISTINCT code FROM bars").fetchall()}
    basics = [r for r in src.execute("SELECT code,name,ipoDate,industry FROM basics").fetchall()
              if r[0] in bar_codes]
    src.close()
    codes = [b[0] for b in basics]
    print(f"[hist] universe={len(codes)} range={start}~{end} -> {BT_PANEL}", flush=True)

    conn = dfetch._panel_conn(BT_PANEL)
    dfetch._panel_init(conn)
    conn.executemany("INSERT OR REPLACE INTO basics(code,name,ipoDate,industry) VALUES(?,?,?,?)", basics)
    conn.commit()

    done = {r[0] for r in conn.execute(
        "SELECT code FROM fetch_meta WHERE start=? AND end=?", (start, end)).fetchall()}
    todo = [c for c in codes if c not in done]
    print(f"[hist] done={len(done)} todo={len(todo)}", flush=True)

    n = 0
    with dfetch.bs_session():
        for code in todo:
            try:
                df = dfetch._kdata(code, start, end, adjust="3")
            except Exception as e:
                print(f"[hist] {code} ERR {repr(e)[:60]}", flush=True)
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
                conn.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", recs)
            conn.execute("INSERT OR REPLACE INTO fetch_meta(code,start,end,fetched_at) VALUES(?,?,?,?)",
                         (code, start, end, pd.Timestamp("now").isoformat()))
            n += 1
            if n % 100 == 0:
                conn.commit()
                print(f"[hist] {n}/{len(todo)} fetched ...", flush=True)
        conn.commit()
    conn.close()
    print(f"[hist] DONE fetched {n} stocks -> {BT_PANEL}", flush=True)


if __name__ == "__main__":
    main()
