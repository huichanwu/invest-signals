"""
run_daily.py｜每日訊號管線總控
執行：uv run run_daily.py --slot day（16:00）／--slot night（22:00）
（Task Scheduler 同樣用 uv run 或 venv python，開始位置設專案資料夾）
原則：
  1. 清單驅動：步驟寫在 STEPS，美股先 enabled=False，D21–D25 完成後改 True 即可
  2. 隔離失敗：每步用 subprocess 跑，單步掛掉記 log、繼續下一步
  3. 全程留痕：logs\run_daily_YYYYMMDD.log
  4. 時段制：day 跑公布得早的、night 跑 21:30 後才齊的與所有判定，取代原本逐支排程的時間設計
"""
import datetime as dt
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# (名稱, 腳本檔名, 啟用, 時段 "day"=16:00／"night"=22:00, 限定星期(0=週一…5=週六, None=每天), 逾時秒)
# 對照 2026/08/20 工作排程器實際任務清單完成（舊任務已全部停用，由本腳本接手）
STEPS = [
    # ── day 16:00（原 14:00–14:10 時段：期交所盤後、收盤價已齊）──
    ("股價同步",         "sync_price.py",              True,  "day",   None, 600),   # 原「股價同步」14:00
    ("台股L0 籌碼抓取",   "notion/notion_writer.py",    True,  "day",   None, 600),   # 原「籌碼訊號」14:00（期交所＋維持率）
    ("台股L0 訊號判定",   "signal_tw_l0.py",            True,  "day",   None, 300),   # 原「第0層自動判定」14:10
    ("技術面 全檔掃描",   "transform/scan_all.py",      True,  "day",   None, 1800),  # 原手動，D18 起正式自動化
    # ── night 22:00（原 21:35–22:00 時段：融資融券、集保）──
    ("台股L1 集保資料",   "fetch/fetch_tdcc.py",        True,  "night", None, 600),   # 原每日 21:35＋週六 08:30
    ("籌碼組合圖",       "fetch/plot_stock_charts.py", True,  "night", None, 900),   # 原 21:40
    ("台股L1 個股籌碼",   "fetch/fetch_stock_chips.py", True,  "night", None, 900),   # 原 22:00
    ("台股L1 大戶線判定", "signal_tw_l1.py",            True,  "night", 5,    300),   # 原週六 09:00 → 改週六 night，接在集保之後
    # ── 美股 L0：尚未整合，先留開關（D21–D25 每完成一支改一支 True；美股收盤在台灣清晨 → day）──
    ("美股L0 情緒三件組", "fetch_us_sentiment.py",      False, "day",   None, 600),
    ("美股L0 市場廣度",   "fetch_us_breadth.py",        False, "day",   None, 600),
    ("美股L0 CFTC COT",   "fetch_cftc_cot.py",          False, "day",   4,    600),
    ("美股L0 FINRA",      "fetch_finra.py",             False, "day",   None, 600),
]


def setup_logger() -> logging.Logger:
    log_file = LOG_DIR / f"run_daily_{dt.date.today():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger("run_daily")


def run_step(log, name: str, script: str, timeout: int) -> bool:
    log.info("=== %s（%s）開始 ===", name, script)
    t0 = dt.datetime.now()
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / script)],
            cwd=ROOT, capture_output=True, timeout=timeout,
            encoding="utf-8", errors="replace",  # 用 UTF-8 解碼子腳本輸出
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},  # 強制子腳本用 UTF-8 印
        )
        elapsed = (dt.datetime.now() - t0).seconds
        if result.stdout:
            log.info("%s stdout（末 500 字）：%s", name, result.stdout[-500:])
        if result.returncode != 0:
            log.error("%s 失敗（exit=%s，%ss）：%s",
                      name, result.returncode, elapsed, result.stderr[-1000:])
            return False
        log.info("=== %s 完成（%ss）===", name, elapsed)
        return True
    except subprocess.TimeoutExpired:
        log.error("%s 逾時（>%ss），略過續跑下一步", name, timeout)
        return False
    except Exception:
        log.exception("%s 出現未預期錯誤", name)
        return False


def main() -> int:
    log = setup_logger()
    slot = sys.argv[sys.argv.index("--slot") + 1] if "--slot" in sys.argv else "night"
    weekday = dt.date.today().weekday()
    log.info("本次時段：--slot %s", slot)
    failed: list[str] = []
    for name, script, enabled, step_slot, only_weekday, timeout in STEPS:
        if not enabled:
            log.info("跳過（未啟用）：%s", name)
            continue
        if step_slot != slot:
            continue
        if only_weekday is not None and weekday != only_weekday:
            log.info("跳過（非週%s）：%s", "一二三四五六日"[only_weekday], name)
            continue
        if not run_step(log, name, script, timeout):
            failed.append(name)
    if failed:
        log.error("今日執行完畢，失敗 %d 步：%s", len(failed), "、".join(failed))
        return 1
    log.info("今日全部步驟成功 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())