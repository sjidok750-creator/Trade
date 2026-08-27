"""전략 연구실 — 후보 전략들을 동일 조건에서 비교한다.

실행: .venv/bin/python -m backtest.lab          # 후보 비교
      .venv/bin/python -m backtest.lab robust   # MA추세 강건성 스윕

전부 저빈도(월 단위 리밸런싱) 전략이다. 첫 백테스트에서 고빈도 매매가
비용으로만 -60%를 낸 것이 확인됐으므로, 회전율을 낮추는 것이 1원칙이다.
"""
import sys

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


def ma_trend_factory(markets, px, ma_len, band=0.0, rebal=REBAL):
    """이평선 추세추종 생성기.

    band: 휩쏘 방지 이력대 — 가격이 MA*(1+band) 위로 가야 진입,
          MA*(1-band) 아래로 내려와야 이탈. 그 사이에서는 직전 상태 유지.
    """
    n = len(markets)
    state = {m: False for m in markets}
    last_j = -1

    def fn(i):
        nonlocal last_j
        j = (i - 1) - ((i - 1) % rebal)
        if j < ma_len:
            return {}
        if j != last_j:
            last_j = j
            for m in markets:
                window = px[m][j - ma_len:j]
                if None in window or px[m][j] is None:
                    state[m] = False
                    continue
                ma = sum(window) / ma_len
                if px[m][j] > ma * (1 + band):
                    state[m] = True
                elif px[m][j] < ma * (1 - band):
                    state[m] = False
        return {m: 1.0 / n for m in markets if state[m]}

    return fn


def make_strategies(markets, dates, closes):
    px = {m: series(closes, m, dates) for m in markets}
    n = len(markets)

    def buy_hold(i):
        return {m: 1.0 / n for m in markets}

    def btc_only(i):
        return {"KRW-BTC": 1.0}

    ma_trend = ma_trend_factory(markets, px, MA_LONG)

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


ROW = ("{name:>14} | {r.total_return_pct:+7.1f}% | {r.cagr_pct:+6.1f}% | "
       "{r.mdd_pct:5.1f}% | {r.sharpe:5.2f} | {r.turnover:5.1f}x | "
       "{r.cost_paid_pct:5.1f}% | {r.days_in_market_pct:4.0f}%")


def robust(markets, dates, closes):
    """MA 기간 × 밴드 × 주기 스윕 — 20일이 우연인지 확인한다."""
    px = {m: series(closes, m, dates) for m in markets}
    print("MA추세 강건성 스윕 (모든 칸이 고르게 좋아야 신뢰 가능)\n")
    for rebal in (7, 3):
        for band in (0.0, 0.01, 0.03):
            print(f"-- 리밸 {rebal}일 / 밴드 {band*100:.0f}% --")
            for ma in (10, 20, 30, 50, 100):
                fn = ma_trend_factory(markets, px, ma, band, rebal)
                r = run(f"MA{ma}", dates, closes, fn)
                print(ROW.format(name=r.name, r=r))
            print()


def takeprofit(markets, dates, closes):
    """익절 후 재진입 규칙을 여러 목표치로 검증한다.

    "순수익 N원마다 정산하고 다시 거래" 아이디어의 실제 효과를 측정한다.
    기준은 익절 없는 현행 전략(MA30/밴드3%/주1회).
    """
    px = {m: series(closes, m, dates) for m in markets}
    fn = ma_trend_factory(markets, px, 30, 0.03, 7)
    print("익절 규칙 비교 — 기준: MA30 추세추종 (100만원 운용 가정)\n")
    print(ROW.format(name="익절없음", r=run("익절없음", dates, closes, fn)))
    for tp, label in ((0.02, "2%=2만원"), (0.05, "5%=5만원"), (0.10, "10%=10만원")):
        for cd in (0, 7):
            name = f"{label}/{cd}일대기"
            r = run(name, dates, closes, fn, take_profit=tp, cooldown=cd)
            print(ROW.format(name=name, r=r))


def main():
    with open("config.yaml") as f:
        markets = yaml.safe_load(f)["universe"]
    dates, closes = load_all(markets)
    print(f"기간: {dates[0]} ~ {dates[-1]} ({len(dates)}일)\n")
    if len(sys.argv) > 1 and sys.argv[1] == "robust":
        return robust(markets, dates, closes)
    if len(sys.argv) > 1 and sys.argv[1] == "takeprofit":
        return takeprofit(markets, dates, closes)
    header = f"{'전략':>14} | {'총수익':>8} | {'연복리':>7} | {'MDD':>6} | {'샤프':>5} | {'회전':>6} | {'비용':>6} | {'투자일':>5}"
    print(header)
    print("-" * len(header))
    for name, fn in make_strategies(markets, dates, closes):
        r = run(name, dates, closes, fn)
        print(ROW.format(name=r.name, r=r))


if __name__ == "__main__":
    main()
