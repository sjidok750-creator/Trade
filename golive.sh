#!/usr/bin/env bash
# 실전 전환 — 사전 점검 통과 시에만 진행한다.
#
# 하는 일:
#  1) preflight 전 항목 통과 확인 (실패 시 아무것도 바꾸지 않음)
#  2) 엔진 정지
#  3) 페이퍼 상태 백업 후 초기화 — 가상 포지션·가상 고점이 실전으로 새어
#     들어오면 존재하지 않는 코인을 팔거나 킬스위치 기준이 어긋난다
#  4) dry_run: false 로 전환
#  5) 엔진 시작 + 텔레그램 통지
set -euo pipefail
cd "$HOME/Trade"

set -a; . ./.env; set +a

echo "== 1/5 사전 점검 =="
.venv/bin/python -m engine.preflight || { echo "점검 실패 — 전환 중단"; exit 1; }

echo "== 2/5 엔진 정지 =="
systemctl stop trade

echo "== 3/5 페이퍼 상태 백업·초기화 =="
STAMP=$(date +%Y%m%d_%H%M%S)
if [ -d state ]; then
  mv state "state_paper_backup_$STAMP"
  echo "   백업: state_paper_backup_$STAMP"
fi
mkdir -p state

echo "== 4/5 실전 모드 전환 =="
sed -i 's/dry_run: true/dry_run: false/' config.yaml
grep -n "dry_run" config.yaml | head -1

echo "== 5/5 엔진 시작 =="
systemctl start trade
sleep 3
systemctl is-active trade

.venv/bin/python -m engine.notify "🚀 실전 모드 시작. 실제 주문이 나갑니다. 긴급 중단: /stop" || true
echo ""
echo "실전 전환 완료. journalctl -u trade -f 로 첫 리밸런싱을 지켜보세요."
