"""빗썸 Open API 2.0 클라이언트.

- 공개 API: 인증 불필요 (시세/캔들)
- 개인 API: JWT 인증 (access key + secret key, 파라미터는 SHA512 query_hash)
- 키는 환경변수 BITHUMB_ACCESS_KEY / BITHUMB_SECRET_KEY 로만 주입한다.

주의: 엔드포인트/파라미터는 apidocs.bithumb.com 기준이며, 실서버 검증은
VPS에서 최소 주문으로 수행한다 (로드맵 1단계 통과 기준).
"""
import hashlib
import os
import time
import uuid as uuid_lib
from urllib.parse import urlencode

import jwt  # PyJWT
import requests

from .base import Exchange

BASE_URL = "https://api.bithumb.com"


class BithumbError(Exception):
    pass


class Bithumb(Exchange):
    def __init__(self, access_key: str | None = None, secret_key: str | None = None,
                 timeout: int = 10):
        self.access_key = access_key or os.environ.get("BITHUMB_ACCESS_KEY", "")
        self.secret_key = secret_key or os.environ.get("BITHUMB_SECRET_KEY", "")
        self.timeout = timeout
        self.session = requests.Session()

    # ---------- 내부 ----------
    def _auth_headers(self, params: dict | None = None) -> dict:
        if not self.access_key or not self.secret_key:
            raise BithumbError("API 키가 없습니다 (BITHUMB_ACCESS_KEY/SECRET_KEY 환경변수 확인)")
        payload = {
            "access_key": self.access_key,
            "nonce": str(uuid_lib.uuid4()),
            "timestamp": round(time.time() * 1000),
        }
        if params:
            query = urlencode(params).encode()
            payload["query_hash"] = hashlib.sha512(query).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, self.secret_key)
        return {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, path: str, params: dict | None = None,
                 auth: bool = False, retries: int = 3) -> dict | list:
        url = BASE_URL + path
        last_err = None
        for attempt in range(retries):
            try:
                headers = self._auth_headers(params) if auth else {}
                if method == "GET":
                    resp = self.session.get(url, params=params, headers=headers,
                                            timeout=self.timeout)
                else:
                    resp = self.session.post(url, json=params, headers=headers,
                                             timeout=self.timeout)
                if resp.status_code == 429:  # 요청 한도 초과 → 백오프
                    time.sleep(2 ** attempt)
                    continue
                data = resp.json()
                if resp.status_code >= 400:
                    raise BithumbError(f"{path} HTTP {resp.status_code}: {data}")
                if isinstance(data, dict) and data.get("error"):
                    raise BithumbError(f"{path}: {data['error']}")
                return data
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
                time.sleep(2 ** attempt)
        raise BithumbError(f"{path} 요청 실패 (재시도 {retries}회 소진): {last_err}")

    # ---------- 공개 API ----------
    def get_markets(self) -> list[str]:
        data = self._request("GET", "/v1/market/all")
        return [m["market"] for m in data if m["market"].startswith("KRW-")]

    def get_tickers(self, markets: list[str]) -> dict[str, float]:
        data = self._request("GET", "/v1/ticker", {"markets": ",".join(markets)})
        return {t["market"]: float(t["trade_price"]) for t in data}

    def get_daily_candles(self, market: str, count: int = 200, to: str | None = None) -> list[dict]:
        params = {"market": market, "count": count}
        if to:
            params["to"] = to
        return self._request("GET", "/v1/candles/days", params)

    # ---------- 개인 API ----------
    def get_balances(self) -> dict[str, dict]:
        data = self._request("GET", "/v1/accounts", auth=True)
        return {
            a["currency"]: {
                "balance": float(a["balance"]) + float(a.get("locked", 0)),
                "avg_buy_price": float(a.get("avg_buy_price", 0) or 0),
            }
            for a in data
        }

    def buy_market(self, market: str, krw_amount: float) -> dict:
        # ord_type=price: 원화 금액 지정 시장가 매수
        params = {"market": market, "side": "bid",
                  "price": str(int(krw_amount)), "ord_type": "price"}
        return self._request("POST", "/v1/orders", params, auth=True)

    def sell_market(self, market: str, volume: float) -> dict:
        # ord_type=market: 수량 지정 시장가 매도
        params = {"market": market, "side": "ask",
                  "volume": f"{volume:.8f}", "ord_type": "market"}
        return self._request("POST", "/v1/orders", params, auth=True)

    def get_order(self, uuid: str) -> dict:
        return self._request("GET", "/v1/order", {"uuid": uuid}, auth=True)
