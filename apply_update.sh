#!/usr/bin/env bash
# 실전 모드에서 config.yaml이 바뀐 업데이트를 수동 반영한다.
# (autoupdate는 실전 모드의 설정 변경을 일부러 보류한다 — 사용자 승인 필요)
#
# 실행 (서버):  cd ~/Trade && git fetch -q origin claude/auto-trading-plan-vqnxhb && \
#               git show origin/claude/auto-trading-plan-vqnxhb:apply_update.sh | bash
#
# 하는 일:
#  1) 자동 업데이트 타이머 정지 (도중에 끼어들지 않게)
#  2) 현재 config.yaml 백업 → 새 버전 병합 → 실전 모드(dry_run: false) 유지
#  3) 테스트 통과 시에만 엔진 재시작
#  실패하면 코드·설정을 원래대로 되돌리고 엔진은 건드리지 않는다.
set -uo pipefail
BRANCH="claude/auto-trading-plan-vqnxhb"
DIR="${TRADE_DIR:-$HOME/Trade}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
cd "$DIR" || { echo "작업 디렉터리 없음: $DIR"; exit 1; }

BACKUP="config.backup.$(date +%Y%m%d_%H%M%S).yaml"
OLD_HEAD=$(git rev-parse HEAD)
WAS_LIVE=0
grep -qE '^[[:space:]]*dry_run:[[:space:]]*false' config.yaml && WAS_LIVE=1

rollback() {
  echo "!! $1 — 원래대로 되돌립니다"
  git reset -q --hard "$OLD_HEAD"
  cp "$BACKUP" config.yaml
  $SYSTEMCTL start autoupdate.timer
  echo "!! 복구 완료: 코드 ${OLD_HEAD:0:7}, 설정은 백업본. 엔진은 기존 그대로 동작 중."
  exit 1
}

echo "== 1/5 자동 업데이트 일시 정지"
$SYSTEMCTL stop autoupdate.timer

echo "== 2/5 설정 백업: $BACKUP"
cp config.yaml "$BACKUP"

echo "== 3/5 새 버전 병합"
git fetch -q origin "$BRANCH" || rollback "git fetch 실패"
git checkout -q -- config.yaml
git merge -q --ff-only "origin/$BRANCH" || rollback "병합 실패"
if [ "$WAS_LIVE" = 1 ]; then
  sed -i 's/^\([[:space:]]*\)dry_run:[[:space:]]*true/\1dry_run: false/' config.yaml
  grep -qE '^[[:space:]]*dry_run:[[:space:]]*false' config.yaml || rollback "실전 모드 유지 실패"
fi

echo "== 4/5 테스트"
.venv/bin/python -m tests.test_all >/tmp/apply_update_test.log 2>&1 \
  || { tail -20 /tmp/apply_update_test.log; rollback "테스트 실패"; }
echo "   통과"

echo "== 5/5 엔진 재시작"
$SYSTEMCTL restart trade || rollback "엔진 재시작 실패"
sleep 3
$SYSTEMCTL is-active trade >/dev/null || rollback "엔진이 켜지지 않음"
$SYSTEMCTL start autoupdate.timer

echo ""
echo "완료: ${OLD_HEAD:0:7} → $(git rev-parse --short HEAD) ($(git log --format=%s -1))"
echo "--- 바뀐 설정 (백업 대비) ---"
CHANGES=$(diff <(grep -vE '^\s*#' "$BACKUP" | sed 's/ *#.*//') \
               <(grep -vE '^\s*#' config.yaml | sed 's/ *#.*//') | grep -E '^[<>]')
echo "${CHANGES:-(없음)}"
