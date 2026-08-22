"""수수료 쿠폰 만료 감지.

빗썸의 '국내 최저 수수료 0.04%'는 신청일로부터 30일만 유효하다.
만료되면 0.25%로 돌아가 전략 수익성이 무너지므로, 날짜 기반 사전 경고와
실제 체결 수수료 기반 사후 감지를 모두 둔다.
"""
from datetime import date, datetime

WARN_DAYS = 3          # 만료 N일 전부터 경고
COUPON_DAYS = 30       # 쿠폰 유효기간
FEE_TOLERANCE = 0.0008 # 실효 수수료가 이 값을 넘으면 쿠폰 만료로 간주 (0.04% 기준 2배)


def days_left(renewed_on: str | None) -> int | None:
    """쿠폰 재신청일(YYYY-MM-DD) 기준 남은 일수."""
    if not renewed_on:
        return None
    try:
        start = datetime.strptime(renewed_on, "%Y-%m-%d").date()
    except ValueError:
        return None
    return COUPON_DAYS - (date.today() - start).days


def expiry_warning(renewed_on: str | None) -> str | None:
    """경고가 필요하면 메시지를, 아니면 None을 반환."""
    left = days_left(renewed_on)
    if left is None:
        return None
    if left < 0:
        return (f"🚨 수수료 쿠폰이 {-left}일 전 만료됐습니다. 수수료가 0.25%로 적용 중일 수 있습니다. "
                f"빗썸 > 혜택 > 국내 최저 수수료 신청 후 config의 fee.coupon_renewed_on을 갱신하세요.")
    if left <= WARN_DAYS:
        return f"⚠️ 수수료 쿠폰이 {left}일 뒤 만료됩니다. 빗썸에서 재신청하세요."
    return None


def effective_fee(order: dict) -> float | None:
    """체결 결과에서 실효 수수료율을 역산. 계산 불가하면 None."""
    try:
        paid = float(order.get("paid_fee") or 0)
        volume = float(order.get("executed_volume") or 0)
        price = float(order.get("price") or order.get("avg_price") or 0)
    except (TypeError, ValueError):
        return None
    notional = volume * price
    if notional <= 0:
        return None
    return paid / notional


def fee_anomaly(order: dict) -> str | None:
    """실제 낸 수수료가 예상보다 높으면 경고 메시지."""
    rate = effective_fee(order)
    if rate is None or rate <= FEE_TOLERANCE:
        return None
    return (f"🚨 실제 수수료율이 {rate*100:.3f}% 로 측정됐습니다 (기대 0.04%). "
            f"수수료 쿠폰이 만료된 것으로 보입니다. 빗썸에서 재신청하세요.")
