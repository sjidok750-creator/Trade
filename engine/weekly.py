"""주간 요약 — 실행: .venv/bin/python -m engine.weekly [일수]

출력 전체를 복사해 Claude에게 붙여넣으면 주간 리뷰가 된다. Claude는 서버에
접속할 수 없으므로, 리뷰에 필요한 것을 이 한 화면에 전부 담는다:
자산 흐름, 같은 코인 단순보유(BuyHold)와의 비교, 체결 슬리피지, 거래 내역,
추세 상태, 이익확정 진행, 리스크 가드, 재시작·오류 이벤트, 쿠폰 잔여일.

기본 7일. 월간 판정(BuyHold 대비 우위 확인)은 `30`을 넘겨 실행한다.
거래소 조회가 안 되면(키 없음·네트워크) 그 부분만 빼고 로그 기준으로 출력한다.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import yaml

from . import fee_guard

KST = timezone(timedelta(hours=9))
FEE = 0.0004


# ---------- 데이터 읽기 ----------
def _load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def read_events(path: str, since: datetime) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            try:
                ev = json.loads(line)
                if datetime.fromisoformat(ev["ts"]) >= since:
                    out.append(ev)
            except (ValueError, KeyError):
                continue
    return out


def read_equity(db_path: str, since: datetime) -> list[tuple[datetime, float]]:
    """10분 단위 자산 기록 (ts, equity)."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT ts, equity_krw FROM equity WHERE ts >= ? ORDER BY ts",
                        (since.isoformat(),)).fetchall()
    conn.close()
    return [(datetime.fromisoformat(ts), float(eq)) for ts, eq in rows]


