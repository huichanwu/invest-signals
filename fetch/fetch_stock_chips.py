# -*- coding: utf-8 -*-
"""
fetch_stock_chips.py — D9 個股籌碼日資料抓取（證交所版，免註冊、免金鑰）
資料流：💹 個股現價（追蹤清單）＋ 證交所開放端點 → 個股籌碼日資料

- 追蹤清單唯一來源：Notion「個股現價」（國家＝台股）；僅涵蓋「上市」標的
- 證交所端點一天一包全市場：依日期迴圈抓回，再篩出清單裡的標的
- 五個端點：T86 三大法人／MI_QFIIS 外資持股比率＋發行股數／MI_MARGN 融資融券（資＋券）／TWT93U 借券賣出／MI_INDEX 收盤行情
- 投信／自營持股比率(推估)＝累計買賣超÷發行張數＋錨點位移；錨點填在「個股現價」，每次執行自動整條重算
- Name＝「代號 日期」去重，重跑不會重複寫入
- 每列自動填「個股現價」relation（用讀清單時拿到的 page_id）

2026/08/18 新增：新標的自動偵測回補
- 每次執行先掃追蹤清單的資料覆蓋率（只認「收盤價非空」的有效列，
  避開 fetch_tdcc.py／signal_tw_l1.py 先建的空殼列）
- 全新／斷更／歷史不足的標的 → 自動把窗口擴大到 60 天並強制補寫，
  其餘檔仍只處理近 --days 天，不影響日常速度
- 狀態記在 state\backfill_state.json，防止上櫃標的與近期上市標的無限重試
- twse_get 加強重試：timeout 60 秒、最多 5 次、間隔 5→10→20→40 秒遞增
  （回補請求量大時證交所偶發不回應，實測會 ReadTimeout 中斷）

2026/08/18 D16 雙寫：主流程逐列收集 sql_rows，結束前一次
upsert_rows("stock_chips", ...) 同步寫入本機 SQL Server invest
（複合主鍵＋MERGE，重跑只更新不重複）

用法：
    python fetch_stock_chips.py                    # 日常：近 5 天＋自動偵測回補
    python fetch_stock_chips.py --refill           # 排程用：近 5 天既有列一併補寫
    python fetch_stock_chips.py --days 60 --refill # 手動全面回補
    python fetch_stock_chips.py --no-auto-backfill # 關閉自動偵測（純日常模式）
"""

import argparse
import json
import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from notion_client import Client

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db_writer import upsert_rows

load_dotenv()
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
PRICE_DB_ID = os.getenv("NOTION_DATABASE_ID")         # 個股現價（清單來源，與 sync.py 共用）
CHIPS_DB_ID = os.getenv("NOTION_DB_TW_STOCK_CHIPS")   # 個股籌碼日資料

TWSE_BASE = "https://www.twse.com.tw/rwd/zh"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal chips fetch script)"}
TWSE_SLEEP = 2.0          # 秒；證交所打太快會被封 IP，回補 300+ 請求時務必放慢
STREAK_WARMUP_DAYS = 30   # 往前多抓 30 天暖身，讓「外資連買賣天數」在區間起點就算得對

