"""오프라인 단위 테스트 — 네트워크 없이 핵심 로직 검증.

실행: python -m tests.test_all
"""
import json
import os
import shutil
import tempfile
import unittest

from engine.risk import RiskManager
from strategies.volatility_breakout import compute_signal

RISK_CFG = {
    "position_pct": 0.18, "max_positions": 5, "stop_loss_pct": 0.05,
    "daily_loss_limit_pct": 0.05, "kill_switch_pct": 0.20,
    "crash_guard_pct": 0.10, "min_order_krw": 5500, "max_daily_buys": 6,
}


def candles(rows):
    """rows: [(open, high, low, close)] 최신순 → API 형태로 변환"""
    return [{"opening_price": o, "high_price": h, "low_price": l, "trade_price": c}
            for o, h, l, c in rows]


class TestStrategy(unittest.TestCase):
    def test_target_price(self):
        # 전일 변동폭 100, k=0.5 → 목표가 = 시가 1000 + 50
        sig = compute_signal("KRW-BTC", candles([(1000, 0, 0, 0), (900, 950, 850, 940)]), k=0.5)
        self.assertAlmostEqual(sig.target_price, 1050)
        self.assertTrue(sig.should_buy(1050))
        self.assertFalse(sig.should_buy(1049))

    def test_trend_filter_blocks(self):
        # 시가 1000이 5일 MA(전일 종가 1100×5)보다 낮으면 진입 금지
        rows = [(1000, 0, 0, 0)] + [(0, 1200, 1000, 1100)] * 5
        sig = compute_signal("KRW-BTC", candles(rows), k=0.5, trend_filter_ma=5)
        self.assertFalse(sig.trend_ok)
        self.assertFalse(sig.should_buy(10_000))

    def test_insufficient_data(self):
        self.assertIsNone(compute_signal("KRW-BTC", candles([(1000, 0, 0, 0)]), k=0.5))


class TestRisk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rm = RiskManager(RISK_CFG, state_path=os.path.join(self.tmp, "risk.json"))
        self.rm.new_day("2026-08-22", 1_000_000)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_daily_loss_limit(self):
        self.assertIsNone(self.rm.check_equity(960_000))       # -4%: 정상
        self.assertEqual(self.rm.check_equity(949_000), "daily_loss_limit")  # -5.1%
        ok, why = self.rm.can_buy(0, 100_000, 500_000)
        self.assertFalse(ok)

    def test_kill_switch(self):
        self.rm.check_equity(1_200_000)                        # 고점 갱신
        self.assertEqual(self.rm.check_equity(959_000), "kill_switch")  # 고점 대비 -20.1%
        self.assertFalse(self.rm.can_buy(0, 100_000, 500_000)[0])

    def test_buy_limits(self):
        self.assertFalse(self.rm.can_buy(5, 100_000, 500_000)[0])   # 동시 보유 한도
        self.assertFalse(self.rm.can_buy(0, 5_000, 500_000)[0])     # 최소 주문 미달
        self.assertFalse(self.rm.can_buy(0, 600_000, 500_000)[0])   # 잔고 부족
        for _ in range(6):
            self.rm.record_buy()
        self.assertFalse(self.rm.can_buy(0, 100_000, 500_000)[0])   # 일일 횟수 상한

    def test_state_persists(self):
        self.rm.record_buy()
        rm2 = RiskManager(RISK_CFG, state_path=self.rm.state_path)
        self.assertEqual(rm2.state.daily_buys, 1)


class TestBacktester(unittest.TestCase):
    def test_synthetic_uptrend(self):
        # 합성 데이터: 꾸준한 상승 + 돌파 발생 → 백테스터가 음수 자본 없이 완주하는지
        import csv
        from backtest.backtester import run
        tmp = tempfile.mkdtemp()
        try:
            price = 1000.0
            rows = []
            for i in range(60):
                o = price
                h = o * 1.06
                l = o * 0.98
                c = o * 1.03
                rows.append({"date": f"2026-01-{i+1:02d}" if i < 30 else f"2026-02-{i-29:02d}",
                             "open": o, "high": h, "low": l, "close": c, "volume": 1})
                price = c
            with open(os.path.join(tmp, "KRW-TST.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
                w.writeheader()
                w.writerows(rows)
            r = run(markets=["KRW-TST"], k=0.5, position_pct=0.18, max_positions=5,
                    stop_loss_pct=0.05, data_dir=tmp)
            self.assertGreater(r["trades"], 0)
            self.assertGreater(r["final_capital"], 0)
        finally:
            shutil.rmtree(tmp)


class TestEnginePaper(unittest.TestCase):
    """가짜 거래소로 엔진의 매수→손절 흐름을 검증."""

    def test_buy_then_stop_loss(self):
        os.chdir(tempfile.mkdtemp())
        import yaml
        cfg = {
            "mode": {"dry_run": True, "poll_interval_sec": 1},
            "universe": ["KRW-TST"],
            "strategy": {"name": "volatility_breakout", "k": 0.5,
                         "reset_hour_kst": 0, "per_coin_k": {}, "trend_filter_ma": 0},
            "risk": RISK_CFG,
            "notify": {"telegram": False},
        }
        with open("config.yaml", "w") as f:
            yaml.safe_dump(cfg, f)

        from engine.runner import Engine

        class FakeExchange:
            def __init__(self):
                self.price = 1050.0
            def get_tickers(self, markets):
                return {"KRW-TST": self.price}
            def get_daily_candles(self, market, count=30, to=None):
                return candles([(1000, 0, 0, 0)] + [(900, 950, 850, 940)] * (count - 1))

        eng = Engine(cfg)
        eng.ex = FakeExchange()
        eng.tick()  # 신규 거래일 → 신호 계산 → 1050 >= 목표 1050 → 매수
        self.assertIn("KRW-TST", eng.positions)
        self.assertLess(eng.paper["krw"], 1_000_000)

        eng.ex.price = 990.0  # 진입가 대비 -5.7% → 손절
        eng.tick()
        self.assertNotIn("KRW-TST", eng.positions)

        with open("logs/events.jsonl") as f:
            events = [json.loads(line) for line in f]
        self.assertTrue(any(e["type"] == "trade" and e["side"] == "sell"
                            and "stop_loss" in e["reason"] for e in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
