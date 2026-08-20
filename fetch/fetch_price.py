# -*- coding: utf-8 -*-
r'''fetch_price.py — D12｜台美股價量抓取（台股 FinMind 主力；yfinance／twstock 備援，美股 yfinance）

用法（專案根目錄 C:\python\invest-signals）：
    python fetch\fetch_price.py --ticker 3231            # 台股：FinMind → yfinance(.TW/.TWO) → twstock
    python fetch\fetch_price.py --ticker NVDA            # 美股：代號原樣
    python fetch\fetch_price.py --ticker 3231 --adjust   # 還原價（回測／跨除權息長期均線用）
    python fetch\fetch_price.py --ticker 3231 --csv      # 另存本機 CSV 快取（非必要）

被其他模組使用：from fetch.fetch_price import get_price

定案：預設原始價 → 與 Yahoo 台灣／看盤軟體對帳一致。
D16：抓完價後 upsert_rows('daily_prices', ...) 雙寫本機 SQL Server invest（Notion 管線照舊）。
注意：台股 Volume 單位是「股」，要張數請 /1000；twstock 備援僅涵蓋上市。
'''
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 專案根目錄
from db_writer import upsert_rows  # noqa: E402

load_dotenv()

COLUMNS = ['Open', 'High', 'Low', 'Close', 'Volume']
FINMIND_URL = 'https://api.finmindtrade.com/api/v4/data'


def normalize_ticker(ticker: str) -> list:
    '''台股純數字代號 → 依序試 .TW（上市）、.TWO（上櫃）；其餘視為美股原樣。'''
    t = ticker.strip().upper()
    if t.replace('.', '').isdigit() and not t.endswith(('.TW', '.TWO')):
        return [t + '.TW', t + '.TWO']
    return [t]


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):  # 新版 yfinance 單檔也會回多層欄位
        df.columns = df.columns.get_level_values(0)
    df = df[[c for c in COLUMNS if c in df.columns]].copy()
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df.index.name = 'Date'
    return df.dropna(subset=['Close'])


def _from_twstock(code: str, days: int) -> pd.DataFrame:
    '''備援：證交所原始價（無還原價可選）。一個月打一次 API，天數多會比較慢。'''
    import twstock

    start = datetime.today() - timedelta(days=days)
    raw = twstock.Stock(code).fetch_from(start.year, start.month)
    df = pd.DataFrame({
        'Open': [r.open for r in raw],
        'High': [r.high for r in raw],
        'Low': [r.low for r in raw],
        'Close': [r.close for r in raw],
        'Volume': [r.capacity for r in raw],  # 單位：股
    }, index=pd.to_datetime([r.date for r in raw]).normalize())
    df.index.name = 'Date'
    return df.dropna(how='all')


def _from_finmind(code: str, days: int, adjust: bool = False) -> pd.DataFrame:
    '''台股主力來源：FinMind（官方日線，當天約 15:00–16:00 更新，含上櫃與 ETF）。'''
    start = (datetime.today() - timedelta(days=days)).strftime('%Y-%m-%d')
    r = requests.get(FINMIND_URL, params={
        'dataset': 'TaiwanStockPriceAdj' if adjust else 'TaiwanStockPrice',
        'data_id': code,
        'start_date': start,
        'token': os.getenv('FINMIND_TOKEN', ''),
    }, timeout=30)
    data = r.json().get('data', [])
    if not data:
        raise RuntimeError(f'FinMind 無 {code} 資料')
    df = pd.DataFrame(data).rename(columns={
        'open': 'Open', 'max': 'High', 'min': 'Low',
        'close': 'Close', 'Trading_Volume': 'Volume',  # 單位：股，與 yfinance 口徑一致
    })
    df.index = pd.to_datetime(df['date']).dt.normalize()  # Series 要經 .dt 才能 normalize
    df.index.name = 'Date'
    return df[COLUMNS].dropna(subset=['Close'])


def get_price(ticker: str, days: int = 365, adjust: bool = False,
              market: str | None = None) -> pd.DataFrame:
    '''回傳 DatetimeIndex ＋ Open/High/Low/Close/Volume 的 DataFrame。'''
    code = ticker.strip().upper()
    # 有給 market 就以它為準（00403A 這種含字母的台股代號不能用 isdigit 猜）
    is_tw = (market == '台股') if market else code.replace('.', '').isdigit()

    if is_tw:  # 台股主力：FinMind
        try:
            df = _from_finmind(code.replace('.TW', '').replace('.TWO', ''), days, adjust)
            if not df.empty:
                df.attrs['symbol'] = code + ' (FinMind)'
                return df
        except Exception:
            pass  # 落到下面 yfinance → twstock 備援

    start = (datetime.today() - timedelta(days=days)).strftime('%Y-%m-%d')
    last_err = None
    for sym in normalize_ticker(ticker):
        try:
            df = _clean(yf.download(sym, start=start, auto_adjust=adjust, progress=False))
            if not df.empty:
                df.attrs['symbol'] = sym
                return df
        except Exception as e:
            last_err = e
    if is_tw:  # 台股最後備援（僅上市）
        df = _from_twstock(code, days)
        if not df.empty:
            df.attrs['symbol'] = code + ' (twstock)'
            return df
    raise RuntimeError(f'抓不到 {ticker} 的價量資料（最後錯誤：{last_err}）')


def to_daily_prices(ticker: str, df: pd.DataFrame) -> pd.DataFrame:
    '''get_price() 回傳 yfinance 欄名（Open/High/Low/Close/Volume、日期在 index），
    先整形成 daily_prices 表格式（symbol＋date＋小寫欄名）再寫入。'''
    out = df.reset_index().rename(columns={
        'Date': 'date', 'Open': 'open_price', 'High': 'high',
        'Low': 'low', 'Close': 'close_price', 'Volume': 'volume',
    })
    out['symbol'] = ticker
    out['date'] = out['date'].dt.strftime('%Y-%m-%d')
    return out[['symbol', 'date', 'open_price', 'high', 'low', 'close_price', 'volume']]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--ticker', required=True)
    ap.add_argument('--days', type=int, default=365)
    ap.add_argument('--adjust', action='store_true', help='還原價（回測用）')
    ap.add_argument('--csv', action='store_true', help='另存 CSV 快取')
    args = ap.parse_args()

    df = get_price(args.ticker, days=args.days, adjust=args.adjust)
    upsert_rows('daily_prices', to_daily_prices(args.ticker, df))  # 雙寫本機 SQL Server（D16）
    sym = df.attrs.get('symbol', args.ticker)
    print(f'{sym}｜{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}｜共 {len(df)} 根')
    print(df.tail())
    if args.csv:
        path = f'price_{args.ticker}.csv'
        df.to_csv(path, encoding='utf-8-sig')
        print('已存 ' + path)


if __name__ == '__main__':
    main()
