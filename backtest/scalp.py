"""단타 연구실 — 빗썸 원화마켓 전체에서 '치고 빠지기'가 수수료를 이기는지 검증한다.

실행 (서버, 처음엔 전 코인 1시간봉 수집에 20~40분 — nohup 권장):
    .venv/bin/python -m backtest.scalp            # 수집(이어받기) + 검증
    .venv/bin/python -m backtest.scalp 180        # 수집 일수 지정

대형 6코인 단타(눌림목·그리드·4h돌파)는 3년 검증에서 전부 수수료에 졌다.
여기서는 한국 알트 시장 특유의 급등 현상을 노리는 두 계열을 본다.

  S1 급등 추격: 1시간 거래대금이 직전 72시간 평균의 X배로 터지고 가격이 r% 이상
     오른 코인을 다음 봉 시가에 매수 → +tp% 지정가 익절 / -sl% 손절 / H시간 뒤 정리.
  S2 강세 코인 순환: rebal시간마다 최근 L시간 상승률 상위 K개 보유, 상위 2K 밖으로
     밀리면 매도, -sl% 손절.

비용 (보수적): 수수료 0.04% + 시장가 슬리피지를 24시간 거래대금으로 차등
     (100억↑ 0.2% / 10억↑ 0.4% / 그 아래 0.7%). 익절은 지정가(슬리피지 없음),
     같은 봉에서 손절·익절이 둘 다 닿으면 손절로 본다. 24h 거래대금 3억 미만 코인은 제외.

판정: 앞 2/3 기간에서 파라미터를 고르고 뒤 1/3에서만 평가. 모든 수치는 %.
한계: 현재 상장된 코인만 받을 수 있어(상장폐지 코인 누락) 결과가 실제보다 낙관적이다.
"""
import csv
import math
import os
import sys
import time
from array import array
from datetime import datetime, timedelta

from exchange.bithumb import Bithumb

FEE = 0.0004
MIN_V24 = 3e8                 # 최소 24시간 거래대금 (원)
DATA_DIR = os.path.join("data", "h1")
NAN = float("nan")
FIELDS = ["ts", "open", "high", "low", "close", "value"]


def slip_for(v24):
    if v24 >= 1e10:
        return 0.002
    if v24 >= 1e9:
        return 0.004
    return 0.007


