# -*- coding: utf-8 -*-
r"""plot_stock_charts.py — 互動版（Plotly HTML）組合圖×5 嵌入 Notion

v3（2026/08/21 D19.5）：BOLL 疊進圖①＋新增圖⑤動能副圖（KD/RSI＋MACD）
  - 指標改用 transform\indicators.py 的 enrich() 一次算完（MA＋BOLL＋KD/RSI/MACD），
    chart4 回傳 enriched df 給圖⑤重用，不重抓價、不重算
  - 順序 ①⑤②③④；台股每檔 5 張、美股每檔 2 張（①⑤台美共用）
v2（2026/08/13 改版）：matplotlib PNG → Plotly 互動 HTML
  - 滑鼠移到圖上浮出當日全部數值（x unified）、滾輪縮放、雙擊還原
  - 新增圖④：K 線×MA5–120×量能（紅漲綠跌；yfinance 現抓原始價，
    MA 用 transform\indicators.py 同一套，D12 chart4 就此併入）
  - X 軸每 7 個日曆天標一個 %m/%d，非交易日 rangebreaks 跳掉不留洞
  - 不再輸出 PNG；HTML 經 File Upload API 上傳，embed.file_upload
    變成 Notion 的 HTML 區塊（沙盒 iframe 內互動）

圖 ①：K 線＋MA5–120＋BOLL(20,2)＋成交量（8/21 疊上 BOLL）
圖 ⑤：動能副圖——上 KD(9,3,3)×RSI14、下 MACD(12,26,9)（8/21 新增，台美共用）
圖 ②：股價（右軸）× 融資／融券／借券賣出餘額（左軸，張）
圖 ③：股價（右軸）× 外資＋投信＋自營買賣超正負堆疊柱（左軸，張／日）
圖 ④：股價（右軸）× 外資／投信(推估)／自營(推估)持股＋千張／400張大戶（左軸，%）

用法：
    python fetch\plot_stock_charts.py                        # 台股清單全部檔（近 90 天）
    python fetch\plot_stock_charts.py --ticker 2330          # 臨時只畫一檔（頁面暫時只剩這檔）
    python fetch\plot_stock_charts.py --ticker 3231 --local  # 只存本機 HTML 不動 Notion（預覽）
    python fetch\plot_stock_charts.py --days 60

.env 與排程全部沿用 PNG 版：NOTION_TOKEN、NOTION_DATABASE_ID、
NOTION_DB_TW_STOCK_CHIPS、NOTION_PAGE_CHARTS；每日 21:40（fetch_tdcc.py 之後）。
需另裝：uv add plotly
"""

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
from dotenv import load_dotenv
from notion_client import Client
from plotly.subplots import make_subplots

NOTION_VERSION = "2025-09-03"  # 若 embed/file_upload 驗證錯誤：升級 notion-client 並調高此值
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE_DIR))  # 專案根目錄，匯入 fetch_price / indicators
from fetch.fetch_price import get_price  # noqa: E402
from transform.indicators import enrich  # noqa: E402

