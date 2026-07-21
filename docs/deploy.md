# 페이퍼 트레이더 배포 (클라우드)

`engine.live` 페이퍼 트레이더를 VPS 등 클라우드에서 24/7 돌리는 뼈대.
**실주문은 안 나감**(PaperExecutor) — 인프라·재시작·알림을 실돈 전에 안전하게 검증하기 위함.

## 무엇이 도나
- GUI/백테스트 아님. `python -m engine.live <preset> --paper --interval 60` 루프 하나.
- 1분봉을 주기 폴링 → 백테스트와 같은 로직으로 페이퍼 매매 → 잔고/포지션 로그.

## 추천 인프라
- **작은 VPS + Docker** (DigitalOcean/Vultr/EC2, ~$5~10/mo).
- **리전은 바이낸스 근처**(도쿄/싱가포르) — 실주문 붙일 때 지연 최소화.

## 로컬에서 먼저 (Docker 없이)
```bash
brew install ta-lib && pip install -r requirements.txt      # C 라이브러리 + 파이썬
python3 -m engine.live presets/examples/rsi-oversold-long.json --paper --once   # 한 번 테스트
python3 -m engine.live presets/examples/rsi-oversold-long.json --paper --interval 60   # 상시
```

## Docker로 (배포)
```bash
# 1) 설정 (.env — gitignore됨)
cat > .env <<'ENV'
PRESET=presets/saved/내전략.json
INTERVAL=60
EQUITY=10000
NOTIFY_WEBHOOK=            # (선택) Discord/Slack 웹훅 URL — 진입/청산/에러 알림
ENV

# 2) 빌드 & 실행 (백그라운드, 자동 재시작)
docker compose up -d --build

# 3) 로그 보기
docker compose logs -f paper

# 4) 중지
docker compose down
```

- **캔들 캐시**는 `./data` 볼륨에 지속 → 재시작해도 워밍업 빠름.
- **프리셋**은 `./presets`(saved/ 포함) 마운트 → GUI에서 저장한 프리셋 그대로 사용.
- **TA-Lib 빌드 실패 시**: `Dockerfile`의 `TALIB_VERSION`을 릴리스 태그에 맞게 조정
  (github.com/ta-lib/ta-lib/releases). 0.4.0 소스로 교체해도 됨.

## 알림 (선택)
`NOTIFY_WEBHOOK`에 Discord/Slack 웹훅 URL을 넣으면 시작·진입·청산·에러를 받는다(stdlib만 사용).

## 자동 배포 (prod 브랜치 push → VPS)
`.github/workflows/deploy.yml` — **`main`=개발, `prod`=배포**. `prod`에 push하면
① Docker 이미지 빌드 검증(TA-Lib 게이트) → ② VPS에 SSH로 `git pull` + `docker compose up -d --build`.

**VPS 1회 준비:**
```bash
# 서버에서
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git
git clone <레포URL> ~/auto_trading && cd ~/auto_trading
cat > .env <<'ENV'
PRESET=presets/saved/내전략.json
INTERVAL=60
NOTIFY_WEBHOOK=
ENV
# 공개키 등록: GitHub Actions가 쓸 키의 pubkey를 ~/.ssh/authorized_keys 에
```

**GitHub Secrets** (Settings → Secrets and variables → Actions):
`VPS_HOST` · `VPS_USER` · `VPS_SSH_KEY`(개인키 전체) · `VPS_PATH`(예: `/home/ubuntu/auto_trading`) · `VPS_PORT`(선택).

**배포하기:**
```bash
git checkout -b prod        # 최초 1회
git push origin prod        # 이후 이 push가 배포 트리거
# 또는 main에서 작업 후:  git push origin main:prod
```

> ⚠️ **실거래(실돈) 트레이더로 바꾸면** 자동배포에 안전장치 필수:
> GitHub Environment 보호규칙(수동 승인) 또는 테스트 통과 게이트를 deploy 앞에 추가.
> 나쁜 push가 곧장 실매매 봇을 갈아치우면 안 됨. (지금은 페이퍼라 자동배포 OK.)

## ⚠️ 실거래(실돈)로 넘어가기 전 반드시
현재는 **페이퍼 전용**(실주문 없음). 실거래는 아래가 선행되어야 안전:
1. **페이퍼로 며칠 실전 검증** (백테스트 가정 vs 실제 체결)
2. **리스크 가드레일** (일일 손실 한도·kill switch·연속손실 차단)
3. **`LiveExecutor`(ccxt) 실구현** — 주문/체결/잔고 동기화
   - **API 키는 절대 레포·compose에 X** → `.env`(gitignore)/시크릿매니저
   - 바이낸스 키: **출금 비활성 + IP 화이트리스트** 필수
   - 재시작 시 **포지션/잔고는 거래소에서 읽어 동기화**(로컬 상태 맹신 금지)
4. **모니터링/알림** 확실히 (봇이 죽거나 이상하면 즉시 알아야 함)
5. **소액부터** → 점진적 확대