# ---------- 데이터 ----------
def download_all(days, out_dir=DATA_DIR):
    """전 원화마켓 1시간봉. 이미 받은 코인은 건너뛴다 (중단돼도 다시 실행하면 이어받기)."""
    ex = Bithumb()
    markets = ex.get_markets()
    os.makedirs(out_dir, exist_ok=True)
    todo = [m for m in markets if not os.path.exists(os.path.join(out_dir, f"{m}.csv"))]
    print(f"원화마켓 {len(markets)}개 / 수집 필요 {len(todo)}개", flush=True)
    for n, m in enumerate(todo, 1):
        rows, to = [], None
        try:
            while len(rows) < days * 24:
                batch = ex.get_minute_candles(m, 60, 200, to)
                if not batch:
                    break
                for c in batch:
                    rows.append([c["candle_date_time_kst"][:13] + ":00:00", c["opening_price"],
                                 c["high_price"], c["low_price"], c["trade_price"],
                                 c.get("candle_acc_trade_price", 0)])
                to = batch[-1]["candle_date_time_utc"]
                if len(batch) < 200:
                    break
                time.sleep(0.07)
        except Exception as e:
            print(f"{m}: 수집 실패 ({e}) — 건너뜀", flush=True)
            continue
        with open(os.path.join(out_dir, f"{m}.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(FIELDS)
            w.writerows(rows[:days * 24][::-1])
        if n % 20 == 0:
            print(f"  {n}/{len(todo)} 완료", flush=True)
    return markets


class Data:
    """전 코인을 하나의 시간축에 정렬한 배열 (메모리 절약용 array)."""

    def __init__(self, markets, data_dir=DATA_DIR, days=None):
        # 1차: 파일별 첫·끝 시각만 읽는다 (서버 메모리 1GB — 전부 올리면 부족)
        spans = {}
        for m in markets:
            path = os.path.join(data_dir, f"{m}.csv")
            if not os.path.exists(path):
                continue
            with open(path) as f:
                next(f, None)
                first, n, last = None, 0, None
                for line in f:
                    if first is None:
                        first = line
                    last, n = line, n + 1
            if n >= 24 * 30:
                spans[m] = (first.split(",", 1)[0], last.split(",", 1)[0])
        t0 = datetime.fromisoformat(min(a for a, _ in spans.values()))
        t1 = datetime.fromisoformat(max(b for _, b in spans.values()))
        if days:
            t0 = max(t0, t1 - timedelta(days=days))
        self.ts = []
        t = t0
        while t <= t1:
            self.ts.append(t.strftime("%Y-%m-%dT%H:%M:%S"))
            t += timedelta(hours=1)
        index = {ts: i for i, ts in enumerate(self.ts)}
        T = len(self.ts)
        self.markets = sorted(spans)
        self.o, self.h, self.l, self.c, self.v = [], [], [], [], []
        self.pv = []     # 거래대금 누적합 (구간합 계산용)
        # 2차: 코인 하나씩 배열로 채운다
        for m in self.markets:
            arrs = [array("d", [NAN]) * T for _ in range(5)]
            with open(os.path.join(data_dir, f"{m}.csv")) as f:
                rd = csv.reader(f)
                next(rd, None)
                for r in rd:
                    i = index.get(r[0])
                    if i is None:
                        continue
                    for k in range(5):
                        arrs[k][i] = float(r[k + 1])
            pv = array("d", [0.0]) * (T + 1)
            for i in range(T):
                x = arrs[4][i]
                pv[i + 1] = pv[i] + (0.0 if math.isnan(x) else x)
            self.o.append(arrs[0]); self.h.append(arrs[1]); self.l.append(arrs[2])
            self.c.append(arrs[3]); self.v.append(arrs[4]); self.pv.append(pv)

    def vsum(self, ci, a, b):
        """[a, b) 구간 거래대금 합"""
        a = max(a, 0)
        return self.pv[ci][b] - self.pv[ci][a]


# ---------- 공통 체결 ----------
class Port:
    def __init__(self):
        self.cash = 1.0
        self.pos = {}            # ci -> [qty, entry_px, t0, cost]
        self.last = {}           # ci -> 마지막 유효 종가
        self.trades = []
        self.fees = 0.0

    def value(self):
        return self.cash + sum(p[0] * self.last.get(ci, p[1]) for ci, p in self.pos.items())

    def buy(self, ci, px, krw, slip, t):
        krw = min(krw, self.cash)
        if krw <= 1e-9:
            return
        fill = px * (1 + slip)
        self.cash -= krw
        self.fees += krw * (FEE + slip)
        self.pos[ci] = [krw * (1 - FEE) / fill, fill, t, krw]
        self.last[ci] = px

    def sell(self, ci, px, slip):
        qty, _, _, cost = self.pos.pop(ci)
        fill = px * (1 - slip)
        got = qty * fill * (1 - FEE)
        self.fees += qty * px * (FEE + slip)
        self.cash += got
        self.trades.append(got / cost - 1)


def stats(eq, port):
    peak, mdd = eq[0], 0.0
    for x in eq:
        peak = max(peak, x)
        mdd = max(mdd, (peak - x) / peak)
    tr = port.trades
    months = len(eq) / 730.0
    total = eq[-1] / eq[0]
    return {
        "ret": (total - 1) * 100,
        "mon": ((total ** (1 / months)) - 1) * 100 if months > 0 and total > 0 else -100.0,
        "mdd": mdd * 100, "n": len(tr),
        "win": sum(1 for x in tr if x > 0) / len(tr) * 100 if tr else 0.0,
        "avg": sum(tr) / len(tr) * 100 if tr else 0.0,
    }


def stop_or_target(D, port, ci, t, sl, tp):
    """보유 코인의 t봉 손절/익절 처리. 청산했으면 True."""
    o, h, l = D.o[ci][t], D.h[ci][t], D.l[ci][t]
    if math.isnan(o):
        return False
    entry = port.pos[ci][1]
    slip = slip_for(D.vsum(ci, t - 24, t))
    stop = entry * (1 - sl)
    if l <= stop:                                   # 같은 봉이면 손절 우선 (보수적)
        port.sell(ci, min(stop, o), slip)
        return True
    if tp and h > entry * (1 + tp):
        port.sell(ci, entry * (1 + tp), 0.0)        # 지정가 익절
        return True
    return False


# ---------- S1 급등 추격 ----------
def spike_signals(D, x_mult, r_min):
    """t봉 마감 기준 신호 {t: [(점수, ci), ...]} — t+1봉 시가에 진입한다."""
    sig = {}
    T = len(D.ts)
    for ci in range(len(D.markets)):
        c, v = D.c[ci], D.v[ci]
        for t in range(73, T - 1):
            vt, ct, cp = v[t], c[t], c[t - 1]
            if math.isnan(vt) or math.isnan(ct) or math.isnan(cp) or cp <= 0:
                continue
            if ct / cp - 1 < r_min:
                continue
            base = D.vsum(ci, t - 72, t) / 72
            if base <= 0 or vt < x_mult * base:
                continue
            if D.vsum(ci, t - 23, t + 1) < MIN_V24:
                continue
            sig.setdefault(t, []).append((vt / base, ci))
    return sig


def regime_ok(D, btc, t):
    """BTC 종가가 30일(720시간) 평균 위인가 — t봉 마감까지의 정보만 사용."""
    if btc is None or t < 720:
        return True
    c = D.c[btc]
    vals = [x for x in c[t - 719:t + 1] if not math.isnan(x)]
    return bool(vals) and not math.isnan(c[t]) and c[t] > sum(vals) / len(vals)


def run_spike(D, sig, a, z, tp, sl, hold, slots=5, regime=False, btc=None):
    port, eq = Port(), []
    reg_cache = {}
    for t in range(a, z):
        # 1) 직전 봉 신호로 시가 진입
        cands = sig.get(t - 1)
        if cands and len(port.pos) < slots:
            ok = True
            if regime:
                ok = reg_cache.setdefault(t - 1, regime_ok(D, btc, t - 1))
            if ok:
                size = port.value() / slots
                for score, ci in sorted(cands, reverse=True):
                    if len(port.pos) >= slots:
                        break
                    o = D.o[ci][t]
                    if ci in port.pos or math.isnan(o):
                        continue
                    port.buy(ci, o, size, slip_for(D.vsum(ci, t - 24, t)), t)
        # 2) 보유분 손절·익절·시간 청산
        for ci in list(port.pos):
            if stop_or_target(D, port, ci, t, sl, tp):
                continue
            cl = D.c[ci][t]
            if not math.isnan(cl):
                port.last[ci] = cl
                if t - port.pos[ci][2] + 1 >= hold:
                    port.sell(ci, cl, slip_for(D.vsum(ci, t - 23, t + 1)))
        eq.append(port.value())
    return eq, port


# ---------- S2 강세 코인 순환 ----------
def run_rotation(D, a, z, look, k, rebal, sl, regime=False, btc=None):
    port, eq = Port(), []
    plan = None                                  # 다음 봉 시가에 실행할 (매도, 매수) 목록
    for t in range(a, z):
        if plan:
            sells, buys = plan
            for ci in sells:
                if ci in port.pos and not math.isnan(D.o[ci][t]):
                    port.sell(ci, D.o[ci][t], slip_for(D.vsum(ci, t - 24, t)))
            if buys:
                size = port.value() / k
                for ci in buys:
                    o = D.o[ci][t]
                    if len(port.pos) < k and ci not in port.pos and not math.isnan(o):
                        port.buy(ci, o, size, slip_for(D.vsum(ci, t - 24, t)), t)
            plan = None
        for ci in list(port.pos):
            if stop_or_target(D, port, ci, t, sl, 0):
                continue
            cl = D.c[ci][t]
            if not math.isnan(cl):
                port.last[ci] = cl
        if (t - a) % rebal == rebal - 1 and t >= look:
            rank = []
            if not regime or regime_ok(D, btc, t):
                for ci in range(len(D.markets)):
                    c0, c1 = D.c[ci][t - look], D.c[ci][t]
                    if math.isnan(c0) or math.isnan(c1) or c0 <= 0:
                        continue
                    if D.vsum(ci, t - 23, t + 1) < MIN_V24:
                        continue
                    r = c1 / c0 - 1
                    if r > 0:
                        rank.append((r, ci))
                rank.sort(reverse=True)
            top_k = [ci for _, ci in rank[:k]]
            keep = {ci for _, ci in rank[:2 * k]}
            sells = [ci for ci in port.pos if ci not in keep]
            buys = [ci for ci in top_k if ci not in port.pos]
            plan = (sells, buys)
        eq.append(port.value())
    return eq, port


def run_hold(D, ci, a, z):
    port, eq = Port(), []
    for t in range(a, z):
        o = D.o[ci][t]
        if not port.pos and port.cash > 0 and not math.isnan(o):
            port.buy(ci, o, 1.0, 0.002, t)
        cl = D.c[ci][t]
        if ci in port.pos and not math.isnan(cl):
            port.last[ci] = cl
        eq.append(port.value())
    return eq, port


# ---------- 실행 ----------
S1_GRID = [dict(x=x, r=r, tp=tp, sl=0.04, hold=12, regime=g)
           for x in (3, 5) for r in (0.03, 0.05) for tp in (0.04, 0.08) for g in (False, True)]
S2_GRID = [dict(look=lb, k=k, rebal=rb, sl=0.08, regime=g)
           for lb in (24, 72) for k in (1, 3) for rb in (4, 24) for g in (False, True)]

ROW = "{:<46} | {:+8.1f}% | {:+6.1f}% | {:5.1f}% | {:5d} | {:4.0f}% | {:+6.2f}%"
HEAD = f"{'전략':<46} | {'총수익':>9} | {'월평균':>7} | {'MDD':>6} | {'거래':>5} | {'승률':>5} | {'거래당':>7}"


def _label(kw):
    return " ".join(f"{k}={'on' if v is True else 'off' if v is False else v}" for k, v in kw.items())


def evaluate(D, a, z, fam, kw, sig_cache, btc):
    if fam == "S1":
        key = (kw["x"], kw["r"])
        if key not in sig_cache:
            sig_cache[key] = spike_signals(D, *key)
        eq, p = run_spike(D, sig_cache[key], a, z, kw["tp"], kw["sl"], kw["hold"],
                          regime=kw["regime"], btc=btc)
    else:
        eq, p = run_rotation(D, a, z, kw["look"], kw["k"], kw["rebal"], kw["sl"],
                             regime=kw["regime"], btc=btc)
    return stats(eq, p)


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    markets = download_all(days)
    D = Data(markets, days=days)
    T = len(D.ts)
    cut = int(T * 2 / 3)
    btc = D.markets.index("KRW-BTC") if "KRW-BTC" in D.markets else None
    print(f"\n코인 {len(D.markets)}개 / {D.ts[0][:13]} ~ {D.ts[-1][:13]}")
    print(f"학습 ~ {D.ts[cut][:10]} / 검증 {D.ts[cut][:10]} ~ (검증 구간은 파라미터 선택에 안 씀)")
    print("비용: 수수료 0.04% + 슬리피지 0.2~0.7%(거래대금별) / 익절은 지정가\n")

    sig_cache, chosen = {}, {}
    print("== 1) 학습 구간 ==")
    print(HEAD)
    for fam, grid in (("S1", S1_GRID), ("S2", S2_GRID)):
        best = None
        for kw in grid:
            s = evaluate(D, 0, cut, fam, kw, sig_cache, btc)
            print(ROW.format(f"{fam} {_label(kw)}", s["ret"], s["mon"], s["mdd"], s["n"],
                             s["win"], s["avg"]), flush=True)
            score = s["ret"] / max(s["mdd"], 10.0)
            if best is None or score > best[0]:
                best = (score, kw)
        chosen[fam] = best[1]
        print()

    print("== 2) 검증 구간: 학습에서 고른 설정 vs BTC 보유 (판정 기준) ==")
    print(HEAD)
    if btc is not None:
        s = stats(*run_hold(D, btc, cut, T))
        print(ROW.format("BTC 보유", s["ret"], s["mon"], s["mdd"], s["n"], s["win"], s["avg"]))
    for fam, kw in chosen.items():
        s = evaluate(D, cut, T, fam, kw, sig_cache, btc)
        print(ROW.format(f"{fam} {_label(kw)}", s["ret"], s["mon"], s["mdd"], s["n"],
                         s["win"], s["avg"]))

    print("\n== 3) 검증 구간: 전 설정 (* = 학습 선택) ==")
    print(HEAD)
    for fam, grid in (("S1", S1_GRID), ("S2", S2_GRID)):
        for kw in grid:
            s = evaluate(D, cut, T, fam, kw, sig_cache, btc)
            mark = " *" if kw == chosen[fam] else ""
            print(ROW.format(f"{fam} {_label(kw)}{mark}", s["ret"], s["mon"], s["mdd"], s["n"],
                             s["win"], s["avg"]), flush=True)


if __name__ == "__main__":
    main()
