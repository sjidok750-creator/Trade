"""오프라인 단위 테스트 — 네트워크 없이 핵심 로직 검증.

실행: python -m tests.test_all
"""
import json
import os
import shutil
import tempfile
import unittest

from engine import commands, fee_guard, report, skim
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

    def test_withdraw_is_not_a_loss(self):
        """이익 확정(출금)으로 자산이 줄어도 킬스위치가 발동하면 안 된다."""
        self.rm.check_equity(1_300_000)          # 고점 갱신
        self.rm.withdraw(280_000)                # 이익 분리
        # 출금 후 자산 1,020,000은 손실이 아니므로 킬스위치 발동 금지
        self.assertIsNone(self.rm.check_equity(1_020_000))
        self.assertFalse(self.rm.state.halted)
        # 보정 후에도 진짜 급락은 여전히 잡아낸다
        self.assertEqual(self.rm.check_equity(700_000), "kill_switch")

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


class TestReport(unittest.TestCase):
    def test_due_only_on_report_hours(self):
        from datetime import datetime
        at = lambda h: datetime(2026, 8, 23, h, 30, tzinfo=report.KST)
        self.assertEqual(report.due("", at(8)), "2026-08-23-08")
        self.assertIsNone(report.due("", at(9)))          # 리포트 시각 아님
        self.assertIsNone(report.due("", at(23)))         # 22시 이후 조용
        # 같은 슬롯은 한 번만
        self.assertIsNone(report.due("2026-08-23-10", at(10)))
        self.assertEqual(report.due("2026-08-23-10", at(12)), "2026-08-23-12")

    def test_build_message(self):
        msg = report.build(
            "페이퍼", 1_050_000, 1_000_000, 200_000,
            {"KRW-ETH": {"volume": 1.0, "entry_price": 100.0, "krw_spent": 100.0}},
            {"KRW-ETH": 120.0},
            {"KRW-ETH": True, "KRW-BTC": False},
            ["KRW-BTC", "KRW-ETH"])
        self.assertIn("페이퍼", msg)
        self.assertIn("+5.00%", msg)      # 총자산 손익률
        self.assertIn("ETH", msg)
        self.assertIn("+20.0%", msg)      # 개별 포지션 손익률
        self.assertIn("BTC", msg)         # 추세 아래 대기 목록

    def test_build_no_positions(self):
        msg = report.build("실전", 900_000, 1_000_000, 900_000, {}, {}, {}, ["KRW-BTC"])
        self.assertIn("보유 없음", msg)
        self.assertIn("-10.00%", msg)


class TestTakeProfit(unittest.TestCase):
    """익절 규칙이 실제로 자산을 잠그고 재진입을 지연시키는지 검증."""

    def setUp(self):
        # 매일 +10% 오르는 단일 코인, 항상 100% 보유하는 전략
        self.dates = [f"2026-01-{d:02d}" for d in range(1, 11)]
        price = {d: 100.0 * (1.1 ** i) for i, d in enumerate(self.dates)}
        self.closes = {"KRW-X": price}
        self.always = lambda i: {"KRW-X": 1.0}

    def test_take_profit_caps_upside(self):
        from backtest.portfolio import run
        base = run("base", self.dates, self.closes, self.always, cost=0.0)
        tp = run("tp", self.dates, self.closes, self.always, cost=0.0,
                 take_profit=0.05)
        # 상승장에서 익절은 수익을 깎는다 (오른 만큼 못 먹음)
        self.assertLess(tp.total_return_pct, base.total_return_pct)

    def test_cooldown_reduces_market_exposure(self):
        from backtest.portfolio import run
        no_cd = run("a", self.dates, self.closes, self.always, cost=0.0,
                    take_profit=0.05, cooldown=0)
        with_cd = run("b", self.dates, self.closes, self.always, cost=0.0,
                      take_profit=0.05, cooldown=3)
        self.assertLess(with_cd.days_in_market_pct, no_cd.days_in_market_pct)

    def test_disabled_by_default(self):
        from backtest.portfolio import run
        a = run("a", self.dates, self.closes, self.always, cost=0.0)
        b = run("b", self.dates, self.closes, self.always, cost=0.0, take_profit=0.0)
        self.assertAlmostEqual(a.total_return_pct, b.total_return_pct)


class TestSkim(unittest.TestCase):
    def test_pending_thresholds(self):
        # 목표 미달이면 확정하지 않는다
        self.assertEqual(skim.pending(1_000_000, 1_015_000, 20_000, 5_500), 0)
        # 딱 도달하면 목표만큼
        self.assertEqual(skim.pending(1_000_000, 1_020_000, 20_000, 5_500), 20_000)
        # 배수로 쌓였으면 배수만큼 한 번에
        self.assertEqual(skim.pending(1_000_000, 1_055_000, 20_000, 5_500), 40_000)
        # 손실 구간에서는 확정 없음
        self.assertEqual(skim.pending(1_000_000, 950_000, 20_000, 5_500), 0)

    def test_plan_uses_cash_first(self):
        p = skim.plan(20_000, cash=50_000, positions={}, prices={}, min_order=5_500)
        self.assertEqual(p.from_cash, 20_000)
        self.assertEqual(p.sell, {})

    def test_plan_sells_proportionally(self):
        positions = {"KRW-A": {"volume": 1.0}, "KRW-B": {"volume": 3.0}}
        prices = {"KRW-A": 100_000.0, "KRW-B": 100_000.0}   # A:B = 1:3
        p = skim.plan(20_000, cash=0, positions=positions, prices=prices,
                      min_order=1_000)
        self.assertAlmostEqual(p.sell["KRW-A"], 5_000)
        self.assertAlmostEqual(p.sell["KRW-B"], 15_000)
        self.assertAlmostEqual(sum(p.sell.values()), 20_000)

    def test_plan_refuses_when_insufficient(self):
        positions = {"KRW-A": {"volume": 0.01}}
        prices = {"KRW-A": 100_000.0}         # 보유 평가액 1,000원
        self.assertIsNone(skim.plan(20_000, 0, positions, prices, 5_500))


