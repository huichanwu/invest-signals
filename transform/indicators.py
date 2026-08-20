# -*- coding: utf-8 -*-
r'''indicators.py — 技術指標共用模組（台美通用）

D12：MA5–120＋均線排列＋跌破判定（本檔）；D13 加 KD/MACD/RSI；D14 加 BIAS/BOLL/量能。

自我測試（現抓資料，不經任何資料庫）：
    python transform\indicators.py --ticker 3231
    python transform\indicators.py --ticker 3231 --date 2026-04-02   # 對帳指定日
    python transform\indicators.py --ticker ASML                     # 美股共用同一套
'''
import argparse
import sys
from pathlib import Path

import pandas as pd

MA_PERIODS = (5, 10, 20, 30, 60, 120)
VOL_RATIO = 1.5  # 「帶量」門檻：今量 > 昨日止的 5 日均量 × 1.5


def add_ma(df: pd.DataFrame, periods=MA_PERIODS) -> pd.DataFrame:
    '''收盤價簡單移動平均 MA5/10/20/30/60/120。'''
    for p in periods:
        df[f'MA{p}'] = df['Close'].rolling(p).mean()
    return df


def add_ma_signals(df: pd.DataFrame, vol_ratio: float = VOL_RATIO) -> pd.DataFrame:
    '''Step 5 的判定欄位（依骨架規格）：

    多頭排列       ＝ MA5 > MA10 > MA20 > MA60
    帶量跌破MA10   ＝ 昨收 ≥ 昨MA10、今收 < 今MA10（今天才破），且今量 > 5日均量×1.5
    跌破MA10站不回 ＝ 連 2 日收盤 < MA10
    '''
    below = df['Close'] < df['MA10']
    prev_above = df['Close'].shift(1) >= df['MA10'].shift(1)
    prev_below = df['Close'].shift(1) < df['MA10'].shift(1)
    vol_ma5_prev = df['Volume'].rolling(5).mean().shift(1)

    df['多頭排列'] = (df['MA5'] > df['MA10']) & (df['MA10'] > df['MA20']) & (df['MA20'] > df['MA60'])
    df['帶量跌破MA10'] = below & prev_above & (df['Volume'] > vol_ma5_prev * vol_ratio)
    df['跌破MA10站不回'] = below & prev_below
    df['MA20乖離%'] = (df['Close'] / df['MA20'] - 1) * 100
    return df

def add_kd(df: pd.DataFrame, n: int = 9, sk: int = 3, sd: int = 3) -> pd.DataFrame:
    '''台股平滑版 KD（9,3,3）：K／D 初始值 50，K=蔲K'+⅓RSV，D=蔲D'+⅓K。'''
    llv = df['Low'].rolling(n).min()
    hhv = df['High'].rolling(n).max()
    rsv = ((df['Close'] - llv) / (hhv - llv) * 100).fillna(50)
    k_list, d_list = [], []
    k, d = 50.0, 50.0
    for v in rsv:
        k = k * (sk - 1) / sk + v / sk
        d = d * (sd - 1) / sd + k / sd
        k_list.append(k)
        d_list.append(d)
    df['K'], df['D'] = k_list, d_list
    return df


