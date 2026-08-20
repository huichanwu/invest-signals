import io
import os
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from notion_client import Client

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db_writer import upsert_rows


# 專案根目錄：C:\python\invest-signals
BASE_DIR = Path(__file__).resolve().parents[1]

# 讀取 C:\python\invest-signals\.env
load_dotenv(BASE_DIR / ".env")


# 建立 Notion Client
notion = Client(auth=os.environ["NOTION_TOKEN"])


# 資料庫 ID 集中管理
DB_IDS = {
    "tw_daily_chips": os.getenv("NOTION_DB_TW_DAILY_CHIPS"),
    "tw_stock_chips": os.getenv("NOTION_DB_TW_STOCK_CHIPS"),
    "tdcc_weekly": os.getenv("NOTION_DB_TDCC_WEEKLY"),
    "us_daily_signal": os.getenv("NOTION_DB_US_DAILY_SIGNAL"),
}


# FinMind：期交所籌碼資料的免費資料源
# 不安裝 FinMind 套件，直接用 requests 呼叫官方 HTTP API
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")


def finmind_get(dataset: str, **params) -> pd.DataFrame:
    """呼叫 FinMind HTTP API，回傳 DataFrame（失敗時回傳空表）"""
    query = {"dataset": dataset, "token": FINMIND_TOKEN, **params}
    resp = requests.get(FINMIND_URL, params=query, timeout=30)
    data = resp.json()
    if data.get("status") != 200:
        print(f"FinMind 回應異常（{dataset}）：", data.get("msg"))
    return pd.DataFrame(data.get("data", []))


def num(v):
    """'12,345 (23.5%)'、'1,234' 這類字串 → 數字；'--' 等非數字 → 0.0"""
    s = str(v).split("(")[0].replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0

# 抓近三週資料，取最新交易日寫入
START = "2026-07-20"


def get_database(database_key: str):
    """
    讀取指定資料庫資訊，用來確認 token、database_id、權限是否正常。
    """
    database_id = DB_IDS.get(database_key)

    if not database_id:
        raise ValueError(f"找不到資料庫 ID，請檢查 .env 裡的設定：{database_key}")

    return notion.databases.retrieve(database_id=database_id)


def fetch_foreign() -> pd.DataFrame:
    """
    外資台指期未平倉（期交所三大法人，經 FinMind）：
    net_oi = 多方未平倉口數 - 空方未平倉口數（外資淨口數）
    delta  = 淨口數與前一交易日的差（外資日增減）
    """
    df = finmind_get(
        "TaiwanFuturesInstitutionalInvestors", data_id="TX", start_date=START
    )
    df = df[df["institutional_investors"] == "外資"].copy()
    df["net_oi"] = (
        df["long_open_interest_balance_volume"]
        - df["short_open_interest_balance_volume"]
    )
    df = df.set_index("date")[["net_oi"]]
    df["delta"] = df["net_oi"].diff()
    return df