OUT_DIR = os.path.join(BASE_DIR, "charts")
LOG_FILE = os.path.join(BASE_DIR, "plot_stock_charts.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("plot")

MA_COLORS = ((5, "#1f77b4"), (10, "#ff7f0e"), (20, "#7b52ab"), (60, "#0e8f8f"), (120, "#8c564b"))


# ---------- Notion 讀取（與 PNG 版相同） ----------

def get_data_source_id(notion, db_id):
    db = notion.databases.retrieve(db_id)
    sources = db.get("data_sources") or []
    if not sources:
        raise RuntimeError("資料庫沒有 data source，請確認已分享給 integration")
    return sources[0]["id"]


def fetch_watchlist(notion):
    """從 💹 個股現價 撈台股＋美股清單（8/15 起含美股，美股只畫 K 線圖）"""
    ds_id = get_data_source_id(notion, os.environ["NOTION_DATABASE_ID"])
    flt = {"or": [dict(property="國家", select=dict(equals="台股")),
                  dict(property="國家", select=dict(equals="美股"))]}
    items = []
    cursor = None
    while True:
        kwargs = dict(data_source_id=ds_id, page_size=100, filter=flt)
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.data_sources.query(**kwargs)
        for page in resp.get("results") or []:
            props = page.get("properties") or {}
            title = (props.get("股票代號") or {}).get("title") or []
            code = "".join(x.get("plain_text", "") for x in title).strip()
            name_arr = (props.get("名稱") or {}).get("rich_text") or []
            name = "".join(x.get("plain_text", "") for x in name_arr).strip()
            market = ((props.get("國家") or {}).get("select") or {}).get("name") or "台股"
            if code and code not in ("TWD=X",):
                items.append(dict(code=code, name=name, market=market))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    seen, out = set(), []
    for it in sorted(items, key=lambda x: (x["market"], x["code"])):
        if it["code"] not in seen:
            seen.add(it["code"])
            out.append(it)
    return out


def query_rows(notion, ds_id, ticker, days):
    since = (date.today() - timedelta(days=days)).isoformat()
    f_ticker = dict(property="代號", rich_text=dict(equals=ticker))
    f_date = dict(property="資料日期", date=dict(on_or_after=since))
    flt = {"and": [f_ticker, f_date]}
    sorts = [dict(property="資料日期", direction="ascending")]
    results = []
    cursor = None
    while True:
        kwargs = dict(data_source_id=ds_id, page_size=100, filter=flt, sorts=sorts)
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.data_sources.query(**kwargs)
        results.extend(resp.get("results") or [])
        if not resp.get("has_more"):
            return results
        cursor = resp.get("next_cursor")


def num(props, name):
    return (props.get(name) or {}).get("number")


def text(props, name):
    arr_ = (props.get(name) or {}).get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in arr_).strip()


def extract(rows):
    data = dict(dates=[], close=[], margin=[], short=[], sbl=[],
                foreign=[], trust=[], dealer=[],
                qfiis=[], trust_pct=[], dealer_pct=[],
                p1000=[], p400=[], name="")
    for row in rows:
        props = row.get("properties") or {}
        d = ((props.get("資料日期") or {}).get("date") or {}).get("start")
        if not d:
            continue
        data["dates"].append(d[:10])  # YYYY-MM-DD（Plotly 用真日期軸）
        data["close"].append(num(props, "收盤價"))
        data["margin"].append(num(props, "融資餘額"))
        data["short"].append(num(props, "融券餘額"))
        data["sbl"].append(num(props, "借券賣出餘額"))
        data["foreign"].append(num(props, "外資買賣超"))
        data["trust"].append(num(props, "投信買賣超"))
        data["dealer"].append(num(props, "自營買賣超"))
        data["qfiis"].append(num(props, "外資持股比率"))
        data["trust_pct"].append(num(props, "投信持股比率(推估)"))
        data["dealer_pct"].append(num(props, "自營持股比率(推估)"))
        data["p1000"].append(num(props, "千張大戶比率"))
        data["p400"].append(num(props, "400張大戶比率"))
        n = text(props, "名稱")
        if n:
            data["name"] = n
    return data


# ---------- 共用軸線／樣式 ----------

def week_ticks(dates):
    """每 7 個日曆天挑一個實際交易日當刻度（回傳日期字串清單）"""
    ticks, last = [], None
    for d in dates:
        dd = pd.Timestamp(d)
        if last is None or (dd - last).days >= 7:
            ticks.append(str(d)[:10])
            last = dd
    return ticks


def missing_days(dates):
    """日曆天 − 交易日 ＝ rangebreaks 要跳掉的日子（週末＋假日）"""
    idx = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    all_days = pd.date_range(idx.min(), idx.max())
    return [d.strftime("%Y-%m-%d") for d in all_days.difference(idx)]


def style_xaxis(fig, dates, **kw):
    fig.update_xaxes(
        type="date",
        tickvals=week_ticks(dates),
        tickformat="%m/%d",
        hoverformat="%Y/%m/%d",
        rangebreaks=[dict(values=missing_days(dates))],
        **kw,
    )


def base_fig(title):
    fig = make_subplots(specs=[[dict(secondary_y=True)]])
    fig.update_layout(
        title=title,
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=60, r=60, t=80, b=40),
    )
    return fig


