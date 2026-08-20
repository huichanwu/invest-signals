import io
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from notion_client import Client

# 重用 notion_writer.py 裡的共用函式（同資料夾直接 import）
from notion_writer import finmind_get, mm_ratio, num

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

notion = Client(auth=os.environ["NOTION_TOKEN"])
DB_ID = os.getenv("NOTION_DB_TW_DAILY_CHIPS")

# 回補區間：執行時用參數指定即可，不用改程式碼
# 用法：python backfill.py 2026-07-01 2026-08-05
# 不給參數時才用下面的預設值
DEFAULT_START = "2026-07-01"
DEFAULT_END = "2026-08-05"

BACKFILL_START = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START
BACKFILL_END = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_END


def fetch_foreign_hist(start: str, end: str) -> pd.DataFrame:
    """外資台指期淨口數（歷史區間，FinMind 免費）"""
    df = finmind_get(
        "TaiwanFuturesInstitutionalInvestors",
        data_id="TX", start_date=start, end_date=end,
    )
    df = df[df["institutional_investors"] == "外資"].copy()
    df["net_oi"] = (
        df["long_open_interest_balance_volume"]
        - df["short_open_interest_balance_volume"]
    )
    df = df.set_index("date")[["net_oi"]]
    df["delta"] = df["net_oi"].diff()
    return df


def fetch_large_traders_hist(start: str, end: str) -> pd.DataFrame:
    """特法淨部位歷史——期交所網站盤後 CSV（每次最多 30 天，自動分段）"""
    url = "https://www.taifex.com.tw/cht/3/largeTraderFutDown"
    frames = []
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    while s <= e:
        chunk_end = min(s + pd.Timedelta(days=29), e)
        resp = requests.post(
            url,
            data={
                "queryStartDate": s.strftime("%Y/%m/%d"),
                "queryEndDate": chunk_end.strftime("%Y/%m/%d"),
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=60,
        )
        resp.encoding = "big5"
        try:
            frames.append(pd.read_csv(io.StringIO(resp.text)))
        except Exception as exc:
            print(f"期交所 CSV 解析失敗（{s:%Y/%m/%d}～{chunk_end:%Y/%m/%d}）：", exc)
        time.sleep(2)
        s = chunk_end + pd.Timedelta(days=1)
    if not frames:
        return pd.DataFrame(columns=["lt_net"])

    raw = pd.concat(frames, ignore_index=True)
    raw.columns = [str(c).strip() for c in raw.columns]

    def col(*keys):
        """欄位防呆：先找完全相同，再找含關鍵字的欄名"""
        for c in raw.columns:
            if len(keys) == 1 and keys[0] == c:
                return c
        for c in raw.columns:
            if all(k in c for k in keys):
                return c
        raise KeyError(f"找不到欄位 {keys}，實際欄位：{list(raw.columns)}")

    c_date = col("日期")
    c_contract = col("契約")
    c_type = col("交易人類別")
    c_buy = col("前十大", "買")
    c_sell = col("前十大", "賣")

    # 臺股期貨：代碼 TX／TXF，或任一名稱欄含「臺股期貨」
    m = raw[c_contract].astype(str).str.strip().str.upper().isin(["TX", "TXF"])
    for c in raw.columns:
        if "名稱" in c:
            m = m | raw[c].astype(str).str.contains("臺股期貨", na=False)
    df = raw[m]

    # 特定法人列（相容 1／1.0／特定法人等格式）
    t = df[c_type].astype(str).str.strip()
    df = df[t.isin(["1", "1.0"]) | t.str.contains("特定", na=False)]
    if df.empty:
        print("篩不到臺股期貨×特定法人，請把下面實際值貼給 AI 排錯：")
        print(raw[[c_contract, c_type]].drop_duplicates().head(15))
        return pd.DataFrame(columns=["lt_net"])

    # 特法淨部位 = 每日近月契約（2026/08/06 定案，同 OpenAPI 版）：
    # 排除「所有契約」彙總列與佔位列，取到期月份最小的一列；
    # 若印出「找不到欄位」訊息，把實際欄位貼給 AI 排錯
    c_month = col("月份")
    out = {}
    for d, g in df.groupby(c_date):
        m = pd.to_numeric(g[c_month], errors="coerce")
        valid = (m % 100 >= 1) & (m % 100 <= 12) & (m != 999912)
        if not valid.any():
            continue
        near = m[valid].min()
        row = g[valid & (m == near)].iloc[0]
        day = pd.to_datetime(str(d).strip()).strftime("%Y-%m-%d")
        out[day] = num(row[c_buy]) - num(row[c_sell])
    return pd.DataFrame({"lt_net": pd.Series(out)})


def existing_dates() -> set:
    """讀出資料庫已有的資料日期，避免重複寫入"""
    dates, cursor = set(), None
    try:
        while True:
            kwargs = {"database_id": DB_ID}
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = notion.databases.query(**kwargs)
            for page in resp["results"]:
                prop = page["properties"].get("資料日期", {}).get("date")
                if prop and prop.get("start"):
                    dates.add(prop["start"][:10])
            if not resp.get("has_more"):
                break
            cursor = resp["next_cursor"]
    except Exception as exc:
        print("讀取既有資料失敗（本次不做去重，可能寫出重複列）：", exc)
    return dates


def write_row(day: str, r: pd.Series, ratio):
    notion.pages.create(
        parent={"database_id": DB_ID},
        properties={
            "日期": {"title": [{"text": {"content": day} }] },
            "資料日期": {"date": {"start": day}},
            "外資淨口數": {"number": int(r["net_oi"])},
            "外資日增減": {"number": int(r["delta"]) if pd.notna(r["delta"]) else None},
            "特法淨部位": {"number": int(r["lt_net"]) if pd.notna(r["lt_net"]) else None},
            "融資維持率": {"number": ratio},
            "事件窗": {"checkbox": False},
            "連三日降": {"checkbox": False},
            "維持率止穩": {"checkbox": False},
            "特法翻正站穩": {"checkbox": False},
            "備註": {"rich_text": [{"text": {"content": "歷史回補（backfill.py）"}}]},
        },
    )


if __name__ == "__main__":
    print(f"=== 歷史回補：{BACKFILL_START} ～ {BACKFILL_END} ===")

    # 外資多往前抓 10 天，讓區間第一天也算得出日增減
    pad = (pd.Timestamp(BACKFILL_START) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    foreign = fetch_foreign_hist(pad, BACKFILL_END)
    lt = fetch_large_traders_hist(BACKFILL_START, BACKFILL_END)

    df = foreign.join(lt, how="left")
    df = df[df.index >= BACKFILL_START]

    skip = existing_dates()
    print(f"資料庫已有 {len(skip)} 個日期，將自動跳過")

    for day, r in df.iterrows():
        if day in skip:
            print(f"{day} 已存在，跳過")
            continue
        try:
            ratio = mm_ratio(day)
        except Exception as exc:
            print(f"{day} 融資維持率抓取失敗，先寫空值：", exc)
            ratio = None
        write_row(day, r, ratio)
        print(
            f"{day} 寫入完成  外資 {int(r['net_oi'])}  "
            f"特法 {int(r['lt_net']) if pd.notna(r['lt_net']) else None}  維持率 {ratio}"
        )
        time.sleep(3)  # 證交所有流量限制，跑太快會被擋

    print("=== 回補完成 ===")