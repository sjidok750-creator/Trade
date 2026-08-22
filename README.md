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

Vultr 콘솔의 **View Console** 버튼(웹 터미널) 또는 SSH 앱으로 서버에 접속한 뒤,
아래 한 줄을 붙여넣으면 파이썬 환경 구성부터 테스트까지 자동으로 끝난다.

```bash
curl -fsSL https://raw.githubusercontent.com/sjidok750-creator/Trade/claude/auto-trading-plan-vqnxhb/setup.sh | bash
```

설치가 끝나면 키를 입력한다 (`.env`는 `.gitignore`에 있어 커밋되지 않는다):

```bash
nano ~/Trade/.env
```

```
BITHUMB_ACCESS_KEY=발급받은_액세스_키
BITHUMB_SECRET_KEY=발급받은_시크릿_키
TG_BOT_TOKEN=텔레그램_봇_토큰
TG_CHAT_ID=
```

저장은 `Ctrl+O` → `Enter` → `Ctrl+X`. 이후 환경변수를 불러온다:

```bash
cd ~/Trade && set -a && . ./.env && set +a
```

### 텔레그램 연결 확인

```bash
.venv/bin/python -m engine.notify
```

챗 ID를 자동으로 찾아 테스트 메시지를 보낸다. 메시지가 도착하면 출력된 챗 ID를
`.env`의 `TG_CHAT_ID=`에 적어둔다 (다음 실행부터 조회 생략).

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

`trade.service`가 레포에 포함되어 있다. 접속이 끊겨도 계속 돌고, 서버가
재부팅돼도 자동으로 다시 뜬다.

```bash
cp ~/Trade/trade.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now trade
```

```bash
systemctl status trade      # 상태 확인
journalctl -u trade -f      # 실시간 로그 (Ctrl+C로 빠져나옴)
systemctl stop trade        # 정지
```

### 자동 업데이트 (선택)

GitHub에 새 커밋이 올라오면 서버가 스스로 받아 적용한다. 10분마다 확인하며,
**테스트 17건을 통과해야만** 반영하고 실패하면 이전 버전으로 되돌린 뒤 알린다.

```bash
cp ~/Trade/autoupdate.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now autoupdate.timer
```

```bash
systemctl list-timers autoupdate.timer   # 다음 실행 시각
journalctl -u autoupdate -n 20           # 업데이트 이력
systemctl disable --now autoupdate.timer # 자동 업데이트 중지
```

실전 모드(`dry_run: false`)에서는 `config.yaml`(전략·리스크 한도) 변경이 포함된
업데이트를 자동 적용하지 않고 보류 알림만 보낸다 — 돈이 걸린 설정은 사람이 승인한다.

## 안전 장치 요약

| 장치 | 값 | 위치 |
|------|-----|------|
| 출금 권한 | 없음 (API 키 발급 시 미부여) | 거래소 설정 |
| IP 제한 | VPS 고정 IP만 | 거래소 설정 |
| 개별 손절 | −5% | `risk.stop_loss_pct` |
| 일일 손실 한도 | −5% → 당일 매수 중단 | `risk.daily_loss_limit_pct` |
| 킬스위치 | 고점 대비 −20% → 전량 매도+정지 | `risk.kill_switch_pct` |
| 수동 정지 | `touch STOP` | 파일 스위치 |
| 수수료 쿠폰 만료 | 만료 3일 전 경고 + 체결 수수료 역산 감지 | `fee.coupon_renewed_on` |

빗썸의 0.04% 쿠폰은 **신청일로부터 30일**만 유효하다. 재신청 후에는
`config.yaml`의 `fee.coupon_renewed_on` 날짜를 갱신해야 경고가 정확해진다.
