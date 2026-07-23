# 페이퍼 트레이더 배포 (클라우드)

`engine.live` 페이퍼 트레이더를 VPS 등 클라우드에서 24/7 돌리는 뼈대.
**실주문은 안 나감**(PaperExecutor) — 인프라·재시작·알림을 실돈 전에 안전하게 검증하기 위함.

## 무엇이 도나 (멀티서비스)
`docker-compose.yml`에 **독립 컨테이너 3개** — 하나만 재배포해도 나머지는 안 멈춘다.
- **trader** : `engine.live` 페이퍼 봇 (1분봉 폴링 → 백테스트와 같은 로직 → 페이퍼 매매). 상태를 `data/state.json`에 기록.
- **collector** : `engine.collector --loop` 캔들 수집 (워치리스트 유지). *(trader도 자체 수집함)*
- **dashboard** : `engine.dashboard` 모니터링 웹(:8080) + 봇 멈춤/재개 제어. **백테스트는 프로덕션에 안 올림**(CPU·보안).

```bash
# 로컬(직접 빌드):
docker compose up -d --build              # 전체
docker compose up -d --build dashboard    # 대시보드만 재배포 (trader/collector 계속 돔) ✅
# 프로덕션(EC2, ghcr 이미지 pull — 컴파일 없음):
docker compose pull && docker compose up -d --no-build
```
셋 다 `./data` 볼륨 공유(캔들 캐시 + 봇 상태). 대시보드는 봇과 완전 디커플 — 상태 파일로만 통신.

## 로컬: 백테스트 + 대시보드 한 포트로
연구·백테스트는 **로컬에서** (프로덕션엔 안 올림). 통합 서버가 둘 다 서빙:
```bash
python3 -m engine.server                  # http://localhost:8765
#   /           → 매매 대시보드 (랜딩, /dashboard 별칭도 동작)
#   /backtest   → 백테스트 스튜디오
#   /collector  → 데이터·수집기 관리
```
대시보드만 가볍게 보려면: `python3 -m engine.dashboard --port 8080`.
대시보드 = 잔고·수익률·포지션·최근 트레이드·자산곡선 + 봇/수집기 멈춤·재개. 3초 자동 새로고침.

## 추천 인프라
- **작은 VPS + Docker** (DigitalOcean/Vultr/EC2, ~$5~10/mo).
- **리전은 바이낸스 근처**(도쿄/싱가포르) — 실주문 붙일 때 지연 최소화.
- **EC2 하나로 충분** — trader+collector+dashboard가 워크로드가 가벼움(60초 폴링). 백테스트를
  프로덕션에서 뺀 덕에 CPU 부담이 작다. **권장: t4g.small**(2vCPU·2GB·ARM Graviton, 도쿄 ~$16/mo).
  가성비. x86 무수정이 좋으면 t3.small(2GB). 최저가는 t3.micro(1GB)+스왑(빠듯).
- **페이퍼는 리전 무관** — 실주문이 없어 바이낸스 지연이 무의미. 싼 리전(us-east-1)이면 더 저렴.
  실거래 전환 시 도쿄/싱가포르로 옮긴다.

## ⚠️ 저사양 EC2 안전장치 — 3가지 (t4g.small 2GB 기준)
세 컨테이너(각 numpy+TA-Lib)가 동시에 뜨므로 아래 셋으로 OOM을 막는다.

**① EC2에서 이미지 빌드 금지 → CI가 빌드, EC2는 pull만** (제일 중요)
TA-Lib C 컴파일은 저사양에서 빌드 중 OOM/타임아웃 위험. 그래서 자동배포가 **GitHub Actions에서
arm64 이미지 빌드→ghcr.io push**, EC2는 `docker compose pull`만 한다(아래 "자동 배포" 참고).
EC2에서 `--build`는 쓰지 말 것. **아키텍처 주의: t4g=ARM64** → `deploy.yml`이 `linux/arm64`로
빌드(설정됨). x86 인스턴스(t3)로 바꾸면 `platforms`를 `linux/amd64`로 되돌릴 것.

**② 스왑 2GB — 순간 피크 완충 (권장).** VPS에서 1회:
```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab   # 재부팅 후에도 유지
free -h                                                       # Swap: 2.0Gi 확인
```

**③ 컨테이너 메모리 상한** — `docker-compose.yml`의 `mem_limit`으로 설정
(trader 800M / collector 400M / dashboard 300M ≈ 1.5G < 2GB, OS/도커 여유 ~0.5G).
한 놈이 다 먹고 전체를 죽이는 것 방지. 1GB 인스턴스로 내리면 340/200/160 + 스왑 필수로 조정.