def add_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    '''MACD（12,26,9）：DIF＝EMA12−EMA26，DEA＝DIF 的 EMA9，柱＝DIF−DEA。'''
    ema_fast = df['Close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['Close'].ewm(span=slow, adjust=False).mean()
    df['DIF'] = ema_fast - ema_slow
    df['DEA'] = df['DIF'].ewm(span=signal, adjust=False).mean()
    df['MACD柱'] = df['DIF'] - df['DEA']
    return df


def add_rsi(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    '''RSI（14）：Wilder 平滑（ewm alpha=1/14）。'''
    delta = df['Close'].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    df['RSI14'] = 100 - 100 / (1 + gain / loss)
    return df

def add_momentum_signals(df: pd.DataFrame) -> pd.DataFrame:
    '''Step 4 的動能判定欄位（依骨架規格）：

    低檔KD金叉   ＝ K 由下往上穿越 D，且交叉當天 K < 50
    紅柱縮短     ＝ MACD 柱 > 0 且連 2 根遞減（多方動能退潮）
    價高DIF黏合  ＝ 收盤創 60 日新高，但 |DIF−DEA| 比 5 日前縮一半以上（高檔背離警示）
    '''
    cross_up = (df['K'] > df['D']) & (df['K'].shift(1) <= df['D'].shift(1))
    df['低檔KD金叉'] = cross_up & (df['K'] < 50)

    hist = df['MACD柱']
    df['紅柱縮短'] = (hist > 0) & (hist < hist.shift(1)) & (hist.shift(1) < hist.shift(2))

    spread = (df['DIF'] - df['DEA']).abs()
    new_high = df['Close'] >= df['Close'].rolling(60).max()
    df['價高DIF黏合'] = new_high & (spread < spread.shift(5) * 0.5)
    return df   

def enrich(df: pd.DataFrame) -> pd.DataFrame:
    '''一站式：MA＋KD＋MACD＋RSI＋BIAS＋BOLL＋量能＋所有判定。'''
    df = add_ma_signals(add_ma(df))
    df = add_momentum_signals(add_rsi(add_macd(add_kd(df))))
    df = add_overheat_signals(add_volume_ma(add_boll(add_bias(df))))
    return df


def report(df: pd.DataFrame, when=None) -> str:
    '''印某一日（預設最新一日）的均線與判定，對帳用。'''
    day = df if when is None else df[df.index == pd.Timestamp(when)]
    if day.empty:
        return f'{when} 不是交易日或不在抓取範圍內（可加大 --days）'
    row, date = day.iloc[-1], day.index[-1]
    close = row['Close']
    bias = row['MA20乖離%']
    mas = []
    for p in MA_PERIODS:
        v = row.get(f'MA{p}')
        if pd.notna(v):
            mas.append(f'MA{p} {v:.2f}')
    flag1 = '✅' if row['多頭排列'] else '❌'
    flag2 = '🚨' if row['帶量跌破MA10'] else '—'
    flag3 = '🚨' if row['跌破MA10站不回'] else '—'
    flag4 = '🟢' if row['低檔KD金叉'] else '—'
    flag5 = '🟠' if row['紅柱縮短'] else '—'
    flag6 = '🚨' if row['價高DIF黏合'] else '—'
    flag7 = '🔥' if row['BIAS過熱'] else '—'
    flag8 = '🔥' if row['觸上軌過熱'] else '—'
    flag9 = '🟠' if row['量增價不漲'] else '—'
    flag10 = '🟠' if row['價漲量縮背離'] else '—'
    boll_state = '開口' if row['BOLL開口'] else ('收口' if row['BOLL收口'] else '—')
    return (
        f'{date:%Y-%m-%d}｜收盤 {close:.2f}\n'
        + '｜'.join(mas)
        + f"\nK {row['K']:.1f}｜D {row['D']:.1f}｜RSI {row['RSI14']:.1f}｜"
        + f"DIF {row['DIF']:.2f}｜DEA {row['DEA']:.2f}｜柱 {row['MACD柱']:+.2f}\n"
        + f'多頭排列 {flag1}｜帶量跌破MA10 {flag2}｜站不回 {flag3}｜MA20乖離 {bias:+.1f}%\n'
        + f'低檔KD金叉 {flag4}｜紅柱縮短 {flag5}｜價高DIF黏合 {flag6}'
        + f"\nBIAS20 {row['BIAS20']:+.1f}%｜BIAS60 {row['BIAS60']:+.1f}%｜"
        + f"帶寬 {row['BOLL帶寬%']:.1f}%（{boll_state}）｜量/MV20 {row['Volume'] / row['MV20']:.2f}x\n"
        + f'BIAS過熱 {flag7}｜觸上軌過熱 {flag8}｜量增價不漲 {flag9}｜價漲量縮 {flag10}'
    )

def add_bias(df: pd.DataFrame, windows: tuple = (20, 60)) -> pd.DataFrame:
    '''BIAS 乖離率（％）：BIASn = (收盤 − MAn) / MAn × 100。預設算 20／60 兩組。'''
    for n in windows:
        ma = df['Close'].rolling(n).mean()
        df[f'BIAS{n}'] = (df['Close'] - ma) / ma * 100
    return df


def add_boll(df: pd.DataFrame, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    '''BOLL 布林通道（20, 2）：中軌＝MA20，上下軌＝中軌 ± 2σ（母體標準差 ddof=0）。
    帶寬％＝(上軌 − 下軌) / 中軌 × 100；開口＝帶寬連 3 根遞增，收口＝連 3 根遞減。'''
    mid = df['Close'].rolling(n).mean()
    std = df['Close'].rolling(n).std(ddof=0)
    df['BOLL上軌'] = mid + k * std
    df['BOLL中軌'] = mid
    df['BOLL下軌'] = mid - k * std
    df['BOLL帶寬%'] = (df['BOLL上軌'] - df['BOLL下軌']) / mid * 100
    bw = df['BOLL帶寬%']
    df['BOLL開口'] = (bw > bw.shift(1)) & (bw.shift(1) > bw.shift(2)) & (bw.shift(2) > bw.shift(3))
    df['BOLL收口'] = (bw < bw.shift(1)) & (bw.shift(1) < bw.shift(2)) & (bw.shift(2) < bw.shift(3))
    return df


def add_volume_ma(df: pd.DataFrame) -> pd.DataFrame:
    '''量能均線：MV5／MV20（成交量的 5 日／20 日均量）。'''
    df['MV5'] = df['Volume'].rolling(5).mean()
    df['MV20'] = df['Volume'].rolling(20).mean()
    return df

def add_overheat_signals(df: pd.DataFrame, pct_window: int = 250, pct: float = 0.90) -> pd.DataFrame:
    '''Step 4 的過熱與背離判定（門檻自適應，台美市場不共用同一組數字）：

    BIAS過熱     ＝ BIAS20 > 自身近 250 日的 90 百分位，且 BIAS20 > 0
    觸上軌過熱   ＝ 收盤 ≥ BOLL上軌 且 BOLL開口（沿上軌走的強勢過熱，收口觸軌不算）
    量增價不漲   ＝ 成交量 > MV20 × 1.5 且 當日漲幅 < 1%（出貨警訊）
    價漲量縮背離 ＝ 收盤創 20 日新高 但 成交量 < MV5（攻擊無量）
    '''
    hot_line = df['BIAS20'].rolling(pct_window, min_periods=60).quantile(pct)
    df['BIAS過熱'] = (df['BIAS20'] > hot_line) & (df['BIAS20'] > 0)

    df['觸上軌過熱'] = (df['Close'] >= df['BOLL上軌']) & df['BOLL開口']

    chg = df['Close'].pct_change() * 100
    df['量增價不漲'] = (df['Volume'] > df['MV20'] * 1.5) & (chg < 1)

    new_high20 = df['Close'] >= df['Close'].rolling(20).max()
    df['價漲量縮背離'] = new_high20 & (df['Volume'] < df['MV5'])
    return df

INDICATOR_COLS = ['ma5', 'ma10', 'ma20', 'ma60', 'ma120', 'k', 'd', 'macd', 'rsi', 'bias',
                  'boll_upper', 'boll_lower', 'mv5', 'mv20']


def to_indicators(ticker: str, df: pd.DataFrame) -> pd.DataFrame:
    '''enrich() 回傳大寫／中文欄名，先整形成 indicators 表格式（symbol＋date＋小寫欄名）再寫入。
    對應：macd＝MACD柱（DIF−DEA）、rsi＝RSI14、bias＝BIAS20、boll_upper/lower＝BOLL上軌/下軌。'''
    df_ind = df.reset_index().rename(columns={
        'Date': 'date',
        'MA5': 'ma5', 'MA10': 'ma10', 'MA20': 'ma20', 'MA60': 'ma60', 'MA120': 'ma120',
        'K': 'k', 'D': 'd', 'MACD柱': 'macd', 'RSI14': 'rsi', 'BIAS20': 'bias',
        'BOLL上軌': 'boll_upper', 'BOLL下軌': 'boll_lower', 'MV5': 'mv5', 'MV20': 'mv20',
    })
    df_ind['symbol'] = ticker
    df_ind['date'] = df_ind['date'].dt.strftime('%Y-%m-%d')
    return df_ind[['symbol', 'date'] + INDICATOR_COLS]


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 專案根目錄
    from fetch.fetch_price import get_price  # ← 補這行
    from db_writer import upsert_rows  # 雙寫本機 SQL Server（D16）
    
    ap = argparse.ArgumentParser()
    ap.add_argument('--ticker', required=True)
    ap.add_argument('--days', type=int, default=365)
    ap.add_argument('--date', help='對帳用：印指定日（YYYY-MM-DD）')
    ap.add_argument('--adjust', action='store_true', help='還原價（回測用）')
    args = ap.parse_args()

    df = enrich(get_price(args.ticker, days=args.days, adjust=args.adjust))
    upsert_rows('indicators', to_indicators(args.ticker, df))  # 雙寫本機 SQL Server（D16）
    print(report(df, args.date))
    print('（對帳提醒：MA 值要對「同一天、同價格基準」的看盤軟體游標值，誤差 < 0.1）')


if __name__ == '__main__':
    main()