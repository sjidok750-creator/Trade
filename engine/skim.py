"""이익 분리(스킴) — 목표 수익 도달 시 그만큼만 현금으로 떼어 출금을 대기시킨다.

빗썸 계좌의 목적은 '이익확정'이다. 원금은 계속 굴리되, 누적 이익이 목표에
닿으면 그 금액만큼만 매도해 확정 현금으로 분리하고 사용자에게 출금을 알린다.
사용자가 출금 후 /settle 을 보내면 기준선을 갱신하고 다음 사이클로 넘어간다.

설계 원칙
- 추세를 타는 포지션을 통째로 끊지 않는다. 이익분만 비례 축소한다.
- 분리된 현금(reserve)은 투자 자산에서 제외해 재매수에 쓰이지 않게 한다.
- 출금은 사람이 한다. 시스템은 계산·분리·알림까지만 한다.
"""
from dataclasses import dataclass


@dataclass
class SkimPlan:
    amount: float                 # 확정할 금액 (원)
    sell: dict                    # {market: 매도할 원화 금액}
    from_cash: float              # 보유 현금에서 충당하는 금액


def pending(baseline: float, investable: float, target: float,
            min_order: float) -> float:
    """확정 대상 금액. 목표 미달이거나 최소 주문 금액에 못 미치면 0.

    baseline:   직전 정산 시점의 투자자산 (이 위로 번 것이 이익)
    investable: 현재 투자자산 (확정 대기 현금 제외)
    target:     목표 이익 (원)
    """
    profit = investable - baseline
    if profit < target:
        return 0.0
    # 목표의 배수만큼 쌓였으면 그만큼 한 번에 확정한다
    amount = (profit // target) * target
    return amount if amount >= min_order else 0.0


def plan(amount: float, cash: float, positions: dict, prices: dict,
         min_order: float) -> SkimPlan | None:
    """확정 금액을 현금 우선, 부족분은 보유 비중에 비례해 매도로 조달한다.

    positions: {market: {"volume": float, "entry_price": float, ...}}
    prices:    {market: 현재가}
    """
    if amount <= 0:
        return None
    from_cash = min(cash, amount)
    remain = amount - from_cash
    sell = {}

    if remain > 0:
        values = {m: p["volume"] * prices[m]
                  for m, p in positions.items() if m in prices}
        total = sum(values.values())
        if total <= 0:
            return None
        if remain > total:            # 보유분을 다 팔아도 모자라면 확정 보류
            return None
        for m, v in values.items():
            part = remain * (v / total)
            if part >= min_order:     # 자투리 주문은 건너뛴다
                sell[m] = part
        raised = sum(sell.values())
        if raised <= 0:
            return None
        # 자투리 제외로 줄어든 만큼 실제 확정액을 낮춘다
        amount = from_cash + raised

    return SkimPlan(amount=amount, sell=sell, from_cash=from_cash)


def message(amount: float, total_settled: float, baseline: float) -> str:
    return (f"💰 이익 {amount:,.0f}원을 확정했습니다.\n"
            f"빗썸에서 출금하신 뒤 /settle 을 보내주세요.\n\n"
            f"확정 대기 누적: {total_settled:,.0f}원\n"
            f"운용 원금 기준: {baseline:,.0f}원")
