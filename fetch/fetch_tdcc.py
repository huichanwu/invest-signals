# -*- coding: utf-8 -*-
"""fetch_tdcc.py — 集保股權分散表：千張／400張大戶比率

資料來源（免費、免註冊）：
    https://opendata.tdcc.com.tw/getOD.ashx?id=1-5
    每週五收盤後結算、週六凌晨更新，一個 CSV 含全部上市＋上櫃證券（含 ETF）。

計算：
    千張大戶比率  = 級距 15（1,000,001 股以上）佔集保庫存比例
    400張大戶比率 = 級距 12＋13＋14＋15（400,001 股以上）合計

寫入：
    「個股籌碼日資料」中 資料日期 >= 本期結算日 的每一列（前向填值）。
    每天排程執行一次，新增的日資料列會自動補上最新一期數值。
    另雙寫本機 SQL Server invest.tdcc_weekly（D16：db_writer.upsert_rows，
    MERGE 去重，重跑不重複）。

歷史回補：
    python fetch\fetch_tdcc.py --backfill 60
    集保開放 CSV 只有最新一期；回補改爬集保官網「集保戶股權分散表」查詢頁
    （免註冊、可回查約一年），逐週查詢後依「結算日 <= 資料日期 < 下一期」回填。
    （原走 FinMind，但股權分散表資料集只開放贊助會員，免費 token 也會回 400）

2026/08/20 修訂：
    新標的自動偵測改為 tdcc 自帶（判斷基準＝「400張大戶比率」非空的有效列），
    與 fetch_stock_chips.py 完全脫鉤——修掉「chips 先補滿收盤價 → tdcc 偵測
    永遠命中 0 檔」的盲點；執行順序從此不影響大戶比率回補。

排程建議：每日 21:35（接在 fetch_stock_chips.py 之後）
"""

import argparse
import csv
import io
import json
import logging
import os
import re
import ssl
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from notion_client import Client
from requests.adapters import HTTPAdapter

TDCC_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
TDCC_QRY_URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
HEADERS = {"User-Agent": "Mozilla/5.0"}
AUTO_BACKFILL_DAYS = 60   # 自動偵測到新標的／斷更檔時的回補天數

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "fetch_tdcc.log")

# D16 雙寫：db_writer.py 在專案根目錄（C:\python\invest-signals\db_writer.py）
sys.path.insert(0, os.path.dirname(BASE_DIR))
from db_writer import upsert_rows
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tdcc")

# ---- 2026/08/20：tdcc 自帶偵測（判斷基準＝400張大戶比率非空）----
STATE_FILE = Path(BASE_DIR) / "state" / "backfill_state.json"   # 與 chips 共用檔案、不同 key
TDCC_GAP_DAYS = 12        # 最新有效列落後 12 天視為斷更（週資料，容忍約 2 個結算週）
TDCC_SLACK_DAYS = 14      # 「歷史夠早」容忍天數（週資料粒度粗，放寬到 2 週）
TDCC_NO_GAIN_LIMIT = 2    # 連 2 次回補「最早有效列」都沒變早 → 標記 coverage_ok


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
                          encoding="utf-8")