def fetch_large_traders() -> pd.DataFrame:
    """
    前十大特定法人淨部位——期交所官方 OpenAPI（免費、免金鑰、免會員）。
    注意：這個端點只回傳「最新一個交易日」的資料，約每日 16:30 後更新。
    實際回傳欄位（2026/08/06 實測）：
    Date, Contract, ContractName, SettlementMonth, TypeOfTraders,
    Top5Buy, Top5Sell, Top10Buy, Top10Sell, OIOfMarket
    """
    url = "https://openapi.taifex.com.tw/v1/OpenInterestOfLargeTradersFutures"
    resp = requests.get(url, headers={"accept": "application/json"}, timeout=30)
    resp.raise_for_status()
    try:
        raw = pd.DataFrame(resp.json())
    except ValueError:
        # 2026/08/07 起實測此端點改回傳 CSV（中文欄位）；
        # 翻成與 JSON 版相同的英文欄名，後面篩選邏輯即可沿用
        try:
            csv_df = pd.read_csv(io.StringIO(resp.text))
            csv_df.columns = [str(c).strip() for c in csv_df.columns]
            raw = csv_df.rename(columns={
                "日期": "Date",
                "契約": "Contract",
                "商品名稱(契約名稱)": "ContractName",
                "到期月份(週別)": "SettlementMonth",
                "交易人類別": "TypeOfTraders",
                "前五大交易人買方數量": "Top5Buy",
                "前五大交易人賣方數量": "Top5Sell",
                "前十大交易人買方數量": "Top10Buy",
                "前十大交易人賣方數量": "Top10Sell",
                "全市場未沖銷部位數": "OIOfMarket",
            })
        except Exception as exc:
            print(f"期交所 OpenAPI 回傳非 JSON 也無法解析為 CSV（HTTP {resp.status_code}），特法欄位今天先留空：", exc)
            print("回傳內容前 200 字：", resp.text[:200])
            return pd.DataFrame(columns=["lt_net"])
    if raw.empty:
        print("期交所 OpenAPI 回傳空資料（可能為假日或尚未更新）")
        return pd.DataFrame(columns=["lt_net"])

    # 臺股期貨：契約代碼 TX／TXF 或名稱含「臺股期貨」（相容不同代碼格式）
    code = raw["Contract"].astype(str).str.strip().str.upper()
    cname = raw["ContractName"].astype(str)
    df = raw[code.isin(["TX", "TXF"]) | cname.str.contains("臺股期貨")]

    # 特定法人列（相容 1／1.0／特定法人／specific 等格式）
    t = df["TypeOfTraders"].astype(str).str.strip().str.lower()
    df = df[t.isin(["1", "1.0"]) | t.str.contains("特定") | t.str.contains("specific")]

    if df.empty:
        print("篩不到臺股期貨×特定法人，請把下面實際值貼給 AI 排錯：")
        print(raw[["Contract", "ContractName", "TypeOfTraders"]].drop_duplicates().head(15))
        return pd.DataFrame(columns=["lt_net"])

    # 特法淨部位 = 近月契約（2026/08/06 定案，對齊網站「大額近月」口徑）：
    # API 實測回傳三種 SettlementMonth：近月（如 202608）、
    # 999912（「所有契約」彙總列）、666666（佔位殘值列）；
    # 舊版取 OI 最大列會挑到 999912（8/05 得到 -760），
    # 網站口徑是近月（8/05 = +6,110），故明確取最小有效月份
    m = pd.to_numeric(df["SettlementMonth"], errors="coerce")
    valid = (m % 100 >= 1) & (m % 100 <= 12) & (m != 999912)
    near_month = m[valid].min()
    row = df[valid & (m == near_month)].iloc[0]

    # 日期防呆：相容 2026/08/06、20260806、民國 1150806 三種格式
    s = str(row["Date"]).strip().replace("/", "").replace("-", "")
    if len(s) == 7:  # 民國年
        s = str(int(s[:3]) + 1911) + s[3:]
    day = f"{s[:4]}-{s[4:6]}-{s[6:8]}"

    lt_net = num(row["Top10Buy"]) - num(row["Top10Sell"])
    print(f"期交所大額交易人：{day} 特法淨部位 {lt_net:+.0f}（近月契約）")
    return pd.DataFrame({"lt_net": [lt_net]}, index=[day])


TWSE_URL = "https://www.twse.com.tw/rwd/zh"


