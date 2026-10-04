"""정기 상태 리포트 — 한국시간 지정 시각마다 텔레그램으로 현황을 보낸다.

거래가 없어도 "살아있고 이렇게 판단 중"을 사용자가 확인할 수 있어야 한다.
"""
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
REPORT_HOURS = (8, 10, 12, 14, 16, 18, 20, 22)   # 한국시간 기준


def due(last_slot: str, now: datetime | None = None) -> str | None:
    """지금이 리포트 시각이고 아직 안 보냈으면 슬롯 키를 반환."""
    now = now or datetime.now(KST)
    if now.hour not in REPORT_HOURS:
        return None
    slot = now.strftime("%Y-%m-%d-%H")
    return None if slot == last_slot else slot


def build(mode: str, equity: float, start_capital: float, krw: float,
          positions: dict, prices: dict, trend: dict, universe: list,
          reserve: float = 0.0, settled_cycles: int = 0) -> str:
    """현황 메시지 작성.

    positions: {market: {volume, entry_price, krw_spent}}
    trend:     {market: bool}  — 추세 판단 상태
    """
    pnl = equity - start_capital
    pct = pnl / start_capital * 100 if start_capital else 0.0
    sign = "🟢" if pnl >= 0 else "🔴"
    now = datetime.now(KST).strftime("%m/%d %H:%M")

    lines = [f"{sign} [{mode}] {now} 현황",
             f"운용자산 {equity:,.0f}원 ({pct:+.2f}%, {pnl:+,.0f}원)",
             f"현금 {krw:,.0f}원"]
    if reserve > 0:
        lines.append(f"💰 출금 대기 {reserve:,.0f}원 (출금 후 /settle)")

    if positions:
        lines.append("")
        lines.append("보유:")
        for market in universe:
            pos = positions.get(market)
            if not pos:
                continue
            price = prices.get(market, pos["entry_price"])
            value = pos["volume"] * price
            p = value - pos["krw_spent"]
            r = p / pos["krw_spent"] * 100 if pos["krw_spent"] else 0.0
            coin = market.replace("KRW-", "")
            # 현재가를 함께 보여준다 — 저가 코인은 호가 단위(1원)가 커서 가격이
            # 한 칸 움직이기 전까지 평가액이 몇 시간씩 그대로일 수 있다
            lines.append(f"  {coin} {value:,.0f}원 ({r:+.1f}%) · {price:,.0f}원")
    else:
        lines.append("")
        lines.append("보유 없음 (전액 현금)")

    waiting = [m.replace("KRW-", "") for m in universe
               if not trend.get(m) and m not in positions]
    if waiting:
        lines.append(f"대기(추세 아래): {', '.join(waiting)}")

    return "\n".join(lines)
