# -*- coding: utf-8 -*-
"""一次性回填：Notion 每日籌碼訊號 → SQL Server market_chips（跑一次即可丟棄）"""
import os
import pandas as pd
from dotenv import load_dotenv
from notion_client import Client
from db_writer import upsert_rows

load_dotenv()
notion = Client(auth=os.environ["NOTION_TOKEN"])
DB_ID = os.environ["NOTION_DB_TW_DAILY_CHIPS"]   # 每日籌碼訊號

# Notion 屬性名 → market_chips 欄位（左邊名稱若和你資料庫不同，照實改）
PROP_MAP = {
    "外資淨口數": "foreign_futures_net",
    "外資日增減": "foreign_net_change",
    "特法淨部位": "top_traders_net",
    "融資餘額":   "margin_balance",
    "融資維持率": "margin_maintenance",
}
DATE_PROP = "資料日期"

def get_ds_id(any_id):
    try:
        return notion.databases.retrieve(database_id=any_id)["data_sources"][0]["id"]
    except Exception:
        return notion.data_sources.retrieve(data_source_id=any_id)["id"]

ds = get_ds_id(DB_ID)
rows, cursor = [], None
while True:
    kwargs = {"data_source_id": ds, "page_size": 100}
    if cursor:
        kwargs["start_cursor"] = cursor
    resp = notion.data_sources.query(**kwargs)
    for p in resp["results"]:
        pr = p["properties"]
        d = ((pr.get(DATE_PROP) or {}).get("date") or {}).get("start")
        if not d:
            continue
        row = {"date": d[:10]}
        for notion_name, col in PROP_MAP.items():
            row[col] = (pr.get(notion_name) or {}).get("number")
        rows.append(row)
    if not resp.get("has_more"):
        break
    cursor = resp["next_cursor"]

df = pd.DataFrame(rows).sort_values("date")
print(f"讀到 {len(df)} 列，區間 {df['date'].min()} ～ {df['date'].max()}")
print("寫入", upsert_rows("market_chips", df), "列")
