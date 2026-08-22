"""오프라인 단위 테스트 — 네트워크 없이 핵심 로직 검증.

실행: python -m tests.test_all
"""
import json
import os
import shutil
import tempfile
import unittest

from engine import fee_guard
from engine.risk import RiskManager
from strategies.ma_trend import update_state
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


class TestFeeGuard(unittest.TestCase):
    def test_expiry_states(self):
        from datetime import date, timedelta
        fresh = (date.today() - timedelta(days=1)).isoformat()
        soon = (date.today() - timedelta(days=28)).isoformat()
        gone = (date.today() - timedelta(days=35)).isoformat()
        self.assertIsNone(fee_guard.expiry_warning(fresh))
        self.assertIn("뒤 만료", fee_guard.expiry_warning(soon))
        self.assertIn("만료됐습니다", fee_guard.expiry_warning(gone))
        self.assertIsNone(fee_guard.expiry_warning(None))
        self.assertIsNone(fee_guard.expiry_warning("엉터리"))

    def test_fee_anomaly(self):
        # 0.04%: 정상
        ok = {"paid_fee": "40", "executed_volume": "1", "price": "100000"}
        self.assertIsNone(fee_guard.fee_anomaly(ok))
        # 0.25%: 쿠폰 만료
        bad = {"paid_fee": "250", "executed_volume": "1", "price": "100000"}
        self.assertIn("재신청", fee_guard.fee_anomaly(bad))
        # 계산 불가한 응답은 조용히 무시
        self.assertIsNone(fee_guard.fee_anomaly({}))


class TestMaTrend(unittest.TestCase):
    def _candles(self, closes):
        """closes: 최신순 종가 리스트 → API 형태"""
        return [{"trade_price": c, "opening_price": c, "high_price": c, "low_price": c}
                for c in closes]

    def test_enter_exit_band(self):
        # 어제 종가 110, 이전 5일 MA 100 → +10%는 밴드 3% 위 → 진입
        c = self._candles([999, 110] + [100] * 5)
        self.assertTrue(update_state(c, 5, 0.03, prev=False))
        # 어제 종가 95 → -5%는 밴드 아래 → 이탈
        c = self._candles([999, 95] + [100] * 5)
        self.assertFalse(update_state(c, 5, 0.03, prev=True))
        # 밴드 안쪽(101)에서는 직전 상태 유지
        c = self._candles([999, 101] + [100] * 5)
        self.assertTrue(update_state(c, 5, 0.03, prev=True))
        self.assertFalse(update_state(c, 5, 0.03, prev=False))

    def test_insufficient_keeps_prev(self):
        self.assertTrue(update_state(self._candles([100, 100]), 5, 0.03, prev=True))


class TestEngineTrend(unittest.TestCase):
    """가짜 거래소로 추세 전략의 리밸런싱(진입→이탈) 흐름 검증."""

    def test_rebalance_cycle(self):
        os.chdir(tempfile.mkdtemp())
        import yaml
        cfg = {
            "mode": {"dry_run": True, "poll_interval_sec": 1},
            "universe": ["KRW-AAA", "KRW-BBB"],
            "strategy": {"name": "ma_trend", "ma_len": 5, "band": 0.03,
                         "rebal_days": 7, "reset_hour_kst": 0,
                         "k": 0.5, "per_coin_k": {}, "trend_filter_ma": 0},
            "risk": {**RISK_CFG, "max_positions": 6},
            "fee": {"coupon_renewed_on": None},
            "notify": {"telegram": False},
        }
        with open("config.yaml", "w") as f:
            yaml.safe_dump(cfg, f)

        from engine.runner import Engine

        class FakeExchange:
            def __init__(self):
                self.trend = {"KRW-AAA": True, "KRW-BBB": False}
            def get_tickers(self, markets):
                return {m: 100.0 for m in markets}
            def get_daily_candles(self, market, count=10, to=None):
                yday = 110.0 if self.trend[market] else 90.0
                return [{"trade_price": p, "opening_price": p,
                         "high_price": p, "low_price": p}
                        for p in [999.0, yday] + [100.0] * (count - 2)]

        eng = Engine(cfg)
        eng.ex = FakeExchange()
        eng.tick()  # 첫 거래일 → 리밸런싱: AAA만 추세
        self.assertIn("KRW-AAA", eng.positions)
        self.assertNotIn("KRW-BBB", eng.positions)

        # 7일 뒤 AAA 추세 꺾임 → 매도되어야 함
        eng.ex.trend["KRW-AAA"] = False
        states = eng._load_json("state/trend.json", {})
        states["_last_rebal"] = "2000-01-01"   # 강제로 리밸 기한 경과 처리
        eng._save_json("state/trend.json", states)
        eng.trade_day = ""                      # 새 거래일 트리거
        eng.tick()
        self.assertNotIn("KRW-AAA", eng.positions)


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
            "fee": {"coupon_renewed_on": None},
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
