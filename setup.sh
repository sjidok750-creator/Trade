#!/usr/bin/env bash
# 빗썸 자동매매 시스템 - 서버 원클릭 설치
set -euo pipefail

REPO="https://github.com/sjidok750-creator/Trade.git"
BRANCH="claude/auto-trading-plan-vqnxhb"
DIR="$HOME/Trade"

echo "==> 1/5 시스템 패키지 설치"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git nano

echo "==> 2/5 코드 내려받기"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch origin "$BRANCH"
  git -C "$DIR" checkout "$BRANCH"
  git -C "$DIR" pull origin "$BRANCH"
else
  git clone -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"

echo "==> 3/5 파이썬 환경 구성"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> 4/5 단위 테스트"
.venv/bin/python -m tests.test_all 2>&1 | tail -3

echo "==> 5/5 키 파일 생성"
if [ ! -f .env ]; then
  printf 'BITHUMB_ACCESS_KEY=\nBITHUMB_SECRET_KEY=\nTG_BOT_TOKEN=\nTG_CHAT_ID=\n' > .env
  chmod 600 .env
  echo "   .env 생성됨"
else
  echo "   .env 이미 존재 (유지)"
fi

echo ""
echo "==========================================="
echo "설치 완료. 다음 순서:"
echo "  1) nano ~/Trade/.env      키 입력 후 Ctrl+O, Enter, Ctrl+X"
echo "  2) cd ~/Trade"
echo "  3) set -a; . ./.env; set +a"
echo "  4) .venv/bin/python -m engine.notify   텔레그램 연결 확인"
echo "  5) .venv/bin/python -m backtest.data   시세 수집"
echo "  6) .venv/bin/python -m backtest.backtester sweep"
echo "==========================================="
