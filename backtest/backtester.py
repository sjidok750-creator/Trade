"""다코인 변동성 돌파 백테스터.

가정:
- 각 코인은 하루 1회, 고가가 목표가를 넘으면 목표가에 체결 (보수적으로 슬리피지 가산)
- 청산은 당일 종가 (다음날 00:00 리셋 근사)
- 자본은 코인별로 position_pct 만큼 배분, 동시 진입 수는 max_positions로 제한
- 수수료 왕복 + 슬리피지 반영
- 장중 저가가 손절가 이하로 내려가면 손절가 체결로 처리 (보수적)

사용법 (data/ 에 CSV가 있어야 함):
    python -m backtest.backtester            # config.yaml 설정으로 1회 실행
    python -m backtest.backtester sweep      # k값 스윕 (0.3~0.8)
"""
import sys
from collections import defaultdict

import yaml

from .data import load_csv

FEE = 0.0004
SLIPPAGE = 0.001


def run(markets: list[str], k: float, position_pct: float, max_positions: int,
        stop_loss_pct: float, trend_filter_ma: int = 0,
        start_capital: float = 1_000_000, data_dir: str = "data",
        per_coin_k: dict | None = None) -> dict:
    per_coin_k = per_coin_k or {}
    data = {m: load_csv(m, data_dir) for m in markets}
    dates = sorted(set(d["date"] for rows in data.values() for d in rows))
    idx = {m: {row["date"]: i for i, row in enumerate(rows)} for m, rows in data.items()}

    capital = start_capital
    peak = capital
    mdd = 0.0
    trades = []
    daily_equity = []

    for date in dates[1:]:
        entries = []
        for m in markets:
            rows = data[m]
            i = idx[m].get(date)
            if i is None or i < max(1, trend_filter_ma):
                continue
            today, yday = rows[i], rows[i - 1]
            prev_range = yday["high"] - yday["low"]
            if prev_range <= 0:
                continue
            kk = per_coin_k.get(m, k)
            target = today["open"] + prev_range * kk
            if trend_filter_ma > 0:
                closes = [rows[j]["close"] for j in range(i - trend_filter_ma, i)]
                if today["open"] <= sum(closes) / len(closes):
                    continue
            if today["high"] >= target:
                entries.append((m, today, target))

        # 실전에서는 돌파 순서대로 진입하지만 일봉만으로는 순서를 모른다 → 앞에서부터 자름
        entries = entries[:max_positions]
        day_pnl = 0.0
        for m, today, target in entries:
            stake = capital * position_pct
            entry = target * (1 + SLIPPAGE)
            stop = entry * (1 - stop_loss_pct)
            if today["low"] <= stop:
                exit_price = stop * (1 - SLIPPAGE)
                reason = "stop"
            else:
                exit_price = today["close"] * (1 - SLIPPAGE)
                reason = "close"
            gross = stake * (exit_price / entry)
            net = gross * (1 - FEE) - stake * FEE - stake
            day_pnl += net
            trades.append({"date": date, "market": m, "pnl": net, "reason": reason})
        capital += day_pnl
        peak = max(peak, capital)
        mdd = max(mdd, (peak - capital) / peak)
        daily_equity.append((date, capital))
        if capital <= 0:
            break

    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "final_capital": capital,
        "return_pct": (capital / start_capital - 1) * 100,
        "mdd_pct": mdd * 100,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "stops": sum(1 for t in trades if t["reason"] == "stop"),
        "daily_equity": daily_equity,
        "by_market": {m: sum(t["pnl"] for t in trades if t["market"] == m)
                      for m in markets},
    }


def summarize(r: dict, label: str = ""):
    print(f"{label:>10} | 수익률 {r['return_pct']:+8.1f}% | MDD {r['mdd_pct']:5.1f}% | "
          f"거래 {r['trades']:4d}회 | 승률 {r['win_rate']:5.1f}% | 손절 {r['stops']}회")


if __name__ == "__main__":
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    s, rk = cfg["strategy"], cfg["risk"]
    common = dict(markets=cfg["universe"], position_pct=rk["position_pct"],
                  max_positions=rk["max_positions"], stop_loss_pct=rk["stop_loss_pct"],
                  trend_filter_ma=s["trend_filter_ma"])
    if len(sys.argv) > 1 and sys.argv[1] == "sweep":
        for k in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
            summarize(run(k=k, **common), f"k={k}")
    else:
        r = run(k=s["k"], per_coin_k=s["per_coin_k"], **common)
        summarize(r, f"k={s['k']}")
        print("\n코인별 손익:")
        for m, pnl in r["by_market"].items():
            print(f"  {m}: {pnl:+,.0f}원")
