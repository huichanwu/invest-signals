import os
import time
from datetime import datetime

import twstock
import yfinance as yf
from dotenv import load_dotenv
from notion_client import Client

load_dotenv()
notion = Client(auth=os.environ["NOTION_TOKEN"])
DB_ID = os.environ["NOTION_DATABASE_ID"]


def get_price(ticker: str, market: str):
    """台股走 yfinance(.TW)，失敗時用 twstock 備援；美股直接 yfinance。"""
    if market == "台股":
        try:
            p = yf.Ticker(f"{ticker}.TW").fast_info["last_price"]
            if p:
                return round(float(p), 2)
        except Exception:
            pass
        rt = twstock.realtime.get(ticker)  # 備援：證交所即時報價
        if rt.get("success"):
            return round(float(rt["realtime"]["latest_trade_price"]), 2)
        return None
    try:
        return round(float(yf.Ticker(ticker).fast_info["last_price"]), 2)
    except Exception:
        return None


def main():
    db = notion.databases.retrieve(database_id=DB_ID)
    ds_id = db["data_sources"][0]["id"]  # 新版 API：資料庫底下的 data source
    rows = notion.data_sources.query(data_source_id=ds_id)["results"]
    for row in rows:
        props = row["properties"]
        title = props["股票代號"]["title"]
        if not title:  # 空白列（沒填股票代號）直接跳過
            print("[SKIP] 空白列，未填股票代號")
            continue
        ticker = title[0]["plain_text"].strip()
        if props["國家"]["select"] is None:  # 「國家」沒選也跳過
            print(f"[SKIP] {ticker} 未選國家")
            continue
        market = props["國家"]["select"]["name"]
        price = get_price(ticker, market)
        if price is None:
            print(f"[SKIP] {ticker} 抓不到價格")
            continue
        notion.pages.update(
            page_id=row["id"],
            properties={
                "市價": {"number": price},
                "更新時間": {"date": {"start": datetime.now().astimezone().isoformat()}},
            },
        )
        print(f"[OK] {ticker} = {price}")
        time.sleep(0.4)  # Notion API 限速約 3 req/s，保守一點


if __name__ == "__main__":
    main()