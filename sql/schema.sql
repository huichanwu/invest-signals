-- 先在 SSMS 連上 localhost\SQLEXPRESS，建立資料庫
CREATE DATABASE invest;
GO
USE invest;
GO

CREATE TABLE stocks (
  symbol       NVARCHAR(20) PRIMARY KEY,  -- 2330 / 0050 / 美股代碼
  name         NVARCHAR(50),
  market       NVARCHAR(10),              -- TW / US
  industry     NVARCHAR(50),
  in_watchlist INT DEFAULT 1
);

CREATE TABLE daily_prices (
  symbol NVARCHAR(20), [date] DATE,
  open_price FLOAT, high FLOAT, low FLOAT, close_price FLOAT, volume BIGINT,
  PRIMARY KEY (symbol, [date])
);

CREATE TABLE market_chips (
  [date] DATE PRIMARY KEY,
  foreign_futures_net INT,     -- 外資台指期未平倉淨口數
  foreign_net_change  INT,     -- 日增減
  top_traders_net     INT,     -- 特法淨部位
  margin_balance      FLOAT,   -- 融資餘額
  margin_maintenance  FLOAT    -- 自家口徑維持率
);

CREATE TABLE stock_chips (
  symbol NVARCHAR(20), [date] DATE,
  foreign_net BIGINT, trust_net BIGINT, dealer_net BIGINT,
  margin_balance BIGINT, short_balance BIGINT,
  PRIMARY KEY (symbol, [date])
);

CREATE TABLE indicators (
  symbol NVARCHAR(20), [date] DATE,
  ma5 FLOAT, ma10 FLOAT, ma20 FLOAT, ma60 FLOAT, ma120 FLOAT,
  k FLOAT, d FLOAT, macd FLOAT, rsi FLOAT, bias FLOAT,
  boll_upper FLOAT, boll_lower FLOAT, mv5 FLOAT, mv20 FLOAT,
  PRIMARY KEY (symbol, [date])
);

CREATE TABLE signals (
  [date] DATE, [level] NVARCHAR(10), symbol NVARCHAR(20) DEFAULT '',
  signal_type NVARCHAR(50), triggered INT, reason NVARCHAR(200),
  PRIMARY KEY ([date], [level], signal_type, symbol)
);

CREATE TABLE tdcc_weekly (
  symbol NVARCHAR(20), week_date DATE,
  over_1000_ratio FLOAT, over_400_ratio FLOAT, holders_count INT,
  PRIMARY KEY (symbol, week_date)
);