def tdcc_coverage(notion, chips_ds, ticker):
    """回傳 (有效列數, 最早日, 最新日)；有效＝「400張大戶比率」非空的列。
    不看收盤價——那是 chips 的事，兩條管線各管各的覆蓋率。"""
    flt = {"and": [
        {"property": "代號", "rich_text": {"equals": str(ticker)}},
        {"property": "400張大戶比率", "number": {"is_not_empty": True}},
    ]}
    dates, cursor = [], None
    while True:
        kwargs = {"data_source_id": chips_ds, "filter": flt, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.data_sources.query(**kwargs)
        for p in resp["results"]:
            d = ((p["properties"].get("資料日期") or {}).get("date") or {}).get("start")
            if d:
                dates.append(d[:10])
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    if not dates:
        return 0, None, None
    dates.sort()
    return len(dates), dates[0], dates[-1]


def detect_tdcc_targets(notion, chips_ds, tickers, today, state):
    """需要回補大戶比率的標的：全新／斷更／歷史不足（state 用 tdcc: 前綴，不與 chips 打架）"""
    stale_before = (today - timedelta(days=TDCC_GAP_DAYS)).isoformat()
    need_start = (today - timedelta(days=AUTO_BACKFILL_DAYS - TDCC_SLACK_DAYS)).isoformat()
    targets = []
    for ticker in sorted(tickers):
        st = state.get("tdcc:" + str(ticker)) or {}
        n, first, last = tdcc_coverage(notion, chips_ds, ticker)
        if n == 0:
            reason = "全新（無任何大戶比率）"
        elif last < stale_before:
            reason = "斷更：最新有效列 %s" % last
        elif not st.get("coverage_ok") and first > need_start:
            reason = "歷史不足：最早有效列 %s（僅 %d 筆）" % (first, n)
        else:
            continue
        targets.append(ticker)
        log.info("🔎 tdcc 回補候選 %s → %s", ticker, reason)
    return targets


class TDCCAdapter(HTTPAdapter):
    """集保網站憑證鏈缺 Subject Key Identifier，Python 3.13 起預設開啟的
    VERIFY_X509_STRICT 嚴格檢查會直接擋下（SSLCertVerificationError）。
    這裡只關閉 strict 旗標，其餘憑證驗證照舊。"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def download_csv():
    last_err = None
    session = requests.Session()
    session.mount("https://", TDCCAdapter())
    for attempt in range(3):
        try:
            r = session.get(TDCC_URL, headers=HEADERS, timeout=90)
            r.raise_for_status()
            return r.content
        except Exception as e:
            last_err = e
            log.warning("下載失敗（第 %d 次）：%s", attempt + 1, e)
            time.sleep(3)
    raise RuntimeError("集保 CSV 下載失敗：%s" % last_err)


def parse_csv(raw):
    """回傳 (結算日 'YYYY-MM-DD', {代號: 千張%}, {代號: 400張%}, {代號: 股東總人數})"""
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    next(reader, None)  # 跳過表頭
    data_date = None
    p1000 = {}
    p400 = {}
    hcount = {}
    for row in reader:
        if len(row) < 6:
            continue
        d = row[0].strip()
        ticker = row[1].strip()
        level = row[2].strip()
        pct = row[5].strip()
        if data_date is None and len(d) == 8 and d.isdigit():
            data_date = d[:4] + "-" + d[4:6] + "-" + d[6:8]
        try:
            lv = int(level)
            val = float(pct)
        except ValueError:
            continue
        if lv == 15:
            p1000[ticker] = round(p1000.get(ticker, 0.0) + val, 2)
        if 12 <= lv <= 15:
            p400[ticker] = round(p400.get(ticker, 0.0) + val, 2)
        if lv == 17:  # 合計列：人數欄＝股東總人數（tdcc_weekly.holders_count 用）
            try:
                hcount[ticker] = int(float(row[3].strip().replace(",", "")))
            except ValueError:
                pass
    return data_date, p1000, p400, hcount


def get_data_source_id(notion, db_id):
    db = notion.databases.retrieve(db_id)
    sources = db.get("data_sources") or []
    if not sources:
        raise RuntimeError("資料庫沒有 data source，請確認已分享給 integration")
    return sources[0]["id"]


def query_all(notion, ds_id, flt):
    results = []
    cursor = None
    while True:
        kwargs = dict(data_source_id=ds_id, page_size=100)
        if flt:
            kwargs["filter"] = flt
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.data_sources.query(**kwargs)
        results.extend(resp.get("results") or [])
        if not resp.get("has_more"):
            return results
        cursor = resp.get("next_cursor")


def fetch_watchlist(notion):
    """從 💹 個股現價 撈 國家=台股 的代號清單"""
    ds_id = get_data_source_id(notion, os.environ["NOTION_DATABASE_ID"])
    flt = dict(property="國家", select=dict(equals="台股"))
    tickers = []
    for page in query_all(notion, ds_id, flt):
        props = page.get("properties") or {}
        title = (props.get("股票代號") or {}).get("title") or []
        ticker = "".join(t.get("plain_text", "") for t in title).strip()
        if ticker:
            tickers.append(ticker)
    return sorted(set(tickers))


def num_prop(props, name):
    return (props.get(name) or {}).get("number")


def tdcc_open_session():
    """開一個掛了 TDCCAdapter 的 session，回傳 (session, 查詢權杖, 可查日期清單)。

    集保查詢頁有 CSRF 權杖（SYNCHRONIZER_TOKEN，藏在表單 hidden 欄位）
    搭配 session cookie，先 GET 一次頁面把兩者拿到手，之後才能 POST 查詢。"""
    session = requests.Session()
    session.mount("https://", TDCCAdapter())
    session.headers.update(HEADERS)
    session.headers["Referer"] = TDCC_QRY_URL
    r = session.get(TDCC_QRY_URL, timeout=60)
    r.raise_for_status()
    token = extract_sync_token(r.text)
    dates = sorted(set(re.findall(r'<option value="(\d{8})"', r.text)))
    if not token or not dates:
        raise RuntimeError("解析不到查詢權杖或日期選單（集保官網可能改版）")
    return session, token, dates


def extract_sync_token(html):
    m = re.search(r'SYNCHRONIZER_TOKEN"[^>]*value="([^"]+)"', html)
    if not m:
        m = re.search(r'value="([^"]+)"[^>]*name="SYNCHRONIZER_TOKEN"', html)
    return m.group(1) if m else None


def parse_qry_table(html):
    """查詢結果的 HTML 表格 → (千張%, 400張%, 股東總人數)；查無資料回傳 None。

    以各級距下限判斷：>=1,000,001 計入千張、>=400,001 計入 400張，
    與集保開放 CSV 的級距 12～15 同口徑。"""
    p1000 = p400 = 0.0
    holders = 0
    hit = False
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", td).replace("&nbsp;", " ").strip()
                 for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 5:
            continue
        digits = re.findall(r"\d+", cells[1].replace(",", ""))  # 持股分級欄
        if not digits:
            continue  # 合計、差異數調整、查無此資料等列
        try:
            pct = float(cells[4].replace("%", "").replace(",", ""))
        except ValueError:
            continue
        hit = True
        low = int(digits[0])
        if low >= 1000001:
            p1000 = round(p1000 + pct, 2)
        if low >= 400001:
            p400 = round(p400 + pct, 2)
        try:  # 人數欄：各級距加總＝股東總人數
            holders += int(cells[2].replace(",", ""))
        except ValueError:
            pass
    return (p1000, p400, holders) if hit else None


def fetch_history_tdcc(session, token, dates, ticker):
    """集保官網逐週查一檔 → ({結算日: (千張%, 400張%, 股東總人數)}, 最新權杖)"""
    out = {}
    for d in dates:
        payload = {
            "method": "submit",
            "firDate": dates[-1],
            "scaDate": d,
            "sqlMethod": "StockNo",
            "stockNo": ticker,
            "stockName": "",
            "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock",
            "SYNCHRONIZER_TOKEN": token,
        }
        r = session.post(TDCC_QRY_URL, data=payload, timeout=60)
        r.raise_for_status()
        token = extract_sync_token(r.text) or token  # 權杖會換發，拿新的接著用
        parsed = parse_qry_table(r.text)
        if parsed:
            out["%s-%s-%s" % (d[:4], d[4:6], d[6:8])] = parsed
        time.sleep(1.2)  # 對官網禮貌一點，避免被擋
    return out, token


def backfill(notion, chips_ds, tickers, days):
    """歷史回補：近 days 天的每週數值，依「結算日 <= 資料日期 < 下一期」逐週回填。"""
    since = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    session, token, all_dates = tdcc_open_session()
    dates = [d for d in all_dates if d >= since]
    if not dates:
        log.error("集保查詢頁沒有 %s 之後的結算日（官網僅保留約一年）", since)
        return
    log.info("要回補 %d 期：%s ～ %s", len(dates), dates[0], dates[-1])
    total = 0
    for ticker in tickers:
        try:
            hist, token = fetch_history_tdcc(session, token, dates, ticker)
        except Exception as e:
            log.warning("%s：集保官網抓取失敗，略過：%s", ticker, e)
            continue
        if not hist:
            log.warning("%s：查無股權分散資料，略過", ticker)
            continue
        weeks = sorted(hist)
        f_ticker = dict(property="代號", rich_text=dict(equals=ticker))
        f_date = dict(property="資料日期", date=dict(on_or_after=weeks[0]))
        flt = {"and": [f_ticker, f_date]}
        updated = 0
        for row in query_all(notion, chips_ds, flt):
            props = row.get("properties") or {}
            d = (((props.get("資料日期") or {}).get("date") or {}).get("start") or "")[:10]
            wk = None
            for w in weeks:            # 找最近一期 結算日 <= 資料日期
                if w <= d:
                    wk = w
                else:
                    break
            if not wk:
                continue
            v1000, v400, _ = hist[wk]
            if num_prop(props, "千張大戶比率") == v1000 and num_prop(props, "400張大戶比率") == v400:
                continue
            notion.pages.update(page_id=row["id"], properties={
                "千張大戶比率": dict(number=v1000),
                "400張大戶比率": dict(number=v400),
            })
            updated += 1
            time.sleep(0.34)
        total += updated
        # D16 雙寫：整段回補歷史逐週 upsert 進 SQL Server tdcc_weekly
        df_tdcc = pd.DataFrame([{
            "symbol": ticker, "week_date": wk,
            "over_1000_ratio": hist[wk][0], "over_400_ratio": hist[wk][1],
            "holders_count": hist[wk][2],
        } for wk in weeks])
        upsert_rows("tdcc_weekly", df_tdcc)
        log.info("%s：%d 期（%s ～ %s），回填 %d 列", ticker, len(weeks), weeks[0], weeks[-1], updated)
        time.sleep(0.5)
    log.info("回補完成，共更新 %d 列", total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0, metavar="N",
                    help="爬集保官網回補近 N 天歷史（例：--backfill 60）；不加＝日常抓最新一期")
    ap.add_argument("--no-auto-backfill", action="store_true",
                    help="關閉新標的自動偵測回補")
    args = ap.parse_args()

    # .env 放在專案根目錄（C:\python\invest-signals\.env），與其他腳本共用；
    # 先讀根目錄，再讀 fetch\ 內的（若有）作備援
    load_dotenv(os.path.join(os.path.dirname(BASE_DIR), ".env"))
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    token = os.getenv("NOTION_TOKEN")
    if not all([token, os.getenv("NOTION_DB_TW_STOCK_CHIPS"), os.getenv("NOTION_DATABASE_ID")]):
        raise SystemExit("請檢查 .env：NOTION_TOKEN / NOTION_DB_TW_STOCK_CHIPS / NOTION_DATABASE_ID")
    notion = Client(auth=token)
    chips_ds = get_data_source_id(notion, os.environ["NOTION_DB_TW_STOCK_CHIPS"])

    tickers = fetch_watchlist(notion)
    log.info("觀察清單 %d 檔：%s", len(tickers), ", ".join(tickers))

    if args.backfill:
        backfill(notion, chips_ds, tickers, args.backfill)
        return

    # 自動偵測：只看大戶比率的覆蓋率，與 chips 完全脫鉤（2026/08/20 修訂）
    if not args.no_auto_backfill:
        state = _load_state()
        targets = detect_tdcc_targets(notion, chips_ds, tickers, date.today(), state)
        if targets:
            log.info("🔎 集保自動回補 %d 檔：%s", len(targets), ", ".join(targets))
            before = {t: tdcc_coverage(notion, chips_ds, t)[1] for t in targets}
            backfill(notion, chips_ds, targets, AUTO_BACKFILL_DAYS)
            # 防呆：近期掛牌（如 00403A／00407A）回補後最早日不會變早，
            # 連 TDCC_NO_GAIN_LIMIT 次沒進展就標 coverage_ok 停止觸發「歷史不足」
            for t in targets:
                st = state.setdefault("tdcc:" + str(t), {})
                st["last_backfill"] = date.today().isoformat()
                after_first = tdcc_coverage(notion, chips_ds, t)[1]
                if after_first == before[t]:
                    st["no_gain"] = st.get("no_gain", 0) + 1
                    if st["no_gain"] >= TDCC_NO_GAIN_LIMIT:
                        st["coverage_ok"] = True
                        log.info("%s：連續 %d 次回補最早日未變早，標記 coverage_ok",
                                 t, TDCC_NO_GAIN_LIMIT)
                else:
                    st["no_gain"] = 0
                    st.pop("coverage_ok", None)
            _save_state(state)

    # 接著照常跑日常前向填值

    data_date, p1000, p400, hcount = parse_csv(download_csv())
    if not data_date:
        log.error("解析不到結算日，中止")
        return
    log.info("集保股權分散表結算日：%s", data_date)

    total = 0
    for ticker in tickers:
        v1000 = p1000.get(ticker)
        v400 = p400.get(ticker)
        if v1000 is None and v400 is None:
            log.warning("%s：本期查無股權分散資料，略過", ticker)
            continue
        v1000 = v1000 or 0.0
        v400 = v400 or 0.0
        f_ticker = dict(property="代號", rich_text=dict(equals=ticker))
        f_date = dict(property="資料日期", date=dict(on_or_after=data_date))
        flt = {"and": [f_ticker, f_date]}
        updated = 0
        for row in query_all(notion, chips_ds, flt):
            props = row.get("properties") or {}
            if num_prop(props, "千張大戶比率") == v1000 and num_prop(props, "400張大戶比率") == v400:
                continue
            new_props = {
                "千張大戶比率": dict(number=v1000),
                "400張大戶比率": dict(number=v400),
            }
            notion.pages.update(page_id=row["id"], properties=new_props)
            updated += 1
            time.sleep(0.34)
        total += updated

        # D16 雙寫：SQL Server invest.tdcc_weekly（MERGE 去重，重跑不重複）
        df_tdcc = pd.DataFrame([{
            "symbol": ticker, "week_date": data_date,   # 該週結算日 'YYYY-MM-DD'
            "over_1000_ratio": v1000, "over_400_ratio": v400,
            "holders_count": hcount.get(ticker),
        }])
        upsert_rows("tdcc_weekly", df_tdcc)
        log.info("%s：千張 %.2f%% / 400張 %.2f%%（%s），更新 %d 列", ticker, v1000, v400, data_date, updated)

    log.info("完成，共更新 %d 列", total)


if __name__ == "__main__":
    main()