class TestEngineSkim(unittest.TestCase):
    """엔진에서 이익 분리 → 출금 대기 → /settle 초기화 전체 흐름 검증."""

    def _engine(self, price):
        os.chdir(tempfile.mkdtemp())
        import yaml
        cfg = {
            "mode": {"dry_run": True, "poll_interval_sec": 1},
            "universe": ["KRW-AAA"],
            "strategy": {"name": "ma_trend", "ma_len": 5, "band": 0.03,
                         "rebal_days": 7, "reset_hour_kst": 0,
                         "k": 0.5, "per_coin_k": {}, "trend_filter_ma": 0},
            "risk": {**RISK_CFG, "max_positions": 6, "min_order_krw": 5_500},
            "settle": {"target_krw": 20_000},
            "fee": {"coupon_renewed_on": None},
            "notify": {"telegram": False},
        }
        with open("config.yaml", "w") as f:
            yaml.safe_dump(cfg, f)
        from engine.runner import Engine

        class FakeExchange:
            def __init__(self, p):
                self.price = p
            def get_tickers(self, markets):
                return {"KRW-AAA": self.price}
            def get_daily_candles(self, market, count=10, to=None):
                return [{"trade_price": v, "opening_price": v,
                         "high_price": v, "low_price": v}
                        for v in [999.0, 110.0] + [100.0] * (count - 2)]

        eng = Engine(cfg)
        eng.ex = FakeExchange(price)
        return eng

    def test_skim_cycle(self):
        eng = self._engine(1000.0)
        eng.tick()                      # 진입
        self.assertIn("KRW-AAA", eng.positions)
        self.assertEqual(eng.settle["reserve"], 0.0)

        eng.ex.price = 1300.0           # +30% → 이익이 목표를 넘김
        eng.trade_day = ""
        states = eng._load_json("state/trend.json", {})
        states["_last_rebal"] = "2000-01-01"
        eng._save_json("state/trend.json", states)
        eng.tick()

        # 이익이 분리되어 출금 대기로 잡혔는지
        self.assertGreaterEqual(eng.settle["reserve"], 20_000)
        # 분리된 현금은 투자자산에서 제외된다
        prices = {"KRW-AAA": 1300.0}
        self.assertAlmostEqual(eng.total_equity(prices) - eng.settle["reserve"],
                               eng.equity(prices))
        # 포지션은 전량 청산되지 않고 일부만 줄었다
        self.assertIn("KRW-AAA", eng.positions)

        # /settle 로 출금 확인 → 대기액 0
        done = eng.confirm_settled()
        self.assertGreaterEqual(done, 20_000)
        self.assertEqual(eng.settle["reserve"], 0.0)

    def test_no_skim_when_disabled(self):
        eng = self._engine(1000.0)
        eng.cfg["settle"]["target_krw"] = 0
        eng.tick()
        eng.ex.price = 2000.0
        eng.trade_day = ""
        eng.tick()
        self.assertEqual(eng.settle["reserve"], 0.0)


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ctx = {
            "report": lambda: "현황입니다",
            "positions": {"KRW-ETH": {"volume": 2.0, "entry_price": 100.0,
                                      "krw_spent": 200.0}},
            "prices": {"KRW-ETH": 150.0},
            "trend": {"KRW-ETH": True, "KRW-BTC": False},
            "universe": ["KRW-BTC", "KRW-ETH"],
            "stop_path": os.path.join(self.tmp, "STOP"),
            "settle_done": lambda: self.settled,
        }
        self.settled = 0.0

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_status_and_help(self):
        self.assertEqual(commands.handle("/status", self.ctx), "현황입니다")
        self.assertIn("/positions", commands.handle("/help", self.ctx))
        self.assertIn("/positions", commands.handle("/start", self.ctx))

    def test_positions(self):
        out = commands.handle("/positions", self.ctx)
        self.assertIn("ETH", out)
        self.assertIn("+50.0%", out)     # 100 → 150

    def test_trend(self):
        out = commands.handle("/trend", self.ctx)
        self.assertIn("보유대상", out)
        self.assertIn("현금대기", out)

    def test_stop_resume(self):
        path = self.ctx["stop_path"]
        commands.handle("/stop", self.ctx)
        self.assertTrue(os.path.exists(path))
        commands.handle("/resume", self.ctx)
        self.assertFalse(os.path.exists(path))
        self.assertIn("이미 정상", commands.handle("/resume", self.ctx))

    def test_unknown_and_suffix(self):
        self.assertIsNone(commands.handle("/뭐라고", self.ctx))
        # /status@봇이름 형태도 인식
        self.assertEqual(commands.handle("/status@sjidok_trade_bot", self.ctx), "현황입니다")

    def test_settle(self):
        self.assertIn("없습니다", commands.handle("/settle", self.ctx))
        self.settled = 23_400.0
        self.assertIn("23,400", commands.handle("/settle", self.ctx))

    def test_no_trade_commands(self):
        """매수·매도 원격 지시는 지원하지 않는다 (규칙 기반 전제 보호)."""
        for c in ("/buy KRW-BTC", "/sell KRW-ETH", "/order"):
            self.assertIsNone(commands.handle(c, self.ctx))


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
