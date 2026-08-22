# 빗썸 자동매매 시스템

계획 전문은 [PLAN.md](PLAN.md) 참고. 운용 규모 100만원, 하이리스크 설정(6코인, 하루 최대 6회 매수).

## 구조

```
exchange/    빗썸 API 2.0 클라이언트 (JWT 인증, 출금 기능 없음)
strategies/  변동성 돌파 전략 (다코인 + 추세 필터)
engine/      24시간 실행 엔진 (리스크 가드, 거래 로그, 텔레그램 알림)
backtest/    데이터 수집 + 백테스터 (수수료·슬리피지 반영)
tests/       오프라인 단위 테스트
config.yaml  전략·리스크 설정 — Claude 튜닝 루프가 이 파일을 조정
```

## 설치 (VPS)

```bash
git clone <repo> && cd Trade
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m tests.test_all        # 전부 OK 확인
```

API 키는 환경변수로만 주입한다 (절대 파일로 커밋 금지):

```bash
export BITHUMB_ACCESS_KEY="..."
export BITHUMB_SECRET_KEY="..."
export TG_BOT_TOKEN="..."   # 선택 (텔레그램 알림)
export TG_CHAT_ID="..."     # 선택
```

## 백테스트

```bash
.venv/bin/python -m backtest.data              # 3년치 일봉 수집 → data/
.venv/bin/python -m backtest.backtester sweep  # k값 0.3~0.8 스윕
.venv/bin/python -m backtest.backtester        # 현재 config 기준 성과
```

## 실행

```bash
.venv/bin/python -m engine.runner
```

- `config.yaml`의 `dry_run: true`(기본)면 **페이퍼 트레이딩** — 실주문 없이 가상 체결.
- 실전 전환은 페이퍼 2주 검증 통과 후 `dry_run: false`로 변경 (사용자 승인 필수).
- **긴급 정지**: 프로젝트 루트에 `touch STOP` → 신규 매수 즉시 중단 (보유분 손절은 계속 동작).
- 킬스위치(총자산 고점 대비 −20%) 발동 시 전량 매도 후 엔진이 스스로 정지한다.

### systemd 상시 실행 (VPS)

```ini
# /etc/systemd/system/trade.service
[Unit]
Description=Bithumb auto trader
After=network-online.target

[Service]
WorkingDirectory=/home/ubuntu/Trade
EnvironmentFile=/home/ubuntu/Trade/.env
ExecStart=/home/ubuntu/Trade/.venv/bin/python -m engine.runner
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now trade
journalctl -u trade -f          # 로그 확인
```

## 안전 장치 요약

| 장치 | 값 | 위치 |
|------|-----|------|
| 출금 권한 | 없음 (API 키 발급 시 미부여) | 거래소 설정 |
| IP 제한 | VPS 고정 IP만 | 거래소 설정 |
| 개별 손절 | −5% | `risk.stop_loss_pct` |
| 일일 손실 한도 | −5% → 당일 매수 중단 | `risk.daily_loss_limit_pct` |
| 킬스위치 | 고점 대비 −20% → 전량 매도+정지 | `risk.kill_switch_pct` |
| 수동 정지 | `touch STOP` | 파일 스위치 |
