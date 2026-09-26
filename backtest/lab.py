"""전략 연구실 — 후보 전략들을 동일 조건에서 비교한다.

실행: .venv/bin/python -m backtest.lab             # 후보 비교
      .venv/bin/python -m backtest.lab robust      # MA추세 강건성 스윕
      .venv/bin/python -m backtest.lab aggressive  # 공격형 후보 비교 (확장 유니버스 자동 수집)
      .venv/bin/python -m backtest.lab concentrate # 추세 코인 몰아주기 강건성·비용 민감도

전부 저빈도(월 단위 리밸런싱) 전략이다. 첫 백테스트에서 고빈도 매매가
비용으로만 -60%를 낸 것이 확인됐으므로, 회전율을 낮추는 것이 1원칙이다.
"""
import os
import sys

import yaml

from .data import download, load_csv
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


# ---------- 공격형 후보 ----------
# 빗썸 원화마켓 중 3년 가까운 이력과 거래량이 있는 코인들 (현행 6개 + 10개)
EXTRA = ["KRW-TRX", "KRW-LINK", "KRW-AVAX", "KRW-DOT", "KRW-BCH",
         "KRW-XLM", "KRW-HBAR", "KRW-SUI", "KRW-ETC", "KRW-SHIB"]
COST_THIN = 0.006   # 확장 유니버스 왕복 비용 — 얇은 호가(DOGE 실측 -0.79%)를 반영해 보수적으로


def ensure_data(markets, days=1095):
    """없는 코인 일봉만 받는다 (공개 API라 키 불필요). 실패한 코인은 제외."""
    ok = []
    for m in markets:
        if not os.path.exists(os.path.join("data", f"{m}.csv")):
            try:
                download(m, days)
            except Exception as e:  # 상장 폐지·미상장 등
                print(f"{m}: 수집 실패로 제외 ({e})")
                continue
        ok.append(m)
    return ok


def ranked_trend_factory(markets, px, ma_len=30, band=0.03, rebal=REBAL,
                         top_n=None, mom_win=60, cap=None):
    """MA 추세 통과 코인 중에서 비중을 몰아준다.

    top_n=None : 추세 통과 코인 전부를 1/k로 (k=통과 개수) — 현금 비중 제거
    top_n=N    : 추세 통과 코인 중 mom_win일 수익률 상위 N개를 각 1/N
                 (통과가 N개 미만이면 나머지는 현금)
    cap        : 한 코인 최대 비중 (1/k 방식에서 한 코인 몰빵 방지)
    """
    trend = ma_trend_factory(markets, px, ma_len, band, rebal)
    cache = {"j": None, "w": {}}

    def fn(i):
        held = trend(i)                       # 추세 통과 코인 (균등 비중)
        j = (i - 1) - ((i - 1) % rebal)
        if cache["j"] == j:
            return cache["w"]
        cache["j"] = j
        if not held:
            cache["w"] = {}
        elif top_n is None:
            w = 1.0 / len(held)
            if cap:
                w = min(w, cap)
            cache["w"] = {m: w for m in held}
        else:
            scores = []
            for m in held:
                if j - mom_win >= 0 and px[m][j - mom_win] and px[m][j]:
                    scores.append((px[m][j] / px[m][j - mom_win] - 1, m))
            scores.sort(reverse=True)
            cache["w"] = {m: 1.0 / top_n for _, m in scores[:top_n]}
        return cache["w"]

    return fn


def yearly(r, dates):
    """연도별 수익률 — 특정 해(상승장)에만 몰린 성과인지 확인한다."""
    out, start_v, year = [], r.equity[0], dates[0][:4]
    for d, v, pv in zip(dates[1:], r.equity[1:], r.equity[:-1]):
        if d[:4] != year:
            out.append(f"{year}:{(pv / start_v - 1) * 100:+.0f}%")
            start_v, year = pv, d[:4]
    out.append(f"{year}:{(r.equity[-1] / start_v - 1) * 100:+.0f}%")
    return "  ".join(out)