def twse_get(path: str, **params) -> dict:
    """證交所官網 JSON API（免費、免金鑰、可指定日期）"""
    resp = requests.get(
        f"{TWSE_URL}/{path}",
        params={**params, "response": "json"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _table(payload: dict, keyword: str):
    """在證交所回傳的 tables 裡找標題含關鍵字的表"""
    for tb in payload.get("tables", []):
        if keyword in tb.get("title", ""):
            return tb
    return None


def _col(tb, keyword: str, occurrence: int = 0):
    """在表的 fields 裡找欄位位置（同名欄位取第 occurrence 個）"""
    hits = [i for i, f in enumerate(tb["fields"]) if keyword in str(f)]
    return hits[occurrence] if len(hits) > occurrence else None


_LAST_MARGIN_BALANCE = None  # mm_ratio 最近一次算出的大盤融資餘額（元），給 SQL Server 留存用


def mm_ratio(day: str):
    """
    M平方式融資維持率 = 不含 ETF 之融資股票市值 / 大盤融資餘額 × 100
    改用證交所官方 JSON API（上市市場）。FinMind 的「依日期抓整個市場」
    是贊助會員限定，免費方案只能逐檔查詢，所以換官方來源。
    """
    d = day.replace("-", "")
    margin = twse_get("marginTrading/MI_MARGN", date=d, selectType="ALL")
    prices = twse_get("afterTrading/MI_INDEX", date=d, type="ALLBUT0999")

    # 只回 {'stat': ...} 代表當日資料尚未公布（融資融券約每晚 21:00 後更新）
    if "creditFields" not in margin and "tables" not in margin:
        print(
            f"證交所 {day} 融資融券資料尚未公布（約每晚 21:00 後更新），"
            "先寫入空值；隔天早上執行即可取得。狀態：",
            margin.get("stat"),
        )
        return None

    # MI_MARGN 不是 tables 包裝（2026/08/06 實測）：
    # 信用交易統計在 creditFields／creditList，個股融資融券在 fields／data
    total_tb = (
        {"fields": margin["creditFields"], "data": margin["creditList"]}
        if "creditFields" in margin
        else _table(margin, "信用交易統計")
    )
    stock_tb = (
        {"fields": margin["fields"], "data": margin["data"]}
        if "fields" in margin
        else _table(margin, "融資融券")
    )
    price_tb = _table(prices, "收盤")
    if not (total_tb and stock_tb and price_tb):
        print(
            "證交所回應格式不符，請把下面實際鍵值貼給 AI 排錯：",
            list(margin.keys()),
            [t.get("title") for t in prices.get("tables", [])],
        )
        return None

    # 全部收盤價：{股票代號: 收盤價}
    pi = _col(price_tb, "收盤")
    close = {str(r[0]).strip(): num(r[pi]) for r in price_tb["data"]}

    # 融資股票市值：個股融資今日餘額(張) × 1000 × 收盤價，排除 00 開頭 ETF
    ci = _col(stock_tb, "今日餘額")  # 第一個「今日餘額」＝融資
    mv = 0.0
    for r in stock_tb["data"]:
        sid = str(r[0]).strip()
        if sid.startswith("00"):  # 排除 ETF（M平方原邏輯）
            continue
        c = close.get(sid, 0.0)
        if c:
            mv += num(r[ci]) * 1000 * c  # 張 → 股

    # 大盤融資餘額：信用交易統計表「融資金額(仟元)」今日餘額 × 1000
    li = _col(total_tb, "今日餘額")
    loan = 0.0
    for r in total_tb["data"]:
        if "融資金額" in str(r[0]):
            loan = num(r[li]) * 1000  # 仟元 → 元
            break

    global _LAST_MARGIN_BALANCE
    if not mv or not loan:
        return None  # 非交易日或當日資料未公布
    _LAST_MARGIN_BALANCE = loan
    return round(mv / loan * 100, 1)


def notion_row_lookup(database_id, day):
    """回傳 (page_id, 外資淨口數)；查無此列回 (None, None)（2026/08/15 改為 upsert 用）"""
    try:
        db = notion.databases.retrieve(database_id=database_id)
        ds_id = db["data_sources"][0]["id"]
        resp = notion.request(
            path=f"data_sources/{ds_id}/query",
            method="POST",
            body={
                "filter": {"property": "資料日期", "date": {"equals": day}},
                "page_size": 1,
            },
        )
        results = resp.get("results")
        if not results:
            return None, None
        page = results[0]
        return page["id"], (page["properties"].get("外資淨口數") or {}).get("number")
    except Exception as exc:
        print("檢查既有資料失敗，為保險起見本次不寫入：", exc)
        return "SKIP", 0  # 查詢失敗時寧可跳過，也不製造重複列


def create_tw_daily_chips_row():
    """
    從網路抓最新交易日的實際籌碼資料，寫入「每日籌碼訊號」。
    1. 外資淨口數／外資日增減：FinMind（期交所三大法人期貨）
    2. 特法淨部位：期交所官方 OpenAPI（大額交易人未沖銷部位）
    3. 融資維持率：證交所官方 JSON API＋M平方式公式（上市融資市值／融資餘額）
    連三日降等布林訊號需要跨日邏輯，留到 D7 的 signal_tw_l0.py 再算。
    """
    database_id = DB_IDS.get("tw_daily_chips")

    if not database_id:
        raise ValueError("找不到 NOTION_DB_TW_DAILY_CHIPS，請檢查 .env")

    lt = fetch_large_traders()
    df = fetch_foreign().join(lt, how="left")
    # 基準日＝特法資料的日期：期交所 OpenAPI 更新最慢（隔日清晨），
    # 以它為準時，外資（當日 15:00）與融資（當晚 21:00）一定都已公布，
    # 不論早上或晚上執行，三個欄位都能齊、日期也一致
    if not lt.empty and lt.index[-1] in df.index:
        day = lt.index[-1]
    else:
        day = df.index[-1]  # 特法抓不到時退回外資最新交易日
        if not lt.empty:
            print(f"提醒：特法資料日期 {lt.index[-1]} 不在外資資料範圍，改用 {day}")
    r = df.loc[day]

    try:
        ratio = mm_ratio(day)
    except Exception as exc:
        print("融資維持率抓取失敗，先寫入空值：", exc)
        ratio = None

    # ===== D3 新增：本地 CSV 留存（2026/08/07）=====
    csv_dir = BASE_DIR / "data" / "taifex"
    csv_dir.mkdir(parents=True, exist_ok=True)  # 資料夾不存在會自動建立
    csv_path = csv_dir / f"{day.replace('-', '')}.csv"

    if csv_path.exists():
        print(f"CSV 已存在，跳過：{csv_path}")
    else:
        row_out = df.loc[[day]].copy()
        row_out["mm_ratio"] = ratio  # 連融資維持率一起存（2026/08/07 補）
        row_out.to_csv(csv_path, encoding="utf-8-sig")  # 只存當日一列（2026/08/07 定案）
        print(f"已留存 CSV：{csv_path}")
    # ===== D3 新增結束 =====

    # ===== 寫入前去重（2026/08/15 改為 upsert：空殼列會補寫數值）=====
    page_id, existing = notion_row_lookup(database_id, day)
    if page_id and existing is not None:
        print(f"資料庫已有 {day} 的完整資料，跳過 Notion 寫入（避免重複列）")
        return day, r, ratio, None
    # ===== 去重結束 =====

    props = {
            # title 欄位：你的資料庫裡叫「日期」
            "日期": {
                "title": [
                    {
                        "text": {
                            "content": day
                        }
                    }
                ]
            },

            # date 欄位：你的資料庫裡叫「資料日期」
            "資料日期": {
                "date": {
                    "start": day
                }
            },

            # number 欄位：全部是真實市場數據
            "外資淨口數": {
                "number": int(r["net_oi"])
            },
            "外資日增減": {
                "number": int(r["delta"]) if pd.notna(r["delta"]) else None
            },
            "特法淨部位": {
                "number": int(r["lt_net"]) if pd.notna(r["lt_net"]) else None
            },
            "融資維持率": {
                "number": ratio
            },

            # checkbox 欄位：布林訊號留給 D7 的 signal_tw_l0.py，先保持未勾
            "事件窗": {
                "checkbox": False
            },
            "連三日降": {
                "checkbox": False
            },
            "維持率止穩": {
                "checkbox": False
            },
            "特法翻正站穩": {
                "checkbox": False
            },

            # rich_text 欄位：你的資料庫裡叫「備註」
            "備註": {
                "rich_text": [
                    {
                        "text": {
                            "content": "D2 串接驗證：期交所×FinMind 實際資料"
                        }
                    }
                ]
            },
    }

    if page_id:
        response = notion.pages.update(page_id=page_id, properties=props)
        print(f"{day} 已有空殼列（別的腳本先建），已補寫數值")
    else:
        response = notion.pages.create(parent={"database_id": database_id}, properties=props)

    return day, r, ratio, response


if __name__ == "__main__":
    print("=== Step 1：讀取每日籌碼訊號資料庫 ===")
    db = get_database("tw_daily_chips")

    print("資料庫名稱：", db["title"][0]["plain_text"])
    print("資料庫 ID：", db["id"])

    print("\n=== Step 2：抓取最新籌碼資料並寫入 ===")
    day, r, ratio, page = create_tw_daily_chips_row()

    print("寫入成功！")
    print("資料日期：", day)
    print("外資淨口數：", int(r["net_oi"]))
    print("外資日增減：", int(r["delta"]) if pd.notna(r["delta"]) else None)
    print("特法淨部位：", int(r["lt_net"]) if pd.notna(r["lt_net"]) else None)
    print("融資維持率：", ratio)
    print("頁面 URL：", page["url"] if page else "（已存在，本次跳過寫入）")

    # ===== SQL Server 留存（2026/08/19 修正：搬進 main、改用實際存在的變數）=====
    # 原本掛在檔案最外層且用了未定義的 day_iso 等變數，執行會 NameError，
    # 連 backfill.py import 本檔時也會觸發；Notion 端跳過寫入時這裡照樣 upsert（不會重複）
    df_mc = pd.DataFrame([{
        "date": day,                                                              # 'YYYY-MM-DD'
        "foreign_futures_net": int(r["net_oi"]),                                  # 外資淨口數
        "foreign_net_change": int(r["delta"]) if pd.notna(r["delta"]) else None,  # 外資日增減
        "top_traders_net": int(r["lt_net"]) if pd.notna(r["lt_net"]) else None,   # 特法淨部位
        "margin_balance": _LAST_MARGIN_BALANCE,                                   # 大盤融資餘額（元）
        "margin_maintenance": ratio,                                              # 自家口徑維持率
    }])
    upsert_rows("market_chips", df_mc)
    print("SQL Server 已 upsert market_chips 一列")