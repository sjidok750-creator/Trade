"""능동 매매 연구실 — 차트를 읽고 자주 매매해서 수수료를 이길 수 있는지 검증한다.

실행: .venv/bin/python -m backtest.active          # 1시간봉 1년치 자동 수집 후 비교
      .venv/bin/python -m backtest.active 540      # 수집 일수 지정

1차 전략(변동성 돌파)이 비용으로 -60%를 낸 원인은 '시장가 체결 + 슬리피지'였다.
여기서는 진입·익절을 **지정가**로 걸어 슬리피지를 없애고(수수료 0.04%만),
손절·강제청산만 시장가(수수료 + 슬리피지 0.2%)로 계산한다.
지정가는 가격이 그 값을 '뚫고 지나가야'(저가 < 지정가) 체결로 본다 — 보수적.

과최적화 방지(워크포워드): 앞 2/3 기간에서 파라미터를 고르고,
**한 번도 안 본 뒤 1/3 기간** 성과로만 판정한다. 뒤 1/3에서
보유(Hold)·현행 MA30보다 나아야 실전 후보다.

후보 (모두 롱 전용 — 빗썸 현물은 공매도가 없다):
  A. 눌림목: 일봉 MA30 위(상승 추세)일 때만, 1시간 RSI 과매도에서 지정가 매수 → 지정가 익절
  B. 그리드: 상승 추세일 때만, g% 간격 하락마다 1칸 매수 → g% 반등 시 매도 (변동성 수확)
  C. 4시간 돌파: 4시간봉 N개 최고가 돌파 시 매수, M개 최저가 이탈 시 매도
"""
import csv
import os
import sys
import time
from datetime import datetime

import yaml

from exchange.bithumb import Bithumb

FEE = 0.0004         # 쿠폰 수수료 (편도)
SLIP = 0.002         # 시장가 체결 슬리피지 (실측 평균 -0.21%)
FIELDS = ["ts", "open", "high", "low", "close"]


# ---------- 데이터 ----------
def download_hourly(market, days=365, out_dir="data"):
    ex = Bithumb()
    rows, to = [], None
    while len(rows) < days * 24:
        batch = ex.get_minute_candles(market, 60, 200, to)
        if not batch:
            break
        for c in batch:
            rows.append({"ts": c["candle_date_time_kst"], "open": c["opening_price"],
                         "high": c["high_price"], "low": c["low_price"],
                         "close": c["trade_price"]})
        to = batch[-1]["candle_date_time_utc"]
        time.sleep(0.12)
    rows = rows[:days * 24][::-1]
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{market}_60m.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"{market}: 1시간봉 {len(rows)}개 저장")
    return path


def load_hourly(market, days, data_dir="data"):
    path = os.path.join(data_dir, f"{market}_60m.csv")
    if not os.path.exists(path):
        download_hourly(market, days, data_dir)
    with open(path) as f:
        return [{k: (r[k] if k == "ts" else float(r[k])) for k in FIELDS}
                for r in csv.DictReader(f)]


# ---------- 지표 ----------
def regime_flags(bars, ma_len=30):
    """각 1시간봉 시점에 '어제까지 일봉 종가 > MA30'인지 (미래 참조 없음)."""
    day_close, order = {}, []
    for b in bars:
        d = b["ts"][:10]
        if d not in day_close:
            order.append(d)
        day_close[d] = b["close"]
    up_by_day = {}
    closes = [day_close[d] for d in order]
    for i, d in enumerate(order):
        if i < ma_len + 1:
            up_by_day[d] = False
            continue
        prev = closes[i - 1]
        ma = sum(closes[i - 1 - ma_len:i - 1]) / ma_len
        up_by_day[d] = prev > ma
    return [up_by_day[b["ts"][:10]] for b in bars]


