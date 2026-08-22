#!/usr/bin/env bash
# 자동 업데이트 — GitHub에 새 커밋이 있으면 테스트 후 적용하고 엔진을 재시작한다.
# systemd 타이머가 10분마다 실행하며, 테스트 실패 시 기존 버전을 유지한다.
set -uo pipefail

DIR="$HOME/Trade"
BRANCH="claude/auto-trading-plan-vqnxhb"
cd "$DIR" || exit 1

notify() {
  set -a; . ./.env 2>/dev/null; set +a
  .venv/bin/python -m engine.notify "$1" >/dev/null 2>&1
}

git fetch -q origin "$BRANCH" 2>/dev/null || exit 0
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
[ "$LOCAL" = "$REMOTE" ] && exit 0          # 변경 없음

# 실전 모드에서 전략·리스크 설정이 바뀌면 자동 적용하지 않는다 (사용자 승인 필요)
if grep -qE '^[[:space:]]*dry_run:[[:space:]]*false' config.yaml; then
  if git diff --name-only "$LOCAL" "$REMOTE" | grep -qx 'config.yaml'; then
    notify "⚠️ 실전 모드: config.yaml 변경이 포함된 업데이트가 있어 자동 적용을 보류했습니다. 검토 후 수동 반영하세요."
    exit 0
  fi
fi

SUBJECT=$(git log --format=%s -1 "$REMOTE")
git merge -q --ff-only "origin/$BRANCH" || {
  notify "🚨 자동 업데이트 실패: 병합 불가. 수동 확인이 필요합니다."
  exit 1
}
.venv/bin/pip install -q -r requirements.txt 2>/dev/null

if ! .venv/bin/python -m tests.test_all >/tmp/autoupdate_test.log 2>&1; then
  git reset -q --hard "$LOCAL"              # 테스트 실패 → 이전 버전으로 되돌림
  notify "🚨 업데이트 테스트 실패 — 이전 버전을 유지합니다. ($SUBJECT)"
  exit 1
fi

systemctl restart trade
notify "🔄 시스템 업데이트 적용: $SUBJECT"
