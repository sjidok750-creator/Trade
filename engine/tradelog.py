"""거래·판단 기록 — SQLite + JSON Lines 이중 기록.

Claude 튜닝 루프와 일일 리포트가 이 기록을 읽는다.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone


class TradeLog:
    def __init__(self, db_path: str = "logs/trades.db", jsonl_path: str = "logs/events.jsonl"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.jsonl_path = jsonl_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                ts TEXT, market TEXT, side TEXT, krw REAL, volume REAL,
                price REAL, reason TEXT, dry_run INTEGER, order_uuid TEXT
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS equity (
                ts TEXT, equity_krw REAL, krw_balance REAL, positions TEXT
            )""")
        self.conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _jsonl(self, event: dict):
        with open(self.jsonl_path, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def trade(self, market: str, side: str, krw: float, volume: float,
              price: float, reason: str, dry_run: bool, order_uuid: str = ""):
        ts = self._now()
        self.conn.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
                          (ts, market, side, krw, volume, price, reason,
                           int(dry_run), order_uuid))
        self.conn.commit()
        self._jsonl({"type": "trade", "ts": ts, "market": market, "side": side,
                     "krw": krw, "volume": volume, "price": price,
                     "reason": reason, "dry_run": dry_run})

    def equity(self, equity_krw: float, krw_balance: float, positions: dict):
        ts = self._now()
        self.conn.execute("INSERT INTO equity VALUES (?,?,?,?)",
                          (ts, equity_krw, krw_balance, json.dumps(positions)))
        self.conn.commit()

    def event(self, kind: str, **fields):
        self._jsonl({"type": kind, "ts": self._now(), **fields})