def rsi_series(closes, n=14):
    out = [None] * len(closes)
    gain = loss = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            gain += g / n
            loss += l / n
            if i < n:
                continue
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + l) / n
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def to_4h(bars, regime):
    out, reg = [], []
    for i in range(0, len(bars) - 3, 4):
        chunk = bars[i:i + 4]
        out.append({"ts": chunk[0]["ts"], "open": chunk[0]["open"],
                    "high": max(b["high"] for b in chunk),
                    "low": min(b["low"] for b in chunk), "close": chunk[-1]["close"]})
        reg.append(regime[i])
    return out, reg


# ---------- 체결 기록 ----------
class Book:
    """코인 1개에 배정된 자본의 현금·보유·거래 기록."""

    def __init__(self, cash):
        self.cash = cash
        self.qty = 0.0
        self.fees = 0.0
        self.trades = []          # 왕복 거래별 순손익률
        self._cost = 0.0

    def buy(self, price, krw, maker):
        krw = min(krw, self.cash)
        if krw <= 0:
            return
        px = price if maker else price * (1 + SLIP)
        fee = krw * FEE
        self.qty += (krw - fee) / px
        self.cash -= krw
        self.fees += fee + (0 if maker else krw * SLIP)
        self._cost += krw

    def sell(self, price, frac, maker, cost=None):
        """cost: 이 매도분의 매수 원가 (그리드 칸별 손익용). 없으면 비례 배분."""
        q = self.qty * frac
        if q <= 0:
            return
        px = price if maker else price * (1 - SLIP)
        gross = q * px
        fee = gross * FEE
        cost = self._cost * frac if cost is None else min(cost, self._cost)
        self.cash += gross - fee
        self.qty -= q
        self._cost -= cost
        self.fees += fee + (0 if maker else q * price * SLIP)
        self.trades.append((gross - fee) / cost - 1 if cost else 0.0)

    def value(self, price):
        return self.cash + self.qty * price


# ---------- 전략 ----------
def strat_dip(bars, regime, cash, rsi_th=30, tp=0.02, sl=0.04, max_hold=48, offset=0.003):
    """A. 상승 추세 속 눌림목. RSI 과매도 봉 종가보다 offset 아래 지정가 매수 (3시간 유효)."""
    bk = Book(cash)
    rsi = rsi_series([b["close"] for b in bars])
    eq, pending, entry, held = [], None, 0.0, 0
    for i, b in enumerate(bars):
        if bk.qty > 0:
            held += 1
            stop, target = entry * (1 - sl), entry * (1 + tp)
            if b["low"] <= stop:                       # 같은 봉이면 손절 먼저 (보수적)
                bk.sell(min(stop, b["open"]), 1.0, maker=False)
            elif b["high"] > target:
                bk.sell(target, 1.0, maker=True)
            elif held >= max_hold or not regime[i]:
                bk.sell(b["close"], 1.0, maker=False)
        elif pending:
            limit, until = pending
            if b["low"] < limit:
                bk.buy(limit, bk.cash, maker=True)
                entry, held, pending = limit, 0, None
            elif i > until:
                pending = None
        if bk.qty == 0 and not pending and regime[i] and rsi[i] is not None and rsi[i] < rsi_th:
            pending = (b["close"] * (1 - offset), i + 3)
        eq.append(bk.value(b["close"]))
    return eq, bk


def strat_grid(bars, regime, cash, g=0.02, slots=5):
    """B. 그리드. 추세 이탈 시 전량 청산. 칸마다 자본 1/slots."""
    bk = Book(cash)
    lot = cash / slots
    lots, ref, eq = [], None, []          # lots: [(매수가, 수량, 원가)]
    for i, b in enumerate(bars):
        if not regime[i]:
            if bk.qty > 0:
                bk.sell(b["close"], 1.0, maker=False)
            lots, ref = [], None
            eq.append(bk.value(b["close"]))
            continue
        if ref is None:
            ref = b["close"]
            eq.append(bk.value(b["close"]))
            continue
        # 매도 먼저: 가장 싸게 산 칸부터 목표가 도달 확인
        sold = False
        for lot_ in sorted(lots):
            lp, q, krw = lot_
            if b["high"] > lp * (1 + g) and bk.qty > 0:
                bk.sell(lp * (1 + g), min(1.0, q / bk.qty), maker=True, cost=krw)
                lots.remove(lot_)
                ref = lp * (1 + g)
                sold = True
                break
        if not sold:
            level = ref * (1 - g)
            if b["low"] < level and len(lots) < slots and bk.cash > lot * 0.5:
                q0, krw = bk.qty, min(lot, bk.cash)
                bk.buy(level, krw, maker=True)
                lots.append((level, bk.qty - q0, krw))
                ref = level
            elif not lots and b["close"] > ref:
                ref = b["close"]             # 보유 없이 오르면 기준을 따라 올린다
        eq.append(bk.value(b["close"]))
    return eq, bk


