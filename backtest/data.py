"""빗썸 일봉 데이터 수집 → data/{market}.csv

사용법 (네트워크가 되는 환경 — VPS 등 — 에서 실행):
    python -m backtest.data                # config.yaml universe 전체, 3년치
    python -m backtest.data KRW-BTC 730    # 특정 코인, 일수 지정
"""
import csv
import os
import sys
import time

import yaml

from exchange.bithumb import Bithumb

FIELDS = ["date", "open", "high", "low", "close", "volume"]


def download(market: str, days: int = 1095, out_dir: str = "data") -> str:
    ex = Bithumb()
    rows = []
    to = None
    while len(rows) < days:
        batch = ex.get_daily_candles(market, count=200, to=to)
        if not batch:
            break
        for c in batch:
            rows.append({
                "date": c["candle_date_time_kst"][:10],
                "open": c["opening_price"], "high": c["high_price"],
                "low": c["low_price"], "close": c["trade_price"],
                "volume": c.get("candle_acc_trade_volume", 0),
            })
        to = batch[-1]["candle_date_time_utc"]
        time.sleep(0.15)  # 요청 한도 준수
    rows = rows[:days][::-1]  # 과거 → 최신 순으로 저장
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{market}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"{market}: {len(rows)}일 저장 → {path}")
    return path


def load_csv(market: str, data_dir: str = "data") -> list[dict]:
    path = os.path.join(data_dir, f"{market}.csv")
    with open(path) as f:
        return [{k: (row[k] if k == "date" else float(row[k]))
                 for k in FIELDS} for row in csv.DictReader(f)]


if __name__ == "__main__":
    if len(sys.argv) > 1:
        download(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1095)
    else:
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)
        for m in cfg["universe"]:
            download(m)
