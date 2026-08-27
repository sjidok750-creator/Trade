"""실전 전환 전 점검 — 실행: .venv/bin/python -m engine.preflight

돈이 걸리기 전에 확인해야 할 것을 전부 검사하고 통과/실패를 출력한다.
하나라도 FAIL이면 실전 전환을 진행하면 안 된다 (golive.sh가 여기서 멈춘다).
"""
import os
import sys

import yaml

from exchange.bithumb import Bithumb, BithumbError
from . import commands, fee_guard

OK, FAIL, WARN = "✅", "❌", "⚠️ "


def check(label: str, ok: bool, detail: str = "", warn: bool = False) -> bool:
    mark = OK if ok else (WARN if warn else FAIL)
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))
    return ok or warn


def main() -> int:
    print("=== 실전 전환 사전 점검 ===\n")
    good = True

    # 1. 설정 파일
    try:
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)
        good &= check("config.yaml 로드", True)
    except Exception as e:
        check("config.yaml 로드", False, str(e))
        return 1

    dry = cfg["mode"]["dry_run"]
    check("현재 모드", True, "페이퍼 (전환 전 정상)" if dry else "이미 실전 모드")

    # 2. API 키 존재
    access = os.environ.get("BITHUMB_ACCESS_KEY", "")
    secret = os.environ.get("BITHUMB_SECRET_KEY", "")
    good &= check("빗썸 API 키 입력", bool(access and secret),
                  "" if access and secret else ".env에 BITHUMB_ACCESS_KEY/SECRET_KEY 필요")

    # 3. API 인증 + 잔고 (키가 있을 때만)
    krw = 0.0
    if access and secret:
        try:
            ex = Bithumb()
            balances = ex.get_balances()
            krw = balances.get("KRW", {}).get("balance", 0.0)
            good &= check("빗썸 API 인증", True, "자산 조회 성공")
            good &= check("원화 잔고", krw >= 900_000,
                          f"{krw:,.0f}원 (권장 100만원)", warn=krw >= 500_000)
            coins = [c for c, b in balances.items()
                     if c != "KRW" and b.get("balance", 0) > 0]
            check("기존 보유 코인", not coins,
                  "없음 (깨끗한 시작)" if not coins
                  else f"{coins} — 시스템 관리 밖 자산, 정리 권장", warn=True)
        except BithumbError as e:
            good &= check("빗썸 API 인증", False,
                          f"{e} — 키 권한(자산조회+주문)과 IP 등록 확인")

    # 4. 텔레그램
    problem = commands.config_problem()
    good &= check("텔레그램 설정", problem is None, problem or "발송·명령 수신 가능")

    # 5. 수수료 쿠폰
    renewed = cfg.get("fee", {}).get("coupon_renewed_on")
    left = fee_guard.days_left(renewed)
    good &= check("수수료 쿠폰(0.04%)", left is not None and left > 3,
                  f"신청일 {renewed}, 잔여 {left}일" if left is not None
                  else "coupon_renewed_on 미설정", warn=(left or 0) > 0)

    # 6. 리스크 설정 확인
    r = cfg["risk"]
    good &= check("킬스위치", 0 < r["kill_switch_pct"] <= 0.5,
                  f"고점 대비 -{r['kill_switch_pct']*100:.0f}%")
    good &= check("개별 손절", 0 < r["stop_loss_pct"] <= 0.3,
                  f"-{r['stop_loss_pct']*100:.0f}%")

    print()
    if good:
        print("전 항목 통과 — 실전 전환 가능. 다음: bash golive.sh")
        return 0
    print("FAIL 항목을 해결한 뒤 다시 실행하세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