> 요금 감각(온디맨드, 도쿄 대략): t4g.small ~$16 · t3.small ~$20 · t3.micro ~$10 /mo.
> ARM(t4g)이 20~40% 저렴. EBS는 별도(20GB ~$2/mo). 프리티어 종료 후 기준.

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
PRESET=presets/examples/live-strategy.json   # git에 있는 프리셋만 이미지에 들어간다
INTERVAL=60
EQUITY=10000
# (선택) Discord/Slack 웹훅 URL — 진입/청산/에러 알림
NOTIFY_WEBHOOK=
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

## 자동 배포 (prod 브랜치 push → EC2가 스스로 가져감)
`.github/workflows/deploy.yml` — **`main`=개발, `prod`=배포**. `prod`에 push하면
① **GitHub Actions가 이미지 빌드(TA-Lib 컴파일) → ghcr.io에 push.** 여기까지가 CI.
② **EC2가 2분마다 `prod`를 폴링**(`deploy/pull-deploy.sh` + systemd 타이머)해서 새 커밋이면
그 커밋 SHA 이미지로 `.env`의 `IMAGE`를 고정하고 `pull` → `up -d`.
무거운 빌드는 Actions 러너가 하고 **EC2는 완성 이미지만 받는다**.

**배포가 매매 봇을 함부로 죽이지 않는다:** 세 서비스가 같은 이미지를 쓰지만 폴러는
**바뀐 경로가 실제로 영향을 주는 서비스만** 재생성한다(`deploy/service-deps.conf`,
드리프트는 `tests/test_deploy_paths.py`가 감시). 게다가 트레이더는 **포지션 보유 중이면
교체를 연기**하고 무포지션이 될 때까지 기다린다. 즉 대시보드 수정은 포지션을 들고 있어도
바로 배포되고, 봇은 계속 돈다. 강제 교체는 `FORCE_TRADER=1`.

**왜 CI가 SSH로 밀지 않는가:** Actions 러너 IP 대역이 7000개 이상이라 보안그룹(60규칙)으로
허용 불가. 22번을 전체 공개하는 대신 서버가 먼저 물어보게 했다. 덕분에 **GitHub에 서버
접속키를 안 맡겨도 된다**(`VPS_*` 시크릿 전부 불필요).

**VPS 1회 준비:**
```bash
# 서버에서 (Amazon Linux 2023. compose 플러그인은 dnf에 없어 수동 설치 — docs/deploy-checklist.md 단계 4)
sudo dnf install -y docker git && sudo systemctl enable --now docker
# Ubuntu면: sudo apt-get install -y docker.io docker-compose-plugin git
git clone <레포URL> ~/auto_trading && cd ~/auto_trading
# 스왑 2GB (위 "프리티어" 절 참고) — 프리티어면 반드시
cat > .env <<'ENV'
IMAGE=ghcr.io/choozx/coin-coin_v2:latest   # ← ghcr 이미지. 배포 시 커밋 SHA로 자동 고정됨
PRESET=presets/examples/live-strategy.json   # presets/saved 는 이미지에 없다(gitignore)
INTERVAL=60
EQUITY=10000
COLLECT_SYMBOLS=BTCUSDC
NOTIFY_WEBHOOK=
ENV
./deploy/install-poll-timer.sh    # 배포 폴러(systemd 타이머, 2분) 등록
```

**ghcr 이미지 접근:** 레포가 public이면 패키지도 public이라 EC2가 인증 없이 pull 된다.
private로 바꾸면 EC2에서 `read:packages` PAT로 `docker login ghcr.io` 를 1회 해둘 것.

**GitHub Secrets: 없음.** 풀 방식이라 CI가 서버에 접속하지 않는다 — `VPS_*` 시크릿은
등록했다면 삭제. `GITHUB_TOKEN`은 Actions가 ghcr push에 자동 사용(등록 불필요).

**배포하기:**
```bash
git checkout -b prod        # 최초 1회
git push origin prod        # 이후 이 push가 배포 트리거
# 또는 main에서 작업 후:  git push origin main:prod
```

> ⚠️ **실거래(실돈) 트레이더로 바꾸면** 자동배포에 안전장치 필수:
> GitHub Environment 보호규칙(수동 승인) 또는 테스트 통과 게이트를 deploy 앞에 추가.
> 나쁜 push가 곧장 실매매 봇을 갈아치우면 안 됨. (지금은 페이퍼라 자동배포 OK.)

