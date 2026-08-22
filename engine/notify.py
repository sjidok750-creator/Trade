"""텔레그램 알림 (선택). TG_BOT_TOKEN / TG_CHAT_ID 환경변수 필요."""
import os

import requests


def send(message: str, enabled: bool = True):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not enabled or not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
    except requests.RequestException:
        pass  # 알림 실패가 거래를 막아서는 안 된다
