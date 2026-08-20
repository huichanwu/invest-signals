# -*- coding: utf-8 -*-
"""SQL Server 雙寫模組：upsert_rows(table, df) 介面比照 notion_writer.py"""
import pyodbc
import pandas as pd

CONN_STR = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost\\SQLEXPRESS;"
    "DATABASE=invest;"
    "Trusted_Connection=yes;"
)

# 每張表的欄位與主鍵定義（欄位順序 = df 欄位順序）
TABLES = {
    "daily_prices": {
        "cols": ["symbol", "date", "open_price", "high", "low", "close_price", "volume"],
        "keys": ["symbol", "date"],
    },
    "market_chips": {
        "cols": ["date", "foreign_futures_net", "foreign_net_change",
                 "top_traders_net", "margin_balance", "margin_maintenance"],
        "keys": ["date"],
    },
    "stock_chips": {
        "cols": ["symbol", "date", "foreign_net", "trust_net", "dealer_net",
                 "margin_balance", "short_balance"],
        "keys": ["symbol", "date"],
    },
    "indicators": {
        "cols": ["symbol", "date", "ma5", "ma10", "ma20", "ma60", "ma120",
                 "k", "d", "macd", "rsi", "bias",
                 "boll_upper", "boll_lower", "mv5", "mv20"],
        "keys": ["symbol", "date"],
    },
    "signals": {
        "cols": ["date", "level", "symbol", "signal_type", "triggered", "reason"],
        "keys": ["date", "level", "signal_type", "symbol"],
    },
    "tdcc_weekly": {
        "cols": ["symbol", "week_date", "over_1000_ratio", "over_400_ratio", "holders_count"],
        "keys": ["symbol", "week_date"],
    },
}


def _bracket(col: str) -> str:
    return "[" + col + "]"  # date/level 是保留字，全部加方括號最省事


def _build_merge_sql(table: str) -> str:
    cfg = TABLES[table]
    cols, keys = cfg["cols"], cfg["keys"]
    non_keys = [c for c in cols if c not in keys]
    col_list = ", ".join(_bracket(c) for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    on = " AND ".join(f"t.{_bracket(k)} = s.{_bracket(k)}" for k in keys)
    update = ", ".join(f"t.{_bracket(c)} = s.{_bracket(c)}" for c in non_keys)
    insert_vals = ", ".join(f"s.{_bracket(c)}" for c in cols)
    sql = (
        f"MERGE {table} AS t "
        f"USING (VALUES ({placeholders})) AS s ({col_list}) "
        f"ON {on} "
    )
    if non_keys:
        sql += f"WHEN MATCHED THEN UPDATE SET {update} "
    sql += f"WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({insert_vals});"
    return sql


def upsert_rows(table: str, df: pd.DataFrame) -> int:
    """把 DataFrame upsert 進指定表，回傳寫入列數。重跑不重複。"""
    if table not in TABLES:
        raise ValueError(f"未定義的表：{table}")
    cols = TABLES[table]["cols"]
    # NaN -> None（8/20 修正：一定要先 astype(object)，float 欄直接 where 會被 pandas 轉回 NaN，
    # 送進 SQL Server 觸發 8023「提供的值不是資料類型 float 的有效執行個體」）
    df = df[cols].astype(object).where(pd.notnull(df[cols]), None)
    sql = _build_merge_sql(table)
    with pyodbc.connect(CONN_STR) as conn:
        cur = conn.cursor()
        cur.fast_executemany = True
        cur.executemany(sql, df.values.tolist())
        conn.commit()
    return len(df)


if __name__ == "__main__":
    # 煙霧測試：upsert 一列假資料到 daily_prices，跑兩次筆數應不變
    test = pd.DataFrame([{
        "symbol": "TEST", "date": "2026-08-18",
        "open_price": 1.0, "high": 1.0, "low": 1.0, "close_price": 1.0, "volume": 100,
    }])
    print("寫入", upsert_rows("daily_prices", test), "列")