## API 키 배선 (로컬/배포 공통)
키는 **환경변수 3개**로 주입: `BINANCE_API_KEY` · `BINANCE_API_SECRET` · `BINANCE_TESTNET`(1=테스트넷).
저장은 **`.env`(gitignore)에만** — 레포·이미지·GitHub 어디에도 안 들어감. 템플릿: `.env.example`.
- **로컬**: `cp .env.example .env && chmod 600 .env` → 값 채움. `engine.*` 실행 시 자동 로드(`engine/env.py`).
- **배포(EC2)**: 서버의 `.env`에 직접 작성(git clone엔 없음). `docker compose`가 **trader 컨테이너에만** 주입
  (collector·dashboard엔 불필요). 재배포해도 `.env`는 서버에 유지 → 키가 GitHub를 안 거침.
- 키 발급: **Reading+Futures / 출금(Withdrawals) OFF / IP 화이트리스트**. 처음엔 `BINANCE_TESTNET=1`.
- 키가 없으면 `LiveExecutor` 생성 시 명확한 에러 → **페이퍼(`--paper`)로만 동작**(실주문 불가).

## 전략 투입 — 코드 배포 없이 (평상시)

로컬에서 백테스트로 만든 프리셋을 **배포와 무관하게** 돌리고 싶을 때. 봇이 고르는 전략 목록은
세 곳을 스캔한다(`engine/preset.py` `STRATEGY_DIRS`):

| 디렉토리 | 배포 경로 | 용도 |
|---|---|---|
| `presets/examples/` | git → 이미지에 구워짐 | **계속 쓸 전략**. 버전 관리·재현 가능 |
| `presets/saved/` | gitignore, **컨테이너엔 없음** | 로컬 GUI 저장용(프로덕션에서 이 경로를 쓰지 말 것) |
| `data/strategies/` | 공유 볼륨 | **평상시 투입 통로.** 파일만 던지면 목록에 뜬다 |

**A. UI 업로드(커맨드 없이) — 권장.** 배포 대시보드(SSH 터널 `http://localhost:8080`)를 브라우저로
열고 매매 봇의 **`⬆ 가져오기`** → 로컬 프리셋 JSON 선택. 파일 선택창은 항상 로컬 PC 기준이라,
브라우저가 그 파일을 EC2 서버로 올려 `data/strategies/` 에 저장한다(`/api/import_preset`). scp 불필요.
프리셋은 로컬 대시보드의 `프리셋 만들기`로 만들어 로컬 `data/strategies/` 에 둔 것을 그대로 올리면 된다.

**B. scp(수동).**
```bash
# 로컬 → EC2 (재배포·재시작 없음)
scp -i ~/.ssh/key.pem my-strategy.json ec2-user@<EIP>:~/auto_trading/data/strategies/
# 대시보드(SSH 터널 http://localhost:8080) → 전략 목록에 📤 로 뜬다 → 선택
```

선택하면 봇이 다음 폴링에 **무포지션이면 교체**한다(포지션 보유 중이면 청산 후로 미룸 — 안전).
`.env` 를 고치거나 컨테이너를 재시작할 필요가 없다.

> ⚠️ `data/strategies/` 는 git 밖이라 **버전 관리가 안 된다.** 원장(`trades.db`)의 `strategy`
> 컬럼이 이 경로를 가리키므로, 파일을 지우면 나중에 "이 거래는 어떤 전략이 친 건가"를 재현할 수
> 없다(`tools/fill_audit.py` 도 이 경로로 프리셋을 찾는다). **계속 쓸 전략은 `presets/examples/`
> 로 커밋할 것** — 프리셋만 바뀌어도 trader 컨테이너가 자동 교체된다(`trader_extra=^presets/`).

## ⚠️ 실거래(실돈)로 넘어가기 전 반드시
현재는 **페이퍼 전용**(실주문 없음). `LiveExecutor`는 배선 골격만 — env 로드·ccxt 연결·읽기전용
잔고 조회까지 되고, **주문(open/close)은 NotImplementedError**(Tier B에서 구현). 실거래는 아래가 선행:
1. **페이퍼로 며칠 실전 검증** (백테스트 가정 vs 실제 체결)
2. **리스크 가드레일** (일일 손실 한도·kill switch·연속손실 차단)
3. **`LiveExecutor` 주문 구현** — create_order·set_leverage, post-only 지정가→3초 미체결 시 taker,
   체결가로 entry_price/qty/fee 갱신. **먼저 테스트넷(`BINANCE_TESTNET=1`)에서 검증.**
   - 재시작 시 **포지션/잔고는 거래소에서 읽어 동기화**(로컬 상태 맹신 금지)
4. **모니터링/알림** 확실히 (봇이 죽거나 이상하면 즉시 알아야 함)
5. **소액부터** → 점진적 확대