# ---- 自動回補設定（2026/08/18 新增）----
PROJ_DIR = Path(__file__).resolve().parent.parent      # C:\python\invest-signals
STATE_FILE = PROJ_DIR / "state" / "backfill_state.json"
NEW_TICKER_DAYS = 60      # 新檔／斷更檔的回補天數
COVERAGE_SLACK = 7        # 「歷史夠早」的容忍天數（避免回補完又被判定不足）
GAP_TOLERANCE = 4         # 最新有效列落後幾天視為斷更（4 天含週末）
MAX_AUTO_BACKFILL = 5     # 一次最多自動回補幾檔，超過改要求手動
NO_GAIN_LIMIT = 2         # 連續幾次回補無新增列就標記 coverage_ok

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("fetch_stock_chips.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ---------- 證交所 ----------

def twse_get(path, params, tries=5):
    """呼叫證交所 rwd JSON 端點；假日／尚未公布回傳 None。

    2026/08/18 修訂：回補模式一次打 300+ 請求，證交所會偶發不回應
    （ReadTimeout, read timeout=30）。改為 timeout 60 秒＋最多重試 5 次、
    間隔 5→10→20→40 秒遞增，等它願意重新理人再繼續。"""
    q = dict(params, response="json")
    body = None
    for attempt in range(tries):
        try:
            resp = requests.get(TWSE_BASE + path, params=q, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            body = resp.json()
            break
        except Exception as e:
            if attempt == tries - 1:
                raise
            wait = 5 * (2 ** attempt)
            log.warning("TWSE %s 失敗（第 %d 次），%d 秒後重試：%s", path, attempt + 1, wait, e)
            time.sleep(wait)
    time.sleep(TWSE_SLEEP)
    if body.get("stat") != "OK":
        return None
    return body


def find_rows(body, key):
    """找出第一欄欄名含 key 的表格，回傳其資料列。"""
    out = []
    for t in body.get("tables") or [body]:
        fields = t.get("fields") or []
        if fields and key in str(fields[0]):
            out.extend(t.get("data") or [])
    return out


def to_num(s):
    """'1,234' → 1234；'--'／空值 → None"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "---", "X", "N/A"):
        return None
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


def to_lots(v):
    """股 → 張（四捨五入）"""
    return None if v is None else int(round(v / 1000.0))


def fetch_t86(d):
    """三大法人買賣超 → {代號: (外資, 投信, 自營)}，單位：張。非交易日回傳 None。
    註：外資自營商依證交所慣例已計入自營商合計（量極小）。"""
    body = twse_get("/fund/T86", {"date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999"})
    if body is None:
        return None
    out = {}
    for row in find_rows(body, "證券代號"):
        if len(row) < 12:
            continue
        # 欄位序：[4] 外陸資買賣超(不含外資自營商) [10] 投信買賣超 [11] 自營商買賣超(合計)
        code = str(row[0]).strip()
        out[code] = (to_lots(to_num(row[4])), to_lots(to_num(row[10])), to_lots(to_num(row[11])))
    return out


def fetch_qfiis(d):
    """外資及陸資持股統計 → {代號: (全體外資及陸資持股比率%, 發行張數)}。"""
    body = twse_get("/fund/MI_QFIIS", {"date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999"})
    if body is None:
        return {}
    out = {}
    for t in body.get("tables") or [body]:
        fields = [str(f) for f in (t.get("fields") or [])]
        if not fields or "證券代號" not in fields[0]:
            continue
        idx = next((i for i, f in enumerate(fields) if "全體外資" in f and "比率" in f), None)
        sh_idx = next((i for i, f in enumerate(fields) if "發行股數" in f), None)
        if idx is None:
            continue
        for row in t.get("data") or []:
            if len(row) > idx:
                lots = to_lots(to_num(row[sh_idx])) if sh_idx is not None and len(row) > sh_idx else None
                out[str(row[0]).strip()] = (to_num(row[idx]), lots)
    return out


def fetch_margin(d):
    """融資融券餘額 → {代號: (融資餘額, 融資增減, 融券餘額, 融券增減)}，單位：張（原始即張）。"""
    body = twse_get("/marginTrading/MI_MARGN", {"date": d.strftime("%Y%m%d"), "selectType": "ALL"})
    if body is None:
        return {}
    out = {}
    for row in find_rows(body, "代號"):   # 此表表頭是「代號」，不是「股票代號」
        if len(row) < 13:
            continue
        # 欄位序：融資群組 [5] 前日餘額 [6] 今日餘額；融券群組 [11] 前日餘額 [12] 今日餘額
        fin_today, fin_prev = to_num(row[6]), to_num(row[5])
        fin_chg = fin_today - fin_prev if fin_today is not None and fin_prev is not None else None
        sh_today, sh_prev = to_num(row[12]), to_num(row[11])
        sh_chg = sh_today - sh_prev if sh_today is not None and sh_prev is not None else None
        out[str(row[0]).strip()] = (fin_today, fin_chg, sh_today, sh_chg)
    return out


def fetch_sbl(d):
    """借券賣出餘額 → {代號: 餘額(張)}；原始單位為股。"""
    body = twse_get("/marginTrading/TWT93U", {"date": d.strftime("%Y%m%d")})
    if body is None:
        return {}
    out = {}
    for row in find_rows(body, "代號"):   # 此表表頭是「代號」，不是「股票代號」
        if len(row) < 13:
            continue
        # 欄位序（借券賣出群組）：[12] 當日餘額（股）
        out[str(row[0]).strip()] = to_lots(to_num(row[12]))
    return out


def fetch_close(d):
    """每日收盤行情 → {代號: 收盤價(元)}；當日無成交為 None。"""
    body = twse_get("/afterTrading/MI_INDEX", {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999"})
    if body is None:
        return {}
    out = {}
    for row in find_rows(body, "證券代號"):
        if len(row) < 9:
            continue
        # 欄位序：[8] 收盤價
        out[str(row[0]).strip()] = to_num(row[8])
    return out


def weekdays(first, last):
    d = first
    while d <= last:
        if d.weekday() < 5:   # 週六日直接跳過；國定假日由 stat 判斷
            yield d
        d += timedelta(days=1)


# ---------- Notion ----------

def get_data_source_id(notion, any_id):
    """接受「資料庫 ID」或「data source ID」，一律回傳 data source ID。
    （sync.py 的 NOTION_DATABASE_ID 若存的是 data source ID 也能用）"""
    try:
        db = notion.databases.retrieve(database_id=any_id)
        return db["data_sources"][0]["id"]
    except Exception:
        ds = notion.data_sources.retrieve(data_source_id=any_id)
        return ds["id"]


def query_all(notion, data_source_id, flt=None):
    results, cursor = [], None
    while True:
        kwargs = {"data_source_id": data_source_id, "page_size": 100}
        if flt:
            kwargs["filter"] = flt
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.data_sources.query(**kwargs)
        results.extend(resp["results"])
        if not resp.get("has_more"):
            return results
        cursor = resp["next_cursor"]


def fetch_watchlist(notion, price_ds):
    """讀「個股現價」國家＝台股 → {代號: {page_id, name}}"""
    watch = {}
    flt = {"property": "國家", "select": {"equals": "台股"}}
    for p in query_all(notion, price_ds, flt):
        props = p["properties"]
        code = "".join(t["plain_text"] for t in props["股票代號"]["title"]).strip()
        name = "".join(t["plain_text"] for t in props["名稱"]["rich_text"]).strip()
        anchor = ((props.get("持股錨點日期") or {}).get("date") or {}).get("start")
        if code:
            watch[code] = {
                "page_id": p["id"], "name": name,
                "anchor_date": anchor[:10] if anchor else None,
                "trust_anchor": (props.get("投信持股錨點") or {}).get("number"),
                "dealer_anchor": (props.get("自營持股錨點") or {}).get("number"),
            }
    return watch


def existing_dates(notion, chips_ds, ticker, start):
    """已寫入的資料日期 → {日期: page_id}（去重與 --refill 回補用）"""
    flt = {"and": [
        {"property": "代號", "rich_text": {"equals": ticker}},
        {"property": "資料日期", "date": {"on_or_after": start}},
    ]}
    pages = {}
    for p in query_all(notion, chips_ds, flt):
        d = p["properties"]["資料日期"]["date"]
        if d and d.get("start"):
            pages[d["start"][:10]] = p["id"]
    return pages


# ---------- 自動回補偵測（2026/08/18 新增）----------

def load_state():
    """讀回補狀態檔；不存在或損壞就從空的開始。"""
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
                          encoding="utf-8")


def ticker_coverage(notion, chips_ds, ticker):
    """回傳 (有效列數, 最早日, 最新日)。

    有效＝「收盤價非空」的列。這是關鍵：fetch_tdcc.py 與 signal_tw_l1.py
    會先建只有日期／大戶比率的空殼列，若用「列是否存在」判斷，新標的會被
    誤判為舊檔而只補 5 天（見 D9 2026/08/15 修訂的 3045 案例）。"""
    flt = {"and": [
        {"property": "代號", "rich_text": {"equals": str(ticker)}},
        {"property": "收盤價", "number": {"is_not_empty": True}},
    ]}
    dates = []
    for p in query_all(notion, chips_ds, flt):
        d = ((p["properties"].get("資料日期") or {}).get("date") or {}).get("start")
        if d:
            dates.append(d[:10])
    if not dates:
        return 0, None, None
    dates.sort()
    return len(dates), dates[0], dates[-1]


def detect_backfill_targets(notion, chips_ds, tickers, today, state, names=None):
    """找出需要 60 天回補的標的：全新／斷更回歸／歷史不足。

    tickers 可以是 list 或 dict（只用到 key）。names 是 {代號: 名稱}，僅供 log。
    fetch_tdcc.py 也會 import 這支共用同一套判斷。"""
    names = names or {}
    stale_before = (today - timedelta(days=GAP_TOLERANCE)).isoformat()
    need_start = (today - timedelta(days=NEW_TICKER_DAYS - COVERAGE_SLACK)).isoformat()
    targets, report = [], []

    for ticker in sorted(tickers):
        st = state.get(ticker) or {}
        if st.get("no_twse_data"):
            continue                      # 已確認證交所無資料（上櫃等），不再重試

        n, first, last = ticker_coverage(notion, chips_ds, ticker)
        if n == 0:
            reason = "全新標的（無任何有效列）"
        elif last < stale_before:
            reason = "斷更：最新有效列 %s" % last
        elif not st.get("coverage_ok") and first > need_start:
            reason = "歷史不足：最早有效列 %s（僅 %d 筆）" % (first, n)
        else:
            continue

        targets.append(ticker)
        report.append("%s %s → %s" % (ticker, names.get(ticker, ""), reason))

    if not targets:
        return []

    if len(targets) > MAX_AUTO_BACKFILL:
        log.warning("偵測到 %d 檔需回補（超過上限 %d），本次不自動擴大窗口。"
                    "請手動執行：python fetch\\fetch_stock_chips.py --days 60 --refill\n  %s",
                    len(targets), MAX_AUTO_BACKFILL, "\n  ".join(report))
        return []

    log.info("🔎 自動回補偵測命中 %d 檔：\n  %s", len(targets), "\n  ".join(report))
    return targets


NUMBER_FIELDS = ["外資買賣超", "投信買賣超", "自營買賣超", "外資連買賣天數",
                 "融資餘額", "融資增減", "融券餘額", "融券增減",
                 "借券賣出餘額", "收盤價", "外資持股比率", "發行張數"]


def create_row(notion, chips_ds, ticker, info, d, vals):
    props = {
        "Name": {"title": [{"text": {"content": "%s %s" % (ticker, d)}}]},
        "代號": {"rich_text": [{"text": {"content": str(ticker)}}]},
        "資料日期": {"date": {"start": d}},
        "個股現價": {"relation": [{"id": info["page_id"]}]},
    }
    if info["name"]:
        props["名稱"] = {"rich_text": [{"text": {"content": info["name"]}}]}
    for field in NUMBER_FIELDS:
        if vals.get(field) is not None:
            props[field] = {"number": vals[field]}
    notion.pages.create(
        parent={"type": "data_source_id", "data_source_id": chips_ds},
        properties=props,
    )
    time.sleep(0.34)   # Notion API 約 3 req/s


REFILL_FIELDS = ["融券餘額", "融券增減", "外資持股比率", "發行張數"]


def update_row(notion, page_id, vals, fields):
    """回補既有列的新欄位（--refill 用）"""
    props = {}
    for field in fields:
        if vals.get(field) is not None:
            props[field] = {"number": vals[field]}
    if props:
        notion.pages.update(page_id=page_id, properties=props)
        time.sleep(0.34)


def recompute_ratios(notion, chips_ds, ticker, info):
    """重算該檔全部歷史的投信／自營持股比率(推估)，只回寫有變動的列。
    比率(t)＝累計買賣超÷發行張數（百分點）＋錨點位移；未設錨點＝從資料起點 0 起算。
    錨點填在「個股現價」（持股錨點日期＋投信/自營持股錨點）；改錨點後
    下一次執行整條自動重算對齊，不需要 --refill。"""
    flt = {"property": "代號", "rich_text": {"equals": str(ticker)}}
    rows = []
    for p in query_all(notion, chips_ds, flt):
        pr = p["properties"]
        d = (pr["資料日期"].get("date") or {}).get("start")
        if not d:
            continue
        rows.append({
            "id": p["id"],
            "date": d[:10],
            "trust": (pr.get("投信買賣超") or {}).get("number"),
            "dealer": (pr.get("自營買賣超") or {}).get("number"),
            "lots": (pr.get("發行張數") or {}).get("number"),
            "old_t": (pr.get("投信持股比率(推估)") or {}).get("number"),
            "old_d": (pr.get("自營持股比率(推估)") or {}).get("number"),
        })
    if not rows:
        return 0
    rows.sort(key=lambda r: r["date"])
    filled = None                     # 發行張數補洞：向後帶最近已知值
    for r in rows:
        filled = r["lots"] or filled
        r["lots"] = filled
    first = next((r["lots"] for r in rows if r["lots"]), None)
    if first is None:
        log.info("%s：無發行張數（上櫃或尚未回補），略過持股比率推估", ticker)
        return 0
    for r in rows:                    # 序列開頭缺值用最早已知值補
        r["lots"] = r["lots"] or first
    cum_t = cum_d = 0.0
    for r in rows:
        cum_t += (r["trust"] or 0) * 100.0 / r["lots"]
        cum_d += (r["dealer"] or 0) * 100.0 / r["lots"]
        r["cum_t"], r["cum_d"] = cum_t, cum_d
    off_t = off_d = 0.0               # 位移量：讓曲線通過（錨點日, 錨點值）
    if info.get("anchor_date"):
        base_t = base_d = 0.0
        for r in rows:
            if r["date"] <= info["anchor_date"]:
                base_t, base_d = r["cum_t"], r["cum_d"]
        if info.get("trust_anchor") is not None:
            off_t = info["trust_anchor"] - base_t
        if info.get("dealer_anchor") is not None:
            off_d = info["dealer_anchor"] - base_d
    changed = 0
    for r in rows:
        new_t = round(r["cum_t"] + off_t, 3)
        new_d = round(r["cum_d"] + off_d, 3)
        props = {}
        if r["old_t"] is None or abs(r["old_t"] - new_t) > 0.0005:
            props["投信持股比率(推估)"] = {"number": new_t}
        if r["old_d"] is None or abs(r["old_d"] - new_d) > 0.0005:
            props["自營持股比率(推估)"] = {"number": new_d}
        if props:
            notion.pages.update(page_id=r["id"], properties=props)
            time.sleep(0.34)
            changed += 1
    return changed


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5,
                    help="抓最近幾天（日常 5；首跑/全面回補 60）")
    ap.add_argument("--refill", action="store_true",
                    help="連同已存在的列一起補寫欄位（新標的自動強制開啟）")
    ap.add_argument("--no-auto-backfill", action="store_true",
                    help="關閉新標的自動偵測回補（純日常模式）")
    args = ap.parse_args()

    if not all([NOTION_TOKEN, PRICE_DB_ID, CHIPS_DB_ID]):
        raise SystemExit("請檢查 .env：NOTION_TOKEN / NOTION_DATABASE_ID / NOTION_DB_TW_STOCK_CHIPS")

    today = date.today()

    notion = Client(auth=NOTION_TOKEN)
    price_ds = get_data_source_id(notion, PRICE_DB_ID)
    chips_ds = get_data_source_id(notion, CHIPS_DB_ID)

    watch = fetch_watchlist(notion, price_ds)
    log.info("追蹤清單（台股）共 %d 檔：%s", len(watch), ", ".join(sorted(watch)))

    # 0) 自動偵測需要 60 天回補的標的（全新／斷更／歷史不足）
    state = load_state()
    auto_targets = []
    if not args.no_auto_backfill:
        names = {k: v["name"] for k, v in watch.items()}
        auto_targets = detect_backfill_targets(notion, chips_ds, watch, today, state, names)

    routine_start = today - timedelta(days=args.days)          # 舊檔：照常近 N 天
    span = max(args.days, NEW_TICKER_DAYS) if auto_targets else args.days
    start_day = today - timedelta(days=span)                   # 全域抓取窗口
    warm_day = start_day - timedelta(days=STREAK_WARMUP_DAYS)
    routine_start_iso = routine_start.isoformat()
    start_iso = start_day.isoformat()
    if auto_targets:
        log.info("窗口擴大為 %d 天（僅 %s 寫入全區間，其餘檔仍只處理近 %d 天）",
                 span, "/".join(auto_targets), args.days)

    # 1) T86 含暖身區間（供連買賣天數計算）；其餘端點只抓目標區間
    t86 = {}
    for d in weekdays(warm_day, today):
        got = fetch_t86(d)
        if got is not None:
            t86[d.isoformat()] = got

    margin, sbl, close, qfiis = {}, {}, {}, {}
    for d in weekdays(start_day, today):
        ds = d.isoformat()
        if ds not in t86:   # 假日／尚未公布
            continue
        margin[ds] = fetch_margin(d)
        sbl[ds] = fetch_sbl(d)
        close[ds] = fetch_close(d)
        qfiis[ds] = fetch_qfiis(d)

    trade_dates = sorted(t86)
    target_dates = [x for x in trade_dates if x >= start_iso]
    log.info("目標區間交易日 %d 天", len(target_dates))

    # 2) 逐檔組資料、算外資連買賣天數（正＝連買、負＝連賣、0＝持平）、寫入
    total = 0
    missing = []
    sql_rows = []   # D16 雙寫 SQL Server 用
    for ticker in sorted(watch):
        info = watch[ticker]
        series = [(x, t86[x].get(ticker)) for x in trade_dates]
        if all(v is None for _, v in series):
            missing.append(ticker)   # 上櫃或非上市代號：證交所端點沒有資料
            continue

        streaks = {}
        streak = 0
        for x, v in series:
            f = v[0] if v else None
            if f is None or f == 0:
                streak = 0
            elif f > 0:
                streak = streak + 1 if streak > 0 else 1
            else:
                streak = streak - 1 if streak < 0 else -1
            streaks[x] = streak

        # 新標的走全區間並強制補寫；舊檔仍只處理近 --days 天
        is_target = ticker in auto_targets
        my_start = start_iso if is_target else routine_start_iso
        my_dates = [x for x in target_dates if x >= my_start]
        have = existing_dates(notion, chips_ds, ticker, my_start)

        added = 0
        refilled = 0
        for x in my_dates:
            v = t86[x].get(ticker)
            if v is None:
                continue
            m = margin.get(x, {}).get(ticker)
            vals = {
                "外資買賣超": v[0],
                "投信買賣超": v[1],
                "自營買賣超": v[2],
                "外資連買賣天數": streaks.get(x),
                "融資餘額": m[0] if m else None,
                "融資增減": m[1] if m else None,
                "融券餘額": m[2] if m else None,
                "融券增減": m[3] if m else None,
                "借券賣出餘額": sbl.get(x, {}).get(ticker),
                "收盤價": close.get(x, {}).get(ticker),
            }
            qv = qfiis.get(x, {}).get(ticker)
            if qv:
                vals["外資持股比率"], vals["發行張數"] = qv[0], qv[1]
            # D16 雙寫：不論 Notion 列是新增／補寫／跳過，SQL 都照寫（MERGE 冪等）
            sql_rows.append({
                "symbol": ticker,
                "date": x,
                "foreign_net": vals["外資買賣超"],
                "trust_net": vals["投信買賣超"],
                "dealer_net": vals["自營買賣超"],
                "margin_balance": vals["融資餘額"],
                "short_balance": vals["融券餘額"],
            })
            if x in have:
                # is_target 時無論有沒有加 --refill 都補寫，才能填滿
                # fetch_tdcc.py／signal_tw_l1.py 先建的空殼列
                if args.refill or is_target:
                    update_row(notion, have[x], vals, NUMBER_FIELDS)
                    refilled += 1
                continue
            create_row(notion, chips_ds, ticker, info, x, vals)
            added += 1
        total += added
        fixed = recompute_ratios(notion, chips_ds, ticker, info)
        log.info("%s %s：新增 %d 列、回補更新 %d 列、略過 %d 列、比率重算 %d 列%s",
                 ticker, info["name"], added, refilled, len(have) - refilled, fixed,
                 "（自動回補）" if is_target else "")

        # 3) 記狀態：連續 NO_GAIN_LIMIT 次回補都沒新增列 → 該檔歷史本來就短
        #    （近期上市），標記 coverage_ok 停止再觸發「歷史不足」條件
        if is_target:
            st = state.setdefault(ticker, {})
            st["last_backfill"] = today.isoformat()
            if added == 0:
                st["no_gain"] = st.get("no_gain", 0) + 1
                if st["no_gain"] >= NO_GAIN_LIMIT:
                    st["coverage_ok"] = True
                    log.info("%s：連續 %d 次回補無新增列，標記 coverage_ok",
                             ticker, NO_GAIN_LIMIT)
            else:
                st["no_gain"] = 0
                st.pop("coverage_ok", None)

    for ticker in missing:
        state.setdefault(ticker, {})["no_twse_data"] = True
    for ticker in watch:                       # 重新上市／改列上市時解除封印
        if ticker not in missing and (state.get(ticker) or {}).get("no_twse_data"):
            state[ticker].pop("no_twse_data", None)
    save_state(state)

    if missing:
        log.info("略過非上市標的（證交所無資料）：%s", ", ".join(missing))

    # D16 雙寫：一次 MERGE 進 SQL Server（None 由 upsert_rows 自動轉 NULL）
    if sql_rows:
        upsert_rows("stock_chips", pd.DataFrame(sql_rows))
        log.info("SQL Server stock_chips 雙寫 %d 列", len(sql_rows))
    log.info("完成！本次共新增 %d 列", total)


if __name__ == "__main__":
    main()