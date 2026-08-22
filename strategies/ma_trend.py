"""이동평균 추세추종 (연구실 검증 통과 전략).

주 1회, 코인별로 "어제 종가가 직전 MA_LEN일 이동평균 위인가"를 판단해
위이면 보유, 아래면 현금. 밴드(이력대)로 경계선 휩쏘를 막는다.

강건성 스윕(MA 10~100 × 밴드 0~3% × 리밸 3/7일, 30조합) 결과
대부분이 BuyHold를 이겼고 MA30/밴드3%가 최적이었다 (2023-08~2026-08).
"""


def update_state(candles: list[dict], ma_len: int, band: float, prev: bool) -> bool:
    """candles: 최신순 일봉. [0]은 진행 중인 오늘이므로 제외.

    어제 종가(candles[1])를 그 이전 ma_len일 종가 평균과 비교한다.
    밴드 안쪽(MA±band)에서는 직전 상태를 유지한다.
    """
    if len(candles) < ma_len + 2:
        return prev
    price = float(candles[1]["trade_price"])
    closes = [float(c["trade_price"]) for c in candles[2:ma_len + 2]]
    ma = sum(closes) / ma_len
    if price > ma * (1 + band):
        return True
    if price < ma * (1 - band):
        return False
    return prev