def strat_breakout(bars4, regime4, cash, n=20, m=10):
    """C. 4시간봉 돌파. 신호 다음 봉 시가에 시장가."""
    bk = Book(cash)
    eq, sig = [], None
    for i, b in enumerate(bars4):
        if sig == "buy" and bk.qty == 0:
            bk.buy(b["open"], bk.cash, maker=False)
        elif sig == "sell" and bk.qty > 0:
            bk.sell(b["open"], 1.0, maker=False)
        sig = None
        if i >= n:
            hi = max(x["high"] for x in bars4[i - n:i])
            lo = min(x["low"] for x in bars4[i - m:i])
            if bk.qty == 0 and regime4[i] and b["close"] > hi:
                sig = "buy"
            elif bk.qty > 0 and (b["close"] < lo or not regime4[i]):
                sig = "sell"
        eq.append(bk.value(b["close"]))
    return eq, bk


def hold(bars, cash):
    bk = Book(cash)
    bk.buy(bars[0]["open"], cash, maker=False)
    return [bk.value(b["close"]) for b in bars], bk


def ma30_daily(bars, regime, cash):
    """현행 전략 근사 — 추세면 보유, 아니면 현금 (주간 대신 일간 판정, 시장가)."""
    bk = Book(cash)
    eq = []
    for i, b in enumerate(bars):
        if b["ts"][11:13] == "00":
            if regime[i] and bk.qty == 0:
                bk.buy(b["open"], bk.cash, maker=False)
            elif not regime[i] and bk.qty > 0:
                bk.sell(b["open"], 1.0, maker=False)
        eq.append(bk.value(b["close"]))
    return eq, bk


# ---------- 평가 ----------
def stats(eq_sum, books, hours_per_bar=1):
    start, end = eq_sum[0], eq_sum[-1]
    peak, mdd = start, 0.0
    for v in eq_sum:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    trades = [t for bk in books for t in bk.trades]
    wins = sum(1 for t in trades if t > 0)
    return {
        "ret": (end / start - 1) * 100, "mdd": mdd * 100,
        "n": len(trades), "win": wins / len(trades) * 100 if trades else 0.0,
        "avg": sum(trades) / len(trades) * 100 if trades else 0.0,
        "fees": sum(bk.fees for bk in books) / start * 100,
    }


def run_family(fn, data, segment, per_coin, **kw):
    """segment: (시작비율, 끝비율). 코인마다 독립 자본으로 돌리고 합산."""
    eqs, books = [], []
    for bars, reg, bars4, reg4 in data:
        if fn is strat_breakout:
            a, z = int(len(bars4) * segment[0]), int(len(bars4) * segment[1])
            eq, bk = fn(bars4[a:z], reg4[a:z], per_coin, **kw)
        elif fn is hold:
            a, z = int(len(bars) * segment[0]), int(len(bars) * segment[1])
            eq, bk = fn(bars[a:z], per_coin)
        else:
            a, z = int(len(bars) * segment[0]), int(len(bars) * segment[1])
            eq, bk = fn(bars[a:z], reg[a:z], per_coin, **kw)
        eqs.append(eq)
        books.append(bk)
    L = min(len(e) for e in eqs)
    total = [sum(e[i] for e in eqs) for i in range(L)]
    return stats(total, books)