PLOTLY_JS_URL = "https://cdn.plot.ly/plotly-finance-3.4.0.min.js"  # finance 精簡引擎（scatter/bar/candlestick）
PLOTLY_JS_CACHE = os.path.join(BASE_DIR, "plotly-finance.min.js")
_PLOTLY_B64 = None


def plotly_js_b64():
    """引擎的 base64：首次自動下載快取，之後離線重用（防火牆看到的只是文字）"""
    global _PLOTLY_B64
    if _PLOTLY_B64 is None:
        import base64
        if not os.path.exists(PLOTLY_JS_CACHE):
            log.info("首次執行：下載 plotly.js finance 精簡引擎（約 1.2MB）…")
            r = requests.get(PLOTLY_JS_URL, timeout=120)
            r.raise_for_status()
            with open(PLOTLY_JS_CACHE, "wb") as f:
                f.write(r.content)
        with open(PLOTLY_JS_CACHE, "rb") as f:
            _PLOTLY_B64 = base64.b64encode(f.read()).decode("ascii")
    return _PLOTLY_B64


LOADER = ('<script>var b64="%s";var bin=atob(b64);var arr=new Uint8Array(bin.length);'
          'for(var i=0;i<bin.length;i++)arr[i]=bin.charCodeAt(i);'
          'var s=document.createElement("script");'
          's.textContent=new TextDecoder("utf-8").decode(arr);'
          'document.head.appendChild(s);</script>')