def aggressive(base):
    """현행(MA30 균등) 대비 '더 공격적인' 후보를 같은 조건에서 비교한다.

    공격성을 높이는 레버는 세 가지뿐이다 (현물·레버리지 없음 전제):
      1) 현금 비중 제거 — 추세 코인에 1/k로 몰기
      2) 집중 — 추세 코인 중 모멘텀 상위 N개만
      3) 유니버스 확장 — 더 많이 오르는 코인을 잡을 기회
    """
    wide = base + [m for m in ensure_data(EXTRA) if m not in base]
    header = f"{'전략':>22} | {'총수익':>8} | {'연복리':>7} | {'MDD':>6} | {'샤프':>5} | {'회전':>6} | {'비용':>6} | {'투자일':>5}"
    keep = []

    for label, markets, cost in ((f"현행 {len(base)}코인", base, 0.0028),
                                 (f"확장 {len(wide)}코인", wide, COST_THIN)):
        dates, closes = load_all(markets)
        px = {m: series(closes, m, dates) for m in markets}
        print(f"== {label} / 왕복비용 {cost*100:.2f}% / {dates[0]} ~ {dates[-1]} ==")
        print(header)
        print("-" * len(header))
        cands = [("BuyHold", lambda i, ms=markets: {m: 1.0 / len(ms) for m in ms}),
                 ("MA30 균등(현행)", ma_trend_factory(markets, px, 30, 0.03))]
        cands.append(("MA30 1/k 최대50%", ranked_trend_factory(markets, px, cap=0.5)))
        cands.append(("MA30 1/k 최대34%", ranked_trend_factory(markets, px, cap=0.34)))
        for n in (3, 2, 1):
            for win in (30, 60, 90):
                cands.append((f"MA30+모멘텀{win}일 상위{n}",
                              ranked_trend_factory(markets, px, top_n=n, mom_win=win)))
        for name, fn in cands:
            r = run(name, dates, closes, fn, cost=cost)
            print(ROW.replace(">14", ">22").format(name=r.name, r=r))
            keep.append((label, r, dates))
        print()

    print("== 연도별 수익률 (한 해에만 몰렸으면 신뢰도 낮음) ==")
    for label, r, dates in keep:
        if r.name in ("BuyHold", "MA30 균등(현행)", "MA30 1/k 최대50%") or "60일 상위" in r.name:
            print(f"{label:>10} {r.name:>22} | {yearly(r, dates)}")


def concentrate(markets):
    """'추세 코인에 몰아주기'가 특정 숫자·낮은 비용 가정에만 맞은 건지 확인한다.

    회전율이 현행의 2배라 비용 가정이 중요하다. 실측 체결(수수료 0.04% +
    평균 슬리피지 0.21%)은 편도 약 0.25% → 왕복 0.5%로, 기본 가정(0.28%)의 2배다.
    """
    dates, closes = load_all(markets)
    px = {m: series(closes, m, dates) for m in markets}
    R = ROW.replace(">14", ">24")
    print(f"기간: {dates[0]} ~ {dates[-1]}\n")
    for cost in (0.0028, 0.005):
        print(f"== 왕복비용 {cost*100:.2f}% ==")
        for ma in (20, 30, 50):
            rows = [(f"MA{ma} 균등(현행)", ma_trend_factory(markets, px, ma, 0.03))]
            for cap in (0.34, 0.5, 1.0):
                rows.append((f"MA{ma} 1/k 최대{cap*100:.0f}%",
                             ranked_trend_factory(markets, px, ma, cap=cap)))
            for name, fn in rows:
                r = run(name, dates, closes, fn, cost=cost)
                print(R.format(name=r.name, r=r) + "  | " + yearly(r, dates))
            print()
        for win in (20, 30, 45):
            fn = ranked_trend_factory(markets, px, 30, top_n=3, mom_win=win)
            r = run(f"MA30+모멘텀{win}일 상위3", dates, closes, fn, cost=cost)
            print(R.format(name=r.name, r=r) + "  | " + yearly(r, dates))
        print()


def main():
    with open("config.yaml") as f:
        markets = yaml.safe_load(f)["universe"]
    if len(sys.argv) > 1 and sys.argv[1] == "concentrate":
        return concentrate(ensure_data(markets))
    if len(sys.argv) > 1 and sys.argv[1] == "aggressive":
        return aggressive(ensure_data(markets))
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
