"""전략 연구실 — 후보 전략들을 동일 조건에서 비교한다.

실행: .venv/bin/python -m backtest.lab

전부 저빈도(월 단위 리밸런싱) 전략이다. 첫 백테스트에서 고빈도 매매가
비용으로만 -60%를 낸 것이 확인됐으므로, 회전율을 낮추는 것이 1원칙이다.
"""
import yaml

from .data import load_csv
from .portfolio import run

MA_LONG = 20      # 추세 판단 이동평균 (일)
MOM_WIN = 90      # 모멘텀 관측 기간 (일)
TOP_N = 2         # 모멘텀 상위 보유 종목 수
REBAL = 7         # 리밸런싱 주기 (일) — 주 1회


def load_all(markets):
    closes, sets = {}, []
    for m in markets:
        rows = load_csv(m)
        closes[m] = {r["date"]: r["close"] for r in rows}
        sets.append(set(closes[m]))
    dates = sorted(set.union(*sets))
    return dates, closes


def series(closes, m, dates):
    """market의 종가를 dates 순서 리스트로 (없으면 직전값 유지)"""
    out, last = [], None
    for d in dates:
        last = closes[m].get(d, last)
        out.append(last)
    return out


def make_strategies(markets, dates, closes):
    px = {m: series(closes, m, dates) for m in markets}
    n = len(markets)

    def buy_hold(i):
        return {m: 1.0 / n for m in markets}

    def btc_only(i):
        return {"KRW-BTC": 1.0}

    def ma_trend(i):
        """가격이 20일 MA 위인 코인만 균등 보유, 나머지는 현금 (주 1회 판단)"""
        j = (i - 1) - ((i - 1) % REBAL)          # 최근 리밸런싱 시점
        j = max(j, MA_LONG)
        if i - 1 < MA_LONG:
            return {}
        held = []
        for m in markets:
            window = px[m][j - MA_LONG:j]
            if None in window or px[m][j] is None:
                continue
            if px[m][j] > sum(window) / MA_LONG:
                held.append(m)
        return {m: 1.0 / n for m in held}        # 균등 슬롯, 빈 슬롯은 현금

    def momentum(i):
        """90일 수익률 상위 2개 보유. 단 그 수익률이 +일 때만 (절대 모멘텀 겸용)"""
        j = (i - 1) - ((i - 1) % REBAL)
        if j < MOM_WIN:
            return {}
        scores = []
        for m in markets:
            p0, p1 = px[m][j - MOM_WIN], px[m][j]
            if p0 and p1:
                scores.append((p1 / p0 - 1, m))
        scores.sort(reverse=True)
        held = [m for s, m in scores[:TOP_N] if s > 0]
        return {m: 1.0 / TOP_N for m in held}

    def mom_ma(i):
        """모멘텀 상위 + MA 추세 필터를 둘 다 통과한 코인만"""
        a, b = momentum(i), ma_trend(i)
        held = [m for m in a if m in b]
        return {m: 1.0 / TOP_N for m in held}

    return [
        ("BuyHold-6", buy_hold),
        ("BTC-Only", btc_only),
        ("MA20-Trend", ma_trend),
        ("Momentum-Top2", momentum),
        ("Mom+MA", mom_ma),
    ]


def main():
    with open("config.yaml") as f:
        markets = yaml.safe_load(f)["universe"]
    dates, closes = load_all(markets)
    print(f"기간: {dates[0]} ~ {dates[-1]} ({len(dates)}일)\n")
    header = f"{'전략':>14} | {'총수익':>8} | {'연복리':>7} | {'MDD':>6} | {'샤프':>5} | {'회전':>6} | {'비용':>6} | {'투자일':>5}"
    print(header)
    print("-" * len(header))
    for name, fn in make_strategies(markets, dates, closes):
        r = run(name, dates, closes, fn)
        print(f"{r.name:>14} | {r.total_return_pct:+7.1f}% | {r.cagr_pct:+6.1f}% | "
              f"{r.mdd_pct:5.1f}% | {r.sharpe:5.2f} | {r.turnover:5.1f}x | "
              f"{r.cost_paid_pct:5.1f}% | {r.days_in_market_pct:4.0f}%")


if __name__ == "__main__":
    main()