def save_html(fig, path, height=560):
    html = fig.to_html(
        include_plotlyjs=False,  # 引擎用下面的 base64 夾帶（沙盒擋 CDN；JS 原始碼直接內嵌會被防火牆擋）
        full_html=True,
        default_width="100%",
        default_height="%dpx" % height,  # 固定像素高；"100%" 在 iframe 內會塌成 0 → 一片空白
        config=dict(responsive=True, displaylogo=False,
                    modeBarButtonsToRemove=["lasso2d", "select2d"]),
    )
    html = html.replace("</head>", (LOADER % plotly_js_b64()) + "</head>", 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ---------- 四張圖 ----------

def chart1(data, ticker):
    fig = base_fig("%s %s｜股價 × 融資／融券／借券" % (ticker, data["name"]))
    x = data["dates"]
    fig.add_trace(go.Scatter(x=x, y=data["margin"], name="融資餘額（張）", line=dict(color="#1f77b4")))
    fig.add_trace(go.Scatter(x=x, y=data["short"], name="融券餘額（張）", line=dict(color="#ff7f0e")))
    fig.add_trace(go.Scatter(x=x, y=data["sbl"], name="借券賣出餘額（張）", line=dict(color="#2ca02c")))
    fig.add_trace(go.Scatter(x=x, y=data["close"], name="收盤價（元）",
                             line=dict(color="#d62728", width=2.4)), secondary_y=True)
    fig.update_yaxes(title_text="張", hoverformat=",.0f", secondary_y=False)
    fig.update_yaxes(title_text="元", hoverformat=",.2f", showgrid=False, secondary_y=True)
    style_xaxis(fig, x)
    return fig


def chart2(data, ticker):
    fig = base_fig("%s %s｜股價 × 三大法人買賣超（堆疊）" % (ticker, data["name"]))
    x = data["dates"]
    for label, key, color in (("外資買賣超", "foreign", "#0e8f8f"),
                              ("投信買賣超", "trust", "#7b52ab"),
                              ("自營買賣超", "dealer", "#d4589e")):
        vals = [v if v is not None else 0 for v in data[key]]
        fig.add_trace(go.Bar(x=x, y=vals, name=label, marker_color=color))
    fig.add_trace(go.Scatter(x=x, y=data["close"], name="收盤價（元）",
                             line=dict(color="#d62728", width=2.4)), secondary_y=True)
    fig.update_layout(barmode="relative")  # 正值往上疊、負值往下疊
    fig.update_yaxes(title_text="買賣超（張／日）", hoverformat="+,.0f", secondary_y=False)
    fig.update_yaxes(title_text="元", hoverformat=",.2f", showgrid=False, secondary_y=True)
    style_xaxis(fig, x)
    return fig


def chart3(data, ticker):
    fig = base_fig("%s %s｜持股比例：外資／投信／自營（推估）／大戶 × 股價" % (ticker, data["name"]))
    x = data["dates"]
    fig.add_trace(go.Scatter(x=x, y=data["qfiis"], name="外資持股比率", line=dict(color="#444444", width=2)))
    fig.add_trace(go.Scatter(x=x, y=data["trust_pct"], name="投信持股比率（推估）", line=dict(color="#7b52ab")))
    fig.add_trace(go.Scatter(x=x, y=data["dealer_pct"], name="自營持股比率（推估）", line=dict(color="#d4589e")))
    fig.add_trace(go.Scatter(x=x, y=data["p1000"], name="千張大戶比率",
                             line=dict(color="#1f77b4", dash="dash"), connectgaps=True))
    fig.add_trace(go.Scatter(x=x, y=data["p400"], name="400張大戶比率",
                             line=dict(color="#2ca02c", dash="dash"), connectgaps=True))
    fig.add_trace(go.Scatter(x=x, y=data["close"], name="收盤價（元）",
                             line=dict(color="#d62728", width=2.4)), secondary_y=True)
    fig.update_yaxes(title_text="持股比率（%）", hoverformat=".2f", secondary_y=False)
    fig.update_yaxes(title_text="元", hoverformat=",.2f", showgrid=False, secondary_y=True)
    style_xaxis(fig, x)
    return fig


def chart4(ticker, days, market="台股"):
    """① K 線＋MA＋BOLL＋量能：現抓原始價，指標用 transform\indicators.py 的 enrich()。
    回傳 (fig, 筆數, 視窗內 enriched df)——df 交給 chart5 動能副圖重用，不重抓價、不重算。"""
    df = enrich(get_price(ticker, days=days + 300, market=market))  # 多抓讓第一根就有 MA120／BOLL／KD 暖機
    df = df[df.index >= (pd.Timestamp.today().normalize() - pd.Timedelta(days=days))]
    if df.empty:
        return None, 0, None
    is_tw = (market == "台股")
    sym = df.attrs.get("symbol", ticker)
    x = [d.strftime("%Y-%m-%d") for d in df.index]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.72, 0.28], vertical_spacing=0.05)
    # ── D19.5：BOLL(20,2) 先畫（墊在 K 棒下層），上下軌之間半透明填色；中軌＝MA20 不重複畫 ──
    fig.add_trace(go.Scatter(x=x, y=df["BOLL下軌"], name="BOLL下軌",
                             line=dict(width=1, color="#999999", dash="dot"),
                             showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=df["BOLL上軌"], name="BOLL(20,2)",
                             line=dict(width=1, color="#999999", dash="dot"),
                             fill="tonexty", fillcolor="rgba(150,150,150,0.15)"), row=1, col=1)
    fig.add_trace(go.Candlestick(
        x=x, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
        name="K線", increasing_line_color="#d62728", increasing_fillcolor="#d62728",
        decreasing_line_color="#2ca02c", decreasing_fillcolor="#2ca02c",
    ), row=1, col=1)
    for p, color in MA_COLORS:
        fig.add_trace(go.Scatter(x=x, y=df["MA%d" % p], name="MA%d" % p,
                                 line=dict(width=1.2, color=color)), row=1, col=1)
    vol_colors = ["#d62728" if c >= o else "#2ca02c" for c, o in zip(df["Close"], df["Open"])]
    vol = df["Volume"] / 1000 if is_tw else df["Volume"]
    vol_unit = "張" if is_tw else "股"
    fig.add_trace(go.Bar(x=x, y=vol, name="成交量（%s）" % vol_unit,
                         marker_color=vol_colors), row=2, col=1)
    fig.update_layout(
        title="%s｜K 線 × MA5–120 × BOLL × 量能" % sym,
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=60, r=30, t=80, b=40),
        xaxis_rangeslider_visible=False,
    )
    fig.update_yaxes(title_text="元" if is_tw else "美元", hoverformat=",.2f", row=1, col=1)
    fig.update_yaxes(title_text=vol_unit, hoverformat=",.0f", row=2, col=1)
    style_xaxis(fig, x, row=1, col=1)
    style_xaxis(fig, x, row=2, col=1)
    return fig, len(df), df