FAMILIES = {
    "A 눌림목": (strat_dip, [dict(rsi_th=r, tp=t, sl=s)
                            for r in (25, 30) for t in (0.015, 0.03) for s in (0.03, 0.06)]),
    "B 그리드": (strat_grid, [dict(g=g, slots=k) for g in (0.01, 0.02, 0.03) for k in (4, 8)]),
    "C 4h돌파": (strat_breakout, [dict(n=n, m=m) for n in (20, 55) for m in (10, 20)]),
}

ROW = "{:<34} | {:+7.1f}% | {:5.1f}% | {:5d} | {:4.0f}% | {:+6.2f}% | {:5.1f}%"
HEAD = f"{'전략':<34} | {'수익':>8} | {'MDD':>6} | {'거래':>5} | {'승률':>5} | {'거래당':>7} | {'비용':>6}"


def label(kw):
    return " ".join(f"{k}={v}" for k, v in kw.items())


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    with open("config.yaml") as f:
        markets = yaml.safe_load(f)["universe"]
    data = []
    for m in markets:
        bars = load_hourly(m, days)
        reg = regime_flags(bars)
        bars4, reg4 = to_4h(bars, reg)
        data.append((bars, reg, bars4, reg4))
    n_bars = min(len(d[0]) for d in data)
    data = [(b[-n_bars:], r[-n_bars:], *to_4h(b[-n_bars:], r[-n_bars:])) for b, r, _, _ in data]
    first, last = data[0][0][0]["ts"], data[0][0][-1]["ts"]
    cut = data[0][0][int(n_bars * 2 / 3)]["ts"]
    per_coin = 1_000_000 / len(markets)
    IS, OOS = (0.0, 2 / 3), (2 / 3, 1.0)

    print(f"1시간봉 {first} ~ {last}  ({len(markets)}코인, 100만원)")
    print(f"학습 구간 ~ {cut[:16]} / 검증 구간 {cut[:16]} ~  (검증 구간은 파라미터 선택에 안 씀)")
    print(f"비용: 지정가 {FEE*100:.2f}% / 시장가 {FEE*100:.2f}%+슬리피지 {SLIP*100:.1f}%\n")

    chosen = {}
    print("== 1) 학습 구간: 파라미터별 성과 ==")
    print(HEAD)
    for fam, (fn, grid) in FAMILIES.items():
        best = None
        for kw in grid:
            s = run_family(fn, data, IS, per_coin, **kw)
            print(ROW.format(f"{fam} {label(kw)}", s["ret"], s["mdd"], s["n"], s["win"],
                             s["avg"], s["fees"]))
            score = s["ret"] / max(s["mdd"], 5.0)        # 수익/낙폭 (칼마 근사)
            if best is None or score > best[0]:
                best = (score, kw)
        chosen[fam] = best[1]
        print()

    print("== 2) 검증 구간: 학습에서 고른 파라미터 vs 기준선 (이게 판정 기준) ==")
    print(HEAD)
    for name, fn in (("보유 (Hold)", hold), ("현행 MA30 추세", ma30_daily)):
        s = run_family(fn, data, OOS, per_coin)
        print(ROW.format(name, s["ret"], s["mdd"], s["n"], s["win"], s["avg"], s["fees"]))
    for fam, kw in chosen.items():
        fn = FAMILIES[fam][0]
        s = run_family(fn, data, OOS, per_coin, **kw)
        print(ROW.format(f"{fam} {label(kw)}", s["ret"], s["mdd"], s["n"], s["win"],
                         s["avg"], s["fees"]))

    print("\n== 3) 검증 구간: 전 파라미터 (고른 것만 운 좋게 좋은지 확인) ==")
    print(HEAD)
    for fam, (fn, grid) in FAMILIES.items():
        for kw in grid:
            s = run_family(fn, data, OOS, per_coin, **kw)
            mark = " *" if kw == chosen[fam] else ""
            print(ROW.format(f"{fam} {label(kw)}{mark}", s["ret"], s["mdd"], s["n"], s["win"],
                             s["avg"], s["fees"]))


if __name__ == "__main__":
    main()
