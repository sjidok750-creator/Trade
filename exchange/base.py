"""거래소 추상 인터페이스.

빗썸 API 2.0은 업비트와 스펙이 거의 동일하므로, 이 인터페이스를 기준으로
어댑터를 추가하면 업비트 확장이 가능하다.
"""
from abc import ABC, abstractmethod


class Exchange(ABC):
    # ---- 공개 API (인증 불필요) ----
    @abstractmethod
    def get_tickers(self, markets: list[str]) -> dict[str, float]:
        """{market: 현재가}"""

    @abstractmethod
    def get_daily_candles(self, market: str, count: int = 200, to: str | None = None) -> list[dict]:
        """일봉 목록 (최신순). 각 항목: opening_price/high_price/low_price/trade_price/candle_date_time_kst"""

    # ---- 개인 API (인증 필요) ----
    @abstractmethod
    def get_balances(self) -> dict[str, dict]:
        """{통화: {"balance": float, "avg_buy_price": float}} — KRW 포함"""

    @abstractmethod
    def buy_market(self, market: str, krw_amount: float) -> dict:
        """시장가 매수 (원화 금액 지정)"""

    @abstractmethod
    def sell_market(self, market: str, volume: float) -> dict:
        """시장가 매도 (수량 지정)"""

    @abstractmethod
    def get_order(self, uuid: str) -> dict:
        """주문 단건 조회 (체결 확인용)"""