def chart5(df, ticker, name=""):
    """⑤ 動能副圖：上＝KD＋RSI14（0–100 同刻度，20/50/80 虛線）；下＝MACD（DIF/DEA＋紅綠柱）。
    df 直接吃 chart4 回傳的 enriched df，台美共用（D19.5 新增）。"""
    x = [d.strftime("%Y-%m-%d") for d in df.index]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.5, 0.5], vertical_spacing=0.08,
                        subplot_titles=("KD（9,3,3）× RSI14", "MACD（12,26,9）"))
    fig.add_trace(go.Scatter(x=x, y=df["K"], name="K", line=dict(color="#1f77b4", width=1.4)), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=df["D"], name="D", line=dict(color="#ff7f0e", width=1.4)), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=df["RSI14"], name="RSI14", line=dict(color="#7b52ab", width=1.4)), row=1, col=1)
    for lv in (20, 50, 80):
        fig.add_hline(y=lv, line=dict(color="#bbbbbb", width=0.8, dash="dash"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=df["DIF"], name="DIF", line=dict(color="#1f77b4", width=1.4)), row=2, col=1)
    fig.add_trace(go.Scatter(x=x, y=df["DEA"], name="DEA", line=dict(color="#ff7f0e", width=1.4)), row=2, col=1)
    hist = df["MACD柱"].fillna(0)
    bar_colors = ["#d62728" if v >= 0 else "#2ca02c" for v in hist]  # 柱>0 紅、<0 綠（目測「紅柱縮短」用）
    fig.add_trace(go.Bar(x=x, y=df["MACD柱"], name="MACD柱", marker_color=bar_colors), row=2, col=1)
    fig.add_hline(y=0, line=dict(color="#888888", width=0.8), row=2, col=1)
    fig.update_layout(
        title="%s %s｜動能指標：KD × RSI × MACD" % (ticker, name),
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=60, r=30, t=80, b=40),
    )
    fig.update_yaxes(range=[0, 100], hoverformat=".1f", row=1, col=1)
    fig.update_yaxes(hoverformat="+.2f", row=2, col=1)
    style_xaxis(fig, x, row=1, col=1)
    style_xaxis(fig, x, row=2, col=1)
    return fig


# ---------- 上傳與寫回 Notion ----------

