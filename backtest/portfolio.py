"""포트폴리오 백테스터 — 전략을 '목표 비중'으로 표현해 공정하게 비교한다.

기존 백테스터는 거래 단위로만 돌아 Buy&Hold 같은 기준선과 비교가 불가능했다.
여기서는 모든 전략을 "매일 각 코인을 자산의 몇 %로 들고 갈지"로 통일한다.
비용은 비중 변화량(회전율)에만 부과되므로, 안 움직이면 비용이 0이다.

미래 참조 방지: t일의 비중은 t-1일 종가까지의 정보로만 결정하고,
t-1일 종가 → t일 종가 수익률에 적용한다.
"""
from dataclasses import dataclass, field

COST = 0.0028  # 편도 0.14%(수수료 0.04 + 슬리피지 0.1) × 왕복 근사


@dataclass
class Result:
    name: str
    total_return_pct: float
    cagr_pct: float
    mdd_pct: float
    sharpe: float
    turnover: float          # 누적 회전율 (1.0 = 전 자산 한 번 교체)
    cost_paid_pct: float     # 비용으로 나간 누적 비율
    days_in_market_pct: float
    equity: list = field(default_factory=list, repr=False)


def _metrics(name, equity, turnover, cost_paid, in_market_days, n_days):
    start, end = equity[0], equity[-1]
    peak, mdd = start, 0.0
    rets = []
    for i, v in enumerate(equity):
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
        if i:
            rets.append(equity[i] / equity[i - 1] - 1)
    years = n_days / 365.25
    cagr = ((end / start) ** (1 / years) - 1) * 100 if years > 0 and end > 0 else -100.0
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean) ** 2 for r in rets) / len(rets) if rets else 0.0
    sharpe = (mean / var ** 0.5 * (365 ** 0.5)) if var > 0 else 0.0
    return Result(name, (end / start - 1) * 100, cagr, mdd * 100, sharpe,
                  turnover, cost_paid / start * 100,
                  in_market_days / n_days * 100, equity)


def run(name, dates, closes, weight_fn, start_capital=1_000_000.0, cost=COST,
        take_profit=0.0, cooldown=0):
    """weight_fn(i) -> {market: weight}  — i일의 목표 비중 (i-1일까지 정보로 결정)

    closes: {market: {date: close}}
    take_profit: 0보다 크면 '익절 후 재진입' 규칙을 적용한다.
        직전 정산 시점 대비 자산이 이 비율만큼 늘면 전량 현금화하고 기준을
        갱신한다. cooldown일 동안 재진입을 막은 뒤 전략 신호를 다시 따른다.
        (예: 0.02 = 순수익 2% — 100만원 기준 2만원마다 정산)
    """
    equity = [start_capital]
    cur = {}                 # 현재 비중
    cost_paid = 0.0
    turnover = 0.0
    in_market = 0
    baseline = start_capital  # 익절 기준 자산
    wait = 0                  # 재진입 대기 잔여일

    for i in range(1, len(dates)):
        d_prev, d_now = dates[i - 1], dates[i]
        target = weight_fn(i) or {}

        if take_profit > 0:
            if wait > 0:                     # 정산 직후 대기 중 → 현금 유지
                target = {}
                wait -= 1
            elif equity[-1] >= baseline * (1 + take_profit):
                target = {}                  # 목표 달성 → 전량 정산
                baseline = equity[-1]
                wait = cooldown

        # 리밸런싱 비용: 비중 변화량 절반씩 사고팔므로 |Δ| 합에 편도 비용
        markets = set(cur) | set(target)
        delta = sum(abs(target.get(m, 0.0) - cur.get(m, 0.0)) for m in markets)
        fee = equity[-1] * delta * (cost / 2)
        turnover += delta / 2
        cost_paid += fee
        cur = {m: w for m, w in target.items() if w > 0}

        # 하루 보유 수익
        capital = equity[-1] - fee
        growth = 0.0
        invested = 0.0
        for m, w in cur.items():
            p0 = closes[m].get(d_prev)
            p1 = closes[m].get(d_now)
            if not p0 or not p1:
                growth += w          # 데이터 없으면 현금으로 간주
                continue
            growth += w * (p1 / p0)
            invested += w
        growth += max(0.0, 1.0 - sum(cur.values()))   # 나머지는 현금
        equity.append(capital * growth)
        if invested > 0:
            in_market += 1

    return _metrics(name, equity, turnover, cost_paid, in_market, len(dates))
