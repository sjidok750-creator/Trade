"""텔레그램 명령 처리 — 사용자가 봇에게 보낸 메시지를 읽고 응답한다.

지원 명령 (등록된 챗 ID에서 온 것만 처리):
    /status      현황 리포트 즉시 발송
    /positions   보유 내역 상세
    /trend       코인별 추세 판단 상태
    /stop        신규 매수 중단 (STOP 파일 생성)
    /resume      중단 해제
    /help        명령 목록

안전 설계:
- TG_CHAT_ID와 일치하는 발신자만 처리한다 (타인의 명령 무시).
- 매수·매도를 직접 지시하는 명령은 두지 않는다. 사람이 원격으로 주문을
  넣기 시작하면 '규칙 기반 자동매매'라는 전제가 무너진다. 사람이 할 수 있는
  개입은 '멈추는 것'뿐이며, 그것으로 충분하다.
"""
import os

import requests

from . import notify

API = "https://api.telegram.org/bot{token}/{method}"
HELP = """사용 가능한 명령:
/status — 현재 자산·손익 현황
/positions — 보유 코인 상세
/trend — 코인별 추세 판단 상태
/stop — 신규 매수 중단
/resume — 중단 해제
/help — 이 목록"""


def fetch_updates(offset: int) -> tuple[list, int]:
    """새 메시지 목록과 다음 offset을 반환. 실패하면 빈 목록."""
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        return [], offset
    try:
        resp = requests.get(API.format(token=token, method="getUpdates"),
                            params={"offset": offset, "timeout": 0}, timeout=8)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return [], offset
    if not data.get("ok"):
        return [], offset

    msgs, last = [], offset
    my_chat = os.environ.get("TG_CHAT_ID", "")
    for upd in data.get("result", []):
        last = max(last, upd.get("update_id", 0) + 1)
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if text and chat_id and chat_id == my_chat:   # 등록된 사용자만
            msgs.append(text)
    return msgs, last


def handle(text: str, ctx: dict) -> str | None:
    """명령 문자열을 처리하고 응답 메시지를 반환. 모르는 명령이면 None.

    ctx: {"report": 현황문자열 함수, "positions": dict, "prices": dict,
          "trend": dict, "universe": list, "stop_path": str}
    """
    cmd = text.split()[0].lower().lstrip("/")
    cmd = cmd.split("@")[0]        # /status@botname 형태 대응

    if cmd in ("help", "start"):
        return HELP

    if cmd == "status":
        return ctx["report"]()

    if cmd == "positions":
        pos, prices = ctx["positions"], ctx["prices"]
        if not pos:
            return "보유 없음 (전액 현금)"
        lines = ["보유 상세:"]
        for market, p in pos.items():
            price = prices.get(market, p["entry_price"])
            value = p["volume"] * price
            pnl = value - p["krw_spent"]
            rate = pnl / p["krw_spent"] * 100 if p["krw_spent"] else 0.0
            lines.append(
                f"{market.replace('KRW-', '')}: {value:,.0f}원 ({rate:+.1f}%)\n"
                f"  진입 {p['entry_price']:,.0f} → 현재 {price:,.0f}")
        return "\n".join(lines)

    if cmd == "trend":
        trend = ctx["trend"]
        lines = ["추세 판단:"]
        for m in ctx["universe"]:
            mark = "📈 보유대상" if trend.get(m) else "💤 현금대기"
            lines.append(f"{m.replace('KRW-', '')}: {mark}")
        return "\n".join(lines)

    if cmd == "stop":
        open(ctx["stop_path"], "w").close()
        return "🛑 신규 매수를 중단했습니다.\n(보유분 손절·추세 이탈 매도는 계속 동작합니다)\n재개하려면 /resume"

    if cmd == "resume":
        if os.path.exists(ctx["stop_path"]):
            os.remove(ctx["stop_path"])
            return "▶️ 중단을 해제했습니다. 다음 리밸런싱부터 신규 매수가 재개됩니다."
        return "이미 정상 동작 중입니다."

    return None


def poll(ctx: dict, offset: int) -> int:
    """새 명령을 처리하고 다음 offset을 반환."""
    msgs, next_offset = fetch_updates(offset)
    for text in msgs:
        reply = handle(text, ctx)
        if reply is None:
            reply = f"모르는 명령입니다.\n\n{HELP}"
        notify.send(reply)
    return next_offset