def upload_file(token, path, content_type):
    headers = {"Authorization": "Bearer %s" % token, "Notion-Version": NOTION_VERSION}
    payload = dict(mode="single_part", filename=os.path.basename(path), content_type=content_type)
    r = requests.post("https://api.notion.com/v1/file_uploads", headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    fid = r.json()["id"]
    with open(path, "rb") as f:
        files = dict(file=(os.path.basename(path), f, content_type))
        r2 = requests.post("https://api.notion.com/v1/file_uploads/%s/send" % fid,
                           headers=headers, files=files, timeout=120)
    if r2.status_code >= 400:
        log.error("上傳 %s 失敗（HTTP %s）：%s", os.path.basename(path), r2.status_code, r2.text[:300])
    r2.raise_for_status()
    return fid


def append_children(token, block_id, children):
    headers = {"Authorization": "Bearer %s" % token, "Notion-Version": NOTION_VERSION,
               "Content-Type": "application/json"}
    r = requests.patch("https://api.notion.com/v1/blocks/%s/children" % block_id,
                       headers=headers, json=dict(children=children), timeout=60)
    r.raise_for_status()


def clear_charts_block(notion, block_id):
    """清空輸出區塊既有內容（列到沒有為止）"""
    while True:
        old = notion.blocks.children.list(block_id=block_id, page_size=100)
        blocks = old.get("results") or []
        if not blocks:
            return
        for blk in blocks:
            notion.blocks.delete(block_id=blk["id"])
            time.sleep(0.2)


def append_stock_section(token, block_id, ticker, data, file_ids, labels, stamp):
    """一檔一個摺疊區塊：內容＝K線＋籌碼三張互動圖（HTML 區塊）"""
    title = "%s %s｜近 %d 個交易日（更新：%s）" % (ticker, data["name"], len(data["dates"]), stamp)
    embeds = [dict(type="embed", embed=dict(
        type="file_upload", file_upload=dict(id=fid),
        caption=[dict(type="text", text=dict(content=label))],
    )) for fid, label in zip(file_ids, labels)]
    toggle = dict(type="toggle", toggle=dict(
        rich_text=[dict(type="text", text=dict(content=title), annotations=dict(bold=True))],
        children=embeds,
    ))
    append_children(token, block_id, [toggle])
    time.sleep(0.34)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default=None, help="只畫這一檔（例 2884）；不加＝台股清單全部檔")
    parser.add_argument("--days", type=int, default=90, help="回看天數，預設 90")
    parser.add_argument("--local", action="store_true", help="只存本機 HTML、不上傳 Notion（預覽用）")
    parser.add_argument("--market", default="台股", choices=["台股", "美股"],
                        help="搭配 --ticker 用；美股出 ①⑤ 兩張")
    args = parser.parse_args()

    load_dotenv(os.path.join(os.path.dirname(BASE_DIR), ".env"))
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    token = os.environ["NOTION_TOKEN"]
    notion = Client(auth=token)
    page_id = os.environ["NOTION_PAGE_CHARTS"]
    chips_ds = get_data_source_id(notion, os.environ["NOTION_DB_TW_STOCK_CHIPS"])

    items = ([dict(code=args.ticker, name="", market=args.market)]
             if args.ticker else fetch_watchlist(notion))
    log.info("本次要畫 %d 檔：%s", len(items),
             ", ".join("%s(%s)" % (it["code"], it["market"]) for it in items))
                 
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    sections = []
    for it in items:
        ticker, market = it["code"], it["market"]

        # ── ①⑤：台美共用（D19.5：K 線＋BOLL、動能副圖）──
        figs = []  # (圖, 標籤, 高度)
        df_k, nbars = None, 0
        try:
            fig_k, nbars, df_k = chart4(ticker, args.days, market=market)
            if fig_k is not None:
                figs.append((fig_k, "① K線×均線×BOLL×量能", 720))
        except Exception as e:
            log.warning("%s K 線圖失敗（%s）", ticker, e)
        if df_k is not None:
            figs.append((chart5(df_k, ticker, it.get("name", "")), "⑤ 動能 KD×RSI×MACD", 560))

        if market == "美股":
            if not figs:
                log.warning("%s 近 %d 天查無資料，略過", ticker, args.days)
                continue
            data = dict(name=it.get("name", ""), dates=["-"] * nbars)
        else:
            rows = query_rows(notion, chips_ds, ticker, args.days)
            if not rows and not figs:
                log.warning("%s 近 %d 天查無資料，略過（新標的先跑 fetch_stock_chips.py）", ticker, args.days)
                continue
            if not rows:
                log.warning("%s 籌碼查無資料，本檔先出 ①⑤（新標的先跑 fetch_stock_chips.py）", ticker)
                data = dict(name=it.get("name", ""), dates=["-"] * nbars)
            else:
                data = extract(rows)
                figs.append((chart1(data, ticker), "② 股價×融資融券借券", 560))
                figs.append((chart2(data, ticker), "③ 股價×法人買賣超", 560))
                figs.append((chart3(data, ticker), "④ 股價×持股比例", 560))

        paths, labels = [], []
        for i, (fig, label, height) in enumerate(figs, start=1):
            path = os.path.join(OUT_DIR, "chart%d_%s.html" % (i, ticker))
            save_html(fig, path, height=height)
            paths.append(path)
            labels.append(label)
        if args.local:
            log.info("%s %s：%d 張互動圖已存 %s（--local 不上傳）", ticker, data["name"], len(paths), OUT_DIR)
            continue
        file_ids = [upload_file(token, p, "text/html") for p in paths]
        sections.append((ticker, data, file_ids, labels))
        log.info("%s %s：%d 張互動圖上傳完成", ticker, data["name"], len(file_ids))

    if args.local:
        return
    if not sections:
        log.error("沒有任何一檔有資料，Notion 頁面維持原狀")
        return
    clear_charts_block(notion, page_id)
    for ticker, data, file_ids, labels in sections:
        append_stock_section(token, page_id, ticker, data, file_ids, labels, stamp)
    log.info("完成：%d 檔互動組合圖已更新到 Notion", len(sections))


if __name__ == "__main__":
    main()
