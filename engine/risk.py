"""리스크 가드 — 전략보다 우선한다.

모든 매수 주문은 이 모듈의 검사를 통과해야 하며,
킬스위치/일일 한도는 전략 로직이 무엇을 원하든 강제된다.
"""
import json
import os
from dataclasses import dataclass, field
from datetime import date


@dataclass
class RiskState:
    day: str = ""                 # 현재 거래일 (KST)
    day_start_equity: float = 0.0 # 거래일 시작 시점 총자산
    peak_equity: float = 0.0      # 역대 최고 총자산 (킬스위치 기준)
    daily_buys: int = 0           # 당일 매수 횟수
    halted: bool = False          # 킬스위치 발동 여부
    daily_halted: bool = False    # 당일 매수 중단 여부

    def to_dict(self):
        return self.__dict__.copy()


class RiskManager:
    def __init__(self, cfg: dict, state_path: str = "state/risk.json"):
        self.cfg = cfg
        self.state_path = state_path
        self.state = self._load()

    def _load(self) -> RiskState:
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                return RiskState(**json.load(f))
        return RiskState()

    def save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump(self.state.to_dict(), f)

    def new_day(self, today: str, equity: float):
        """거래일 리셋 시 호출."""
        self.state.day = today
        self.state.day_start_equity = equity
        self.state.daily_buys = 0
        self.state.daily_halted = False
        self.state.peak_equity = max(self.state.peak_equity, equity)
        self.save()

    def check_equity(self, equity: float) -> str | None:
        """주기적으로 호출. 발동한 가드 이름을 반환 (없으면 None)."""
        s = self.state
        s.peak_equity = max(s.peak_equity, equity)
        if s.peak_equity > 0 and equity <= s.peak_equity * (1 - self.cfg["kill_switch_pct"]):
            s.halted = True
            self.save()
            return "kill_switch"
        if (not s.daily_halted and s.day_start_equity > 0
                and equity <= s.day_start_equity * (1 - self.cfg["daily_loss_limit_pct"])):
            s.daily_halted = True
            self.save()
            return "daily_loss_limit"
        return None

    def can_buy(self, open_positions: int, order_krw: float, krw_balance: float) -> tuple[bool, str]:
        s = self.state
        if s.halted:
            return False, "킬스위치 발동 상태"
        if s.daily_halted:
            return False, "일일 손실 한도 도달"
        if s.daily_buys >= self.cfg["max_daily_buys"]:
            return False, "일일 매수 횟수 상한"
        if open_positions >= self.cfg["max_positions"]:
            return False, "동시 보유 한도"
        if order_krw < self.cfg["min_order_krw"]:
            return False, "최소 주문 금액 미달"
        if order_krw > krw_balance:
            return False, "원화 잔고 부족"
        return True, ""

    def record_buy(self):
        self.state.daily_buys += 1
        self.save()
