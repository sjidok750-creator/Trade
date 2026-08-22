"""자동매매 실행 엔진 (24시간 상시 실행).

실행: python -m engine.runner
정지: 프로젝트 루트에 STOP 파일 생성 (touch STOP) → 신규 주문 즉시 중단

dry_run=true(기본)이면 실주문 없이 가상 체결로 동작한다.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import yaml

from exchange.bithumb import Bithumb, BithumbError
from strategies.volatility_breakout import compute_signal
from . import notify
from .risk import RiskManager
from .tradelog import TradeLog

KST = timezone(timedelta(hours=9))
FEE = 0.0004  # 수수료 0.04% (무료 쿠폰 적용 기준)
PAPER_START_KRW = 1_000_000


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class Engine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dry_run = cfg["mode"]["dry_run"]
        self.ex = Bithumb()
        self.risk = RiskManager(cfg["risk"])
        self.log = TradeLog()
        self.tg = cfg["notify"]["telegram"]
        self.positions = self._load_json("state/positions.json", {})
        self.paper = self._load_json("state/paper.json", {"krw": PAPER_START_KRW})
        self.signals = {}
        self.trade_day = ""
        self.price_history = {}  # market -> [(ts, price)] 급변동 가드용

    # ---------- 상태 저장 ----------
    @staticmethod
    def _load_json(path, default):
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return default

    @staticmethod
    def _save_json(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    def save_state(self):
        self._save_json("state/positions.json", self.positions)
        self._save_json("state/paper.json", self.paper)

    # ---------- 잔고/자산 ----------
    def krw_balance(self) -> float:
        if self.dry_run:
            return self.paper["krw"]
        return self.ex.get_balances().get("KRW", {}).get("balance", 0.0)

    def equity(self, prices: dict[str, float]) -> float:
        total = self.krw_balance()
        for market, pos in self.positions.items():
            total += pos["volume"] * prices.get(market, pos["entry_price"])
        return total

    # ---------- 거래일 ----------
    def current_trade_day(self) -> str:
        now = datetime.now(KST) - timedelta(hours=self.cfg["strategy"]["reset_hour_kst"])
        return now.strftime("%Y-%m-%d")

    def on_new_day(self, prices: dict[str, float]):
        day = self.current_trade_day()
        self.log.event("new_day", day=day)
        # 1) 전일 포지션 전량 청산
        for market in list(self.positions):
            self.sell(market, prices.get(market), reason="daily_reset")
        # 2) 신호 재계산
        self.signals = {}
        scfg = self.cfg["strategy"]
        for market in self.cfg["universe"]:
            try:
                candles = self.ex.get_daily_candles(market, count=max(30, scfg["trend_filter_ma"] + 2))
                k = scfg["per_coin_k"].get(market, scfg["k"])
                sig = compute_signal(market, candles, k, scfg["trend_filter_ma"])
                if sig:
                    self.signals[market] = sig
            except BithumbError as e:
                self.log.event("error", where="signal", market=market, error=str(e))
        # 3) 리스크 리셋
        self.risk.new_day(day, self.equity(prices))
        self.trade_day = day
        self.log.event("signals", day=day, targets={
            m: {"target": s.target_price, "trend_ok": s.trend_ok}
            for m, s in self.signals.items()})

    # ---------- 급변동 가드 ----------
    def crash_guard(self, market: str, price: float) -> bool:
        """5분 전 대비 급변동이면 True (진입 금지)."""
        now = time.time()
        hist = self.price_history.setdefault(market, [])
        hist.append((now, price))
        self.price_history[market] = [(t, p) for t, p in hist if now - t <= 360]
        old = [p for t, p in self.price_history[market] if now - t >= 240]
        if old:
            change = abs(price - old[0]) / old[0]
            return change >= self.cfg["risk"]["crash_guard_pct"]
        return False

    # ---------- 주문 ----------
    def buy(self, market: str, price: float, krw_amount: float, reason: str):
        volume = krw_amount * (1 - FEE) / price
        if self.dry_run:
            self.paper["krw"] -= krw_amount
        else:
            self.ex.buy_market(market, krw_amount)
        self.positions[market] = {"volume": volume, "entry_price": price,
                                  "krw_spent": krw_amount}
        self.risk.record_buy()
        self.save_state()
        self.log.trade(market, "buy", krw_amount, volume, price, reason, self.dry_run)
        notify.send(f"[매수] {market} {krw_amount:,.0f}원 @ {price:,.0f} ({reason})", self.tg)

    def sell(self, market: str, price: float | None, reason: str):
        pos = self.positions.pop(market, None)
        if not pos:
            return
        price = price or pos["entry_price"]
        krw_got = pos["volume"] * price * (1 - FEE)
        if self.dry_run:
            self.paper["krw"] += krw_got
        else:
            self.ex.sell_market(market, pos["volume"])
        self.save_state()
        pnl = krw_got - pos["krw_spent"]
        self.log.trade(market, "sell", krw_got, pos["volume"], price,
                       f"{reason} (손익 {pnl:+,.0f}원)", self.dry_run)
        notify.send(f"[매도] {market} @ {price:,.0f} 손익 {pnl:+,.0f}원 ({reason})", self.tg)

    def liquidate_all(self, prices: dict[str, float], reason: str):
        for market in list(self.positions):
            self.sell(market, prices.get(market), reason=reason)

    # ---------- 메인 루프 ----------
    def tick(self):
        markets = self.cfg["universe"]
        prices = self.ex.get_tickers(markets)

        if self.current_trade_day() != self.trade_day:
            self.on_new_day(prices)

        eq = self.equity(prices)
        guard = self.risk.check_equity(eq)
        if guard == "kill_switch":
            self.liquidate_all(prices, "kill_switch")
            self.log.event("kill_switch", equity=eq)
            notify.send(f"🚨 킬스위치 발동! 전량 매도 후 정지. 총자산 {eq:,.0f}원", self.tg)
            return False  # 엔진 정지
        if guard == "daily_loss_limit":
            self.log.event("daily_loss_limit", equity=eq)
            notify.send(f"⚠️ 일일 손실 한도 도달, 당일 매수 중단. 총자산 {eq:,.0f}원", self.tg)

        stop_requested = os.path.exists("STOP")

        for market, price in prices.items():
            crashed = self.crash_guard(market, price)
            pos = self.positions.get(market)
            # 손절 (STOP 파일과 무관하게 항상 동작 — 보유분 방어)
            if pos and price <= pos["entry_price"] * (1 - self.cfg["risk"]["stop_loss_pct"]):
                self.sell(market, price, reason="stop_loss")
                continue
            # 신규 진입
            if stop_requested or pos or crashed:
                continue
            sig = self.signals.get(market)
            if sig and sig.should_buy(price):
                order_krw = eq * self.cfg["risk"]["position_pct"]
                ok, why = self.risk.can_buy(len(self.positions), order_krw, self.krw_balance())
                if ok:
                    self.buy(market, price, order_krw, reason=f"breakout@{sig.target_price:,.0f}")
                else:
                    self.log.event("buy_blocked", market=market, reason=why)
        return True

    def run(self):
        mode = "페이퍼(가상)" if self.dry_run else "실전"
        self.log.event("engine_start", mode=mode)
        notify.send(f"엔진 시작 — {mode} 모드", self.tg)
        interval = self.cfg["mode"]["poll_interval_sec"]
        errors = 0
        while True:
            try:
                if not self.tick():
                    break
                # 주기적 자산 기록 (10분마다)
                if int(time.time()) % 600 < interval:
                    prices = self.ex.get_tickers(self.cfg["universe"])
                    self.log.equity(self.equity(prices), self.krw_balance(), self.positions)
                errors = 0
            except BithumbError as e:
                errors += 1
                self.log.event("error", where="tick", error=str(e), consecutive=errors)
                if errors >= 5:
                    notify.send(f"🚨 API 오류 {errors}회 연속 — 신규 주문 중단 상태로 대기: {e}", self.tg)
            except Exception as e:  # 예상 못한 오류도 엔진을 죽이지 않는다
                errors += 1
                self.log.event("error", where="unexpected", error=repr(e))
            time.sleep(interval)


if __name__ == "__main__":
    Engine(load_config()).run()
