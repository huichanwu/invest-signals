# -*- coding: utf-8 -*-
r'''plot_price_chart.py — D12 選配｜K 線＋MA5–120＋成交量（台式紅漲綠跌）

用法：
    python fetch\plot_price_chart.py --ticker 3231           # 近 120 根 → chart4_3231.png
    python fetch\plot_price_chart.py --ticker 3231 --show    # 開視窗直接看
需另裝：mplfinance
'''
import argparse
import sys
from pathlib import Path

import mplfinance as mpf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fetch.fetch_price import get_price  # noqa: E402
from transform.indicators import add_ma  # noqa: E402

TW_STYLE = mpf.make_mpf_style(
    base_mpf_style='yahoo',
    marketcolors=mpf.make_marketcolors(
        up='red', down='green', edge='inherit', wick='inherit', volume='inherit'
    ),
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--ticker', required=True)
    ap.add_argument('--bars', type=int, default=120, help='顯示最近幾根 K 棒')
    ap.add_argument('--show', action='store_true', help='開視窗（預設存 PNG）')
    args = ap.parse_args()

    df = add_ma(get_price(args.ticker, days=max(args.bars * 3, 600)))  # 多抓讓 MA120 有值
    sym = df.attrs.get('symbol', args.ticker)
    view = df.tail(args.bars)
    mas = [mpf.make_addplot(view[f'MA{p}'], width=1) for p in (5, 10, 20, 60, 120)]

    kwargs = dict(type='candle', volume=True, style=TW_STYLE, addplot=mas,
                  title=str(sym), figsize=(12, 7))
    if args.show:
        mpf.plot(view, **kwargs)
    else:
        out = f'chart4_{args.ticker}.png'
        mpf.plot(view, **kwargs, savefig=out)
        print('已存 ' + out)


if __name__ == '__main__':
    main()