# ---------- 계산 ----------
def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def _kst(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(KST).strftime("%m/%d %H:%M")


def buyhold_returns(candles: dict[str, list[dict]], prices: dict[str, float],
                    days: int) -> dict[str, float]:
    """코인별 단순보유 수익률(%): days일 전 종가 → 현재가.

    candles: {market: 최신순 일봉}. candles[days]가 days일 전 봉이다.
    """
    out = {}
    for m, cs in candles.items():
        if m in prices and len(cs) > days:
            base = float(cs[days]["trade_price"])
            out[m] = _pct(prices[m], base)
    return out


def build(days: int, cfg: dict, events: list[dict], equity: list, positions: dict,
          trend: dict, settle: dict, risk: dict, prices: dict | None,
          balances: dict | None, candles: dict | None,
          now: datetime | None = None) -> str:
    now = now or datetime.now(KST)
    since = now - timedelta(days=days)
    if not equity:   # 자산 DB가 없으면 2시간 리포트에 남은 자산으로 대신한다
        equity = [(datetime.fromisoformat(e["ts"]), float(e["equity"]))
                  for e in events if e.get("type") == "report" and "equity" in e]
    dry = cfg["mode"]["dry_run"]
    universe = cfg["universe"]
    L = []

    L.append(f"=== 주간 요약 {since:%Y-%m-%d} ~ {now:%Y-%m-%d} "
             f"({days}일, {'페이퍼' if dry else '실전'}) 생성 {now:%m/%d %H:%M} KST ===")

    # --- 자산 ---
    start = settle.get("start", 0.0)
    krw = 0.0
    if balances is not None:
        krw = balances.get("KRW", {}).get("balance", 0.0)
    reserve = settle.get("reserve", 0.0)
    if prices:
        cur = krw + sum(p["volume"] * prices.get(m, p["entry_price"])
                        for m, p in positions.items()) - reserve
        cur_src = "실시간"
    elif equity:
        cur = equity[-1][1]
        cur_src = f"마지막 기록 {equity[-1][0].astimezone(KST):%m/%d %H:%M}"
    else:
        cur = start
        cur_src = "기록 없음"
    L.append("")
    L.append("[자산]")
    L.append(f"  시작 {start:,.0f} → 현재 {cur:,.0f}원 ({_pct(cur, start):+.2f}%) [{cur_src}]")
    if equity:
        first = equity[0][1]
        hi = max(e for _, e in equity)
        lo = min(e for _, e in equity)
        L.append(f"  기간 내: 시작 {first:,.0f} → ({_pct(cur, first):+.2f}%) "
                 f"고점 {hi:,.0f} / 저점 {lo:,.0f} / 표본 {len(equity)}개")
    if prices:
        L.append(f"  현금 {krw - reserve:,.0f}원" + (f", 출금 대기 {reserve:,.0f}원" if reserve else ""))

    # --- 시장 비교 ---
    L.append("")
    L.append("[시장 비교 — 같은 기간 단순보유]")
    if candles and prices:
        bh = buyhold_returns(candles, prices, days)
        if bh:
            eq_w = sum(bh.values()) / len(bh)
            strat = _pct(cur, equity[0][1]) if equity else _pct(cur, start)
            L.append(f"  {len(bh)}코인 균등보유 {eq_w:+.2f}% | BTC {bh.get('KRW-BTC', 0):+.2f}% "
                     f"| 전략 {strat:+.2f}% → 차이 {strat - eq_w:+.2f}%p")
            L.append("  코인별: " + ", ".join(
                f"{m.replace('KRW-', '')} {r:+.1f}%" for m, r in bh.items()))
        else:
            L.append("  일봉 부족")
    else:
        L.append("  (거래소 조회 불가 — 생략)")

    # --- 거래 ---
    trades = [e for e in events if e.get("type") == "trade"]
    L.append("")
    L.append(f"[거래] {len(trades)}건 "
             f"(매수 {sum(t['side'] == 'buy' for t in trades)}, "
             f"매도 {sum(t['side'] == 'sell' for t in trades)}), "
             f"수수료 추정 {sum(t['krw'] for t in trades) * FEE:,.0f}원")
    for t in trades[-20:]:
        L.append(f"  {_kst(t['ts'])} {t['side']:4} {t['market'].replace('KRW-', ''):5} "
                 f"{t['krw']:>10,.0f}원 @ {t['price']:,.0f}  {t['reason']}")

    # --- 포지션 ---
    L.append("")
    L.append(f"[포지션] {len(positions)}개")
    for m in universe:
        p = positions.get(m)
        if not p:
            continue
        coin = m.replace("KRW-", "")
        line = f"  {coin:5} 진입 {p['entry_price']:,.0f}"
        if prices and m in prices:
            val = p["volume"] * prices[m]
            line += (f" → 현재 {prices[m]:,.0f} ({_pct(prices[m], p['entry_price']):+.2f}%) "
                     f"가치 {val:,.0f}원")
        if balances is not None:
            actual = balances.get(coin, {}).get("balance", 0.0)
            diff = _pct(actual, p["volume"]) if p["volume"] else 0.0
            note = " ※기존보유 포함" if diff > 5 else ""
            line += f" | 수량 기록 {p['volume']:.6f} 실제 {actual:.6f} ({diff:+.3f}%){note}"
        L.append(line)
    if balances is not None:
        diffs = [_pct(balances.get(m.replace("KRW-", ""), {}).get("balance", 0.0), p["volume"])
                 for m, p in positions.items() if p["volume"]]
        diffs = [d for d in diffs if abs(d) <= 5]      # 기존 보유분이 섞인 코인 제외
        if diffs:
            L.append(f"  슬리피지 평균 {sum(diffs) / len(diffs):+.3f}% "
                     f"(백테스트 가정 편도 -0.24% 안쪽이면 정상)")

    # --- 추세 ---
    scfg = cfg["strategy"]
    last = trend.get("_last_rebal", "")
    L.append("")
    L.append("[추세] " + ", ".join(
        f"{m.replace('KRW-', '')} {'▲' if trend.get(m) else '▽'}" for m in universe))
    if last:
        nxt = datetime.fromisoformat(last) + timedelta(days=scfg.get("rebal_days", 7))
        L.append(f"  마지막 리밸런싱 {last}, 다음 {nxt:%Y-%m-%d} "
                 f"(MA{scfg['ma_len']} ±{scfg['band'] * 100:.0f}%)")
    rebals = [e for e in events if e.get("type") == "trend_rebalance"]
    for r in rebals:
        L.append(f"  {_kst(r['ts'])} 리밸런싱 → 보유군 "
                 f"{[m.replace('KRW-', '') for m in r.get('targets', [])]}")

    # --- 이익확정 ---
    target = cfg.get("settle", {}).get("target_krw", 0)
    L.append("")
    if target:
        baseline = settle.get("baseline", 0.0)
        progress = cur - baseline
        L.append(f"[이익확정] 기준선 {baseline:,.0f} → 진행 이익 {progress:+,.0f}원 / 목표 {target:,}원, "
                 f"확정 {settle.get('cycles', 0)}회, 출금 대기 {reserve:,.0f}원")
    else:
        L.append("[이익확정] 비활성 (전액 재투자)")

    # --- 리스크 ---
    peak = risk.get("peak_equity", 0.0)
    rcfg = cfg["risk"]
    L.append("")
    L.append(f"[리스크] 고점 {peak:,.0f} 대비 {_pct(cur, peak):+.2f}% "
             f"(킬스위치 -{rcfg['kill_switch_pct'] * 100:.0f}% = {peak * (1 - rcfg['kill_switch_pct']):,.0f}원)"
             + (" ⚠️ 당일 매수 중단 상태" if risk.get("daily_halted") else "")
             + (" 🚨 킬스위치 발동됨" if risk.get("halted") else ""))

    # --- 이벤트 ---
    watch = ["engine_start", "error", "buy_blocked", "daily_loss_limit", "kill_switch",
             "fee_anomaly", "fee_coupon", "volume_clamp", "skim", "skim_deferred",
             "telegram_config", "settled"]
    counts = {k: sum(e.get("type") == k for e in events) for k in watch}
    label = {"engine_start": "재시작"}
    shown = [f"{label.get(k, k)} {v}" for k, v in counts.items() if v]
    L.append("")
    L.append("[이벤트] " + (", ".join(shown) if shown else "특이사항 없음"))
    errors = [e for e in events if e.get("type") == "error"]
    for e in errors[-3:]:
        L.append(f"  {_kst(e['ts'])} {e.get('where', '')}: {str(e.get('error', ''))[:80]}")
    for e in [e for e in events if e.get("type") in ("buy_blocked", "skim_deferred")][-3:]:
        L.append(f"  {_kst(e['ts'])} {e['type']} {e.get('market', '')} {e.get('reason', '')}")

    # --- 쿠폰 ---
    left = fee_guard.days_left(cfg.get("fee", {}).get("coupon_renewed_on"))
    L.append("")
    L.append(f"[수수료 쿠폰] 잔여 {left}일" if left is not None else "[수수료 쿠폰] 미설정")
    if left is not None and left <= 7:
        L.append("  ⚠️ 빗썸에서 재신청 후 config.yaml fee.coupon_renewed_on 갱신 필요")

    return "\n".join(L)


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    now = datetime.now(KST)
    since = (now - timedelta(days=days)).astimezone(timezone.utc)

    events = read_events("logs/events.jsonl", since)
    equity = read_equity("logs/trades.db", since)
    positions = _load_json("state/positions.json", {})
    trend = _load_json("state/trend.json", {})
    settle = _load_json("state/settle.json", {})
    risk = _load_json("state/risk.json", {})

    prices = balances = candles = None
    try:
        from exchange.bithumb import Bithumb
        ex = Bithumb()
        prices = ex.get_tickers(cfg["universe"])
        candles = {m: ex.get_daily_candles(m, count=days + 2) for m in cfg["universe"]}
        if cfg["mode"]["dry_run"]:
            paper = _load_json("state/paper.json", {"krw": 0.0})
            balances = {"KRW": {"balance": paper["krw"]},
                        **{m.replace("KRW-", ""): {"balance": p["volume"]}
                           for m, p in positions.items()}}
        else:
            balances = ex.get_balances()
    except Exception as e:  # 오프라인이어도 로그 기준 요약은 나와야 한다
        print(f"(거래소 조회 실패: {e})")

    print(build(days, cfg, events, equity, positions, trend, settle, risk,
                prices, balances, candles, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
