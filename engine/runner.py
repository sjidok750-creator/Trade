"""자동매매 실행 엔진 (24시간 상시 실행).

실행: python -m engine.runner
정지: 프로젝트 루트에 STOP 파일 생성 (touch STOP) → 신규 주문 즉시 중단

dry_run=true(기본)이면 실주문 없이 가상 체결로 동작한다.
"""
import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import yaml

from exchange.bithumb import Bithumb, BithumbError
from strategies import ma_trend
from strategies.volatility_breakout import compute_signal
from . import fee_guard, notify, report
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
        self.last_report = self._load_json("state/report.json", {}).get("slot", "")

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
        scfg = self.cfg["strategy"]
        if scfg["name"] == "volatility_breakout":
            # 전일 포지션 전량 청산 후 신호 재계산
            for market in list(self.positions):
                self.sell(market, prices.get(market), reason="daily_reset")
            self.signals = {}
            for market in self.cfg["universe"]:
                try:
                    candles = self.ex.get_daily_candles(market, count=max(30, scfg["trend_filter_ma"] + 2))
                    k = scfg["per_coin_k"].get(market, scfg["k"])
                    sig = compute_signal(market, candles, k, scfg["trend_filter_ma"])
                    if sig:
                        self.signals[market] = sig
                except BithumbError as e:
                    self.log.event("error", where="signal", market=market, error=str(e))
            self.log.event("signals", day=day, targets={
                m: {"target": s.target_price, "trend_ok": s.trend_ok}
                for m, s in self.signals.items()})
        self.risk.new_day(day, self.equity(prices))
        self.trade_day = day
        warn = fee_guard.expiry_warning(self.cfg.get("fee", {}).get("coupon_renewed_on"))
        if warn:
            self.log.event("fee_coupon", message=warn)
            notify.send(warn, self.tg)
        if scfg["name"] == "ma_trend":
            self.rebalance_trend(prices)

    def rebalance_trend(self, prices: dict[str, float]):
        """MA 추세추종: rebal_days마다 코인별 추세 상태를 갱신하고
        목표 보유군에 맞춰 매도/매수한다. 보유 중인 포지션 크기는 건드리지 않는다."""
        scfg = self.cfg["strategy"]
        states = self._load_json("state/trend.json", {})
        last = states.get("_last_rebal", "")
        if last:
            elapsed = (date.fromisoformat(self.trade_day) - date.fromisoformat(last)).days
            if elapsed < scfg.get("rebal_days", 7):
                return
        universe = self.cfg["universe"]
        new_states = {}
        for m in universe:
            try:
                candles = self.ex.get_daily_candles(m, count=scfg["ma_len"] + 3)
                new_states[m] = ma_trend.update_state(
                    candles, scfg["ma_len"], scfg["band"], bool(states.get(m, False)))
            except BithumbError as e:
                self.log.event("error", where="trend", market=m, error=str(e))
                new_states[m] = bool(states.get(m, False))
        new_states["_last_rebal"] = self.trade_day
        self._save_json("state/trend.json", new_states)

        targets = [m for m in universe if new_states.get(m)]
        self.log.event("trend_rebalance", day=self.trade_day, targets=targets)
        # 1) 추세가 꺾인 보유분 매도
        for m in list(self.positions):
            if m not in targets:
                self.sell(m, prices.get(m), reason="trend_exit")
        # 2) 새로 추세에 오른 코인 매수 (슬롯 = 총자산/유니버스, 수수료 여유 2%)
        eq = self.equity(prices)
        slot = eq / len(universe) * 0.98
        for m in targets:
            if m in self.positions or m not in prices:
                continue
            if self.crash_guard(m, prices[m]):
                self.log.event("buy_blocked", market=m, reason="급변동 가드")
                continue
            ok, why = self.risk.can_buy(len(self.positions), slot, self.krw_balance())
            if ok:
                self.buy(m, prices[m], slot, reason=f"trend_entry MA{scfg['ma_len']}")
            else:
                self.log.event("buy_blocked", market=m, reason=why)

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
            order = self.ex.buy_market(market, krw_amount)
            anomaly = fee_guard.fee_anomaly(order)
            if anomaly:
                self.log.event("fee_anomaly", market=market, message=anomaly)
                notify.send(anomaly, self.tg)
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

    def maybe_report(self, equity: float, prices: dict[str, float]):
        """한국시간 지정 시각마다 현황 리포트를 텔레그램으로 보낸다."""
        slot = report.due(self.last_report)
        if not slot:
            return
        mode = "페이퍼" if self.dry_run else "실전"
        trend = self._load_json("state/trend.json", {})
        msg = report.build(mode, equity, PAPER_START_KRW, self.krw_balance(),
                           self.positions, prices, trend, self.cfg["universe"])
        notify.send(msg, self.tg)
        self.log.event("report", slot=slot, equity=equity)
        self.last_report = slot
        self._save_json("state/report.json", {"slot": slot})

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

        self.maybe_report(eq, prices)

        stop_requested = os.path.exists("STOP")

        for market, price in prices.items():
            crashed = self.crash_guard(market, price)
            pos = self.positions.get(market)
            # 손절 (STOP 파일과 무관하게 항상 동작 — 보유분 방어)
            if pos and price <= pos["entry_price"] * (1 - self.cfg["risk"]["stop_loss_pct"]):
                self.sell(market, price, reason="stop_loss")
                continue
            # 신규 진입 (변동성 돌파 전략 전용 — 추세 전략은 리밸런싱에서만 매매)
            if self.cfg["strategy"]["name"] != "volatility_breakout":
                continue
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
