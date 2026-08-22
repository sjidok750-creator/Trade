"""텔레그램 알림.

환경변수: TG_BOT_TOKEN (필수), TG_CHAT_ID (없으면 자동 조회)

단독 실행 시 챗 ID를 자동으로 찾아 테스트 메시지를 보낸다:
    python -m engine.notify
"""
import os
import sys

import requests

API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str:
    return os.environ.get("TG_BOT_TOKEN", "")


def discover_chat_id(token: str) -> str | None:
    """봇에게 보낸 최근 메시지에서 챗 ID를 찾는다 (사전에 /start 전송 필요)."""
    try:
        resp = requests.get(API.format(token=token, method="getUpdates"), timeout=10)
        data = resp.json()
    except requests.RequestException as e:
        print(f"텔레그램 접속 실패: {e}")
        return None
    if not data.get("ok"):
        print(f"토큰 오류: {data.get('description')}")
        return None
    for update in reversed(data.get("result", [])):
        msg = update.get("message") or update.get("channel_post") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id:
            return str(chat_id)
    return None


def send(message: str, enabled: bool = True):
    token, chat_id = _token(), os.environ.get("TG_CHAT_ID")
    if not enabled or not token or not chat_id:
        return
    try:
        requests.post(API.format(token=token, method="sendMessage"),
                      json={"chat_id": chat_id, "text": message}, timeout=5)
    except requests.RequestException:
        pass  # 알림 실패가 거래를 막아서는 안 된다


def _setup():
    token = _token()
    if not token:
        print("TG_BOT_TOKEN 환경변수가 없습니다. .env를 확인하세요.")
        return 1
    chat_id = os.environ.get("TG_CHAT_ID") or discover_chat_id(token)
    if not chat_id:
        print("챗 ID를 찾지 못했습니다. 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
        return 1
    os.environ["TG_CHAT_ID"] = chat_id
    send("✅ 자동매매 시스템 연결 성공. 이 채팅으로 매매 알림이 전송됩니다.")
    print(f"챗 ID: {chat_id}")
    print(f"텔레그램을 확인하세요. 메시지가 왔다면 .env의 TG_CHAT_ID={chat_id} 로 저장하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(_setup())
