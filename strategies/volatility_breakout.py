"""다코인 변동성 돌파 전략.

일일 리셋 시각(KST 00:00 기준)에 코인별 목표가를 계산하고,
당일 현재가가 목표가를 넘으면 매수 신호를 낸다. 다음 리셋 때 전량 청산.

목표가 = 당일 시가 + (전일 고가 - 전일 저가) * k
추세 필터: 당일 시가가 최근 N일 종가 이동평균보다 높을 때만 진입 허용.
"""
from dataclasses import dataclass


@dataclass
class Signal:
    market: str
    target_price: float      # 돌파 목표가
    trend_ok: bool           # 추세 필터 통과 여부

    def should_buy(self, current_price: float) -> bool:
        return self.trend_ok and current_price >= self.target_price


def compute_signal(market: str, candles: list[dict], k: float,
                   trend_filter_ma: int = 0) -> Signal | None:
    """candles: 최신순 일봉. candles[0]이 오늘(진행 중), candles[1]이 전일."""
    if len(candles) < max(2, trend_filter_ma + 1):
        return None
    today, yesterday = candles[0], candles[1]
    today_open = float(today["opening_price"])
    prev_range = float(yesterday["high_price"]) - float(yesterday["low_price"])
    if prev_range <= 0:
        return None
    target = today_open + prev_range * k

    trend_ok = True
    if trend_filter_ma > 0:
        closes = [float(c["trade_price"]) for c in candles[1:trend_filter_ma + 1]]
        ma = sum(closes) / len(closes)
        trend_ok = today_open > ma

    return Signal(market=market, target_price=target, trend_ok=trend_ok)
