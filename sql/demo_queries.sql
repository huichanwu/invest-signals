USE invest;

/* =============================================================
   Q1. JOIN：台積電近 20 日收盤價 × 外資買賣超 × 融資餘額
   商業意義：股價漲跌是「誰推的」——外資連買上漲＝法人行情，
   融資暴增上漲＝散戶行情，後者籌碼不穩。
   （T-SQL 用 TOP，不是 LIMIT）
   ============================================================= */
SELECT TOP 20
       p.[date],
       p.close_price,
       c.foreign_net      AS 外資買賣超_張,
       c.trust_net        AS 投信買賣超_張,
       c.margin_balance   AS 融資餘額_張
FROM daily_prices p
JOIN stock_chips  c
  ON c.symbol = p.symbol AND c.[date] = p.[date]
WHERE p.symbol = '2330'
ORDER BY p.[date] DESC;

/* =============================================================
   Q2. Window function：用 AVG() OVER 自算 MA20，
       驗證與 Python 管線算的 indicators.ma20 一致
   商業意義：同一指標兩套產線交叉驗證，確保衍生資料可信——
   這就是資料品質稽核的日常做法。diff 應接近 0。
   ============================================================= */
WITH ma AS (
  SELECT symbol, [date], close_price,
         AVG(close_price) OVER (PARTITION BY symbol ORDER BY [date]
              ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20_sql,
         COUNT(close_price) OVER (PARTITION BY symbol ORDER BY [date]
              ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS n_days
  FROM daily_prices
)
SELECT TOP 20
       m.[date],
       m.close_price,
       ROUND(m.ma20_sql, 2)          AS ma20_sql,
       ROUND(i.ma20, 2)              AS ma20_python,
       ROUND(m.ma20_sql - i.ma20, 4) AS diff
FROM ma m
JOIN indicators i
  ON i.symbol = m.symbol AND i.[date] = m.[date]
WHERE m.symbol = '2330'
  AND m.n_days = 20            -- 前 19 天樣本不足不比
  AND i.ma20 IS NOT NULL
ORDER BY m.[date] DESC;

/* =============================================================
   Q3. 連續天數（gaps-and-islands）：外資連買 ≥3 日標的
   商業意義：單日買超可能是隨機波動，連續買超才是法人表態；
   用兩個 ROW_NUMBER 差值分組找出每一段連買區間。
   ============================================================= */
WITH seq AS (
  SELECT symbol, [date], foreign_net,
         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY [date]) AS rn_all,
         ROW_NUMBER() OVER (PARTITION BY symbol,
                            CASE WHEN foreign_net > 0 THEN 1 ELSE 0 END
                            ORDER BY [date]) AS rn_grp
  FROM stock_chips
),
streaks AS (
  SELECT symbol,
         MIN([date])      AS start_date,
         MAX([date])      AS end_date,
         COUNT(*)         AS buy_days,
         SUM(foreign_net) AS total_net_張
  FROM seq
  WHERE foreign_net > 0
  GROUP BY symbol, rn_all - rn_grp
)
SELECT s.symbol, k.name, s.start_date, s.end_date, s.buy_days, s.total_net_張
FROM streaks s
LEFT JOIN stocks k ON k.symbol = s.symbol
WHERE s.buy_days >= 3
ORDER BY s.buy_days DESC, s.total_net_張 DESC;

/* =============================================================
   Q4. 彙總＋篩選：近 60 日外資累計買超排名 × 爆量天數
   商業意義：「外資持續買＋量能放大」同時成立的標的，
   是籌碼面最強的一批；爆量定義：當日成交量 > 20 日均量 × 1.5。
   ============================================================= */
SELECT c.symbol,
       k.name,
       SUM(c.foreign_net) AS 外資累計買超_張,
       SUM(CASE WHEN p.volume > i.mv20 * 1.5 THEN 1 ELSE 0 END) AS 爆量天數,
       COUNT(*) AS 樣本日數
FROM stock_chips c
JOIN daily_prices p ON p.symbol = c.symbol AND p.[date] = c.[date]
JOIN indicators  i ON i.symbol = c.symbol AND i.[date] = c.[date]
LEFT JOIN stocks k ON k.symbol = c.symbol
WHERE c.[date] >= DATEADD(DAY, -60, CAST(GETDATE() AS DATE))
GROUP BY c.symbol, k.name
ORDER BY 外資累計買超_張 DESC;

/* =============================================================
   Q5. 跨頻度 JOIN：週頻大戶線向上 × 日頻收盤站上 MA20
   商業意義：大戶週線回升＝籌碼流向強手，日線站上 MA20＝
   價格趨勢配合；兩個不同頻度的訊號交集才是高品質候選。
   ============================================================= */
WITH ranked AS (
  SELECT symbol, week_date, over_400_ratio,
         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY week_date DESC) AS rn
  FROM tdcc_weekly
),
rising AS (               -- 最新一週 400 張大戶比率 > 前一週
  SELECT a.symbol,
         b.over_400_ratio AS 前週大戶率,
         a.over_400_ratio AS 本週大戶率
  FROM ranked a
  JOIN ranked b ON b.symbol = a.symbol AND b.rn = 2
  WHERE a.rn = 1 AND a.over_400_ratio > b.over_400_ratio
),
latest_px AS (            -- 每檔最新交易日的收盤與 MA20
  SELECT p.symbol, p.[date], p.close_price, i.ma20,
         ROW_NUMBER() OVER (PARTITION BY p.symbol ORDER BY p.[date] DESC) AS rn
  FROM daily_prices p
  JOIN indicators i ON i.symbol = p.symbol AND i.[date] = p.[date]
)
SELECT r.symbol, k.name,
       r.前週大戶率, r.本週大戶率,
       ROUND(r.本週大戶率 - r.前週大戶率, 2) AS 週增減,
       x.[date] AS 最新交易日, x.close_price, ROUND(x.ma20, 2) AS ma20
FROM rising r
JOIN latest_px x ON x.symbol = r.symbol AND x.rn = 1
LEFT JOIN stocks k ON k.symbol = r.symbol
WHERE x.close_price > x.ma20
ORDER BY 週增減 DESC;