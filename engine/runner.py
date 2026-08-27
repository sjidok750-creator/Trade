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
from . import commands, fee_guard, notify, report, skim
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
        self.tg_offset = self._load_json("state/telegram.json", {}).get("offset", 0)
        self.settle = self._load_json(
            "state/settle.json",
            {"baseline": PAPER_START_KRW, "reserve": 0.0, "cycles": 0})

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
        """투자에 쓰이는 자산. 확정 대기 현금(reserve)은 제외한다."""
        return self.total_equity(prices) - self.settle.get("reserve", 0.0)

    def total_equity(self, prices: dict[str, float]) -> float:
        total = self.krw_balance()
        for market, pos in self.positions.items():
            total += pos["volume"] * prices.get(market, pos["entry_price"])
        return total

    def investable_cash(self) -> float:
        """확정 대기분을 뺀 가용 현금."""
        return max(0.0, self.krw_balance() - self.settle.get("reserve", 0.0))

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
            ok, why = self.risk.can_buy(len(self.positions), slot, self.investable_cash())
            if ok:
                self.buy(m, prices[m], slot, reason=f"trend_entry MA{scfg['ma_len']}")
            else:
                self.log.event("buy_blocked", market=m, reason=why)
        self.skim_profit(prices)

    def skim_profit(self, prices: dict[str, float]):
        """목표 이익 도달 시 그만큼만 현금화해 출금 대기로 분리한다."""
        cfg = self.cfg.get("settle", {})
        target = cfg.get("target_krw", 0)
        if not target:
            return
        min_order = self.cfg["risk"]["min_order_krw"]
        amount = skim.pending(self.settle["baseline"], self.equity(prices),
                              target, min_order)
        if not amount:
            return
        p = skim.plan(amount, self.investable_cash(), self.positions, prices, min_order)
        if not p:
            self.log.event("skim_deferred", wanted=amount, reason="조달 불가")
            return

        for market, krw in p.sell.items():
            pos = self.positions[market]
            portion = min(1.0, krw / (pos["volume"] * prices[market]))
            self.sell_partial(market, prices[market], portion, reason="profit_skim")

        self.settle["reserve"] += p.amount
        self.settle["cycles"] += 1
        self.risk.withdraw(p.amount)      # 출금은 손실이 아니다 — 낙폭 기준 보정
        self.settle["baseline"] = self.equity(prices)
        self._save_json("state/settle.json", self.settle)
        self.log.event("skim", amount=p.amount, reserve=self.settle["reserve"],
                       baseline=self.settle["baseline"])
        notify.send(skim.message(p.amount, self.settle["reserve"],
                                 self.settle["baseline"]), self.tg)

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

    def _sellable_volume(self, market: str, want: float) -> float:
        """실전 매도 직전 실제 보유 수량으로 보정한다.

        시장가 매수는 원화 금액 지정이라 실제 체결 수량이 기록과 미세하게
        다르다. 기록된 수량 그대로 팔면 잔고 부족으로 주문이 거부될 수 있다.
        """
        if self.dry_run:
            return want
        try:
            cur = market.split("-", 1)[1]
            avail = self.ex.get_balances().get(cur, {}).get("balance", 0.0)
        except BithumbError as e:
            self.log.event("error", where="sellable", market=market, error=str(e))
            return want
        if avail < want * 0.99:
            self.log.event("volume_clamp", market=market, recorded=want, actual=avail)
        return min(want, avail)

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
        volume = self._sellable_volume(market, pos["volume"])
        krw_got = volume * price * (1 - FEE)
        if self.dry_run:
            self.paper["krw"] += krw_got
        else:
            self.ex.sell_market(market, volume)
        self.save_state()
        pnl = krw_got - pos["krw_spent"]
        self.log.trade(market, "sell", krw_got, pos["volume"], price,
                       f"{reason} (손익 {pnl:+,.0f}원)", self.dry_run)
        notify.send(f"[매도] {market} @ {price:,.0f} 손익 {pnl:+,.0f}원 ({reason})", self.tg)

    def sell_partial(self, market: str, price: float, portion: float, reason: str):
        """보유 수량의 일부만 매도한다 (이익 분리용). portion: 0~1"""
        pos = self.positions.get(market)
        if not pos or portion <= 0:
            return
        if portion >= 1.0:
            self.sell(market, price, reason)
            return
        volume = self._sellable_volume(market, pos["volume"] * portion)
        krw_got = volume * price * (1 - FEE)
        if self.dry_run:
            self.paper["krw"] += krw_got
        else:
            self.ex.sell_market(market, volume)
        # 남은 포지션의 원가도 같은 비율로 줄여 손익 계산을 유지한다
        pos["volume"] -= volume
        pos["krw_spent"] *= (1 - portion)
        self.save_state()
        self.log.trade(market, "sell", krw_got, volume, price, reason, self.dry_run)

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
        msg = report.build(mode, equity, PAPER_START_KRW, self.investable_cash(),
                           self.positions, prices, trend, self.cfg["universe"],
                           self.settle.get("reserve", 0.0),
                           self.settle.get("cycles", 0))
        notify.send(msg, self.tg)
        self.log.event("report", slot=slot, equity=equity)
        self.last_report = slot
        self._save_json("state/report.json", {"slot": slot})

    def confirm_settled(self) -> float:
        """사용자가 출금을 마쳤음을 확인 — 확정 대기액을 비운다."""
        done = self.settle.get("reserve", 0.0)
        if done > 0:
            if self.dry_run:      # 페이퍼에서는 가상 잔고에서도 실제로 빼준다
                self.paper["krw"] = max(0.0, self.paper["krw"] - done)
                self.save_state()
            self.settle["reserve"] = 0.0
            self._save_json("state/settle.json", self.settle)
            self.log.event("settled", amount=done)
        return done

    def poll_commands(self, prices: dict[str, float]):
        """텔레그램 명령을 확인하고 응답한다 (등록된 사용자만)."""
        if not self.tg:
            return
        eq = self.equity(prices)
        ctx = {
            "report": lambda: report.build(
                "페이퍼" if self.dry_run else "실전", eq, PAPER_START_KRW,
                self.investable_cash(), self.positions, prices,
                self._load_json("state/trend.json", {}), self.cfg["universe"],
                self.settle.get("reserve", 0.0), self.settle.get("cycles", 0)),
            "positions": self.positions,
            "prices": prices,
            "trend": self._load_json("state/trend.json", {}),
            "universe": self.cfg["universe"],
            "stop_path": "STOP",
            "settle_done": self.confirm_settled,
        }
        offset = commands.poll(ctx, self.tg_offset)
        if offset != self.tg_offset:
            self.tg_offset = offset
            self._save_json("state/telegram.json", {"offset": offset})

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
        self.poll_commands(prices)

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
        if self.tg:
            problem = commands.config_problem()
            if problem:
                self.log.event("telegram_config", problem=problem)
                print(f"[텔레그램 설정 경고] {problem}", flush=True)
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
