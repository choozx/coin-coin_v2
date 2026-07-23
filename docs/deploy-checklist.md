# 🚀 배포 준비 체크리스트 (페이퍼 트레이더 → EC2 t4g.small)

현재는 **페이퍼 트레이더**(실주문 없음) 배포라 **바이낸스 API 키가 필요 없습니다**.
아래를 위→아래 순서로 진행하면 `git push origin main:prod` 한 번으로 배포됩니다.
실거래(실돈) 준비는 **단계 8**에 따로 정리.

> 진행 소요: 전체 약 1시간 (AWS 처음이면 계정 생성 포함 ~1.5시간)

---

## ✅ 단계 0 — 배포 대상 프리셋을 레포에 포함  *(완료됨)*

- [x] 배포용 프리셋을 추적 경로로 커밋 → `presets/examples/live-strategy.json`

> **함정:** `presets/saved/` 는 `.gitignore` 라 GUI 저장 프리셋은 이미지·EC2에 안 올라감.
> 이미 처리 완료(커밋 `a49ab85`, 동적 레버리지 전략). `.env`의 `PRESET` 이 이 파일을 가리키면 됨.

---

## 단계 1 — AWS EC2 인스턴스 (유료, 프리티어 종료)   `AWS · 15분`

- [ ] EC2 콘솔 → **Launch instance**
- [ ] 인스턴스: **t4g.small** (2GB·ARM Graviton) · Amazon Linux 2023

**방법**
- **리전**(우상단): 페이퍼는 지연 무관 → 싼 `us-east-1`도 OK. 실거래 전환 시 `ap-northeast-1`(도쿄)
- **AMI**: Amazon Linux 2023 — **Arm 아키텍처(64-bit ARM)** 선택 (t4g는 ARM!)
- **유형**: **`t4g.small`** (2vCPU·2GB·ARM, 도쿄 ~$16/mo) — 가성비 선택
  - x86 무수정 원하면 `t3.small`(2GB, ~$20) / 최저가는 `t3.micro`(1GB, ~$10)+스왑
- **스토리지**: 8GB → **20GB** (gp3, ~$2/mo)

> **OS는 무관:** 컨테이너가 `python:3.11-slim`(Debian) 기반이고 이미지는 Actions에서 빌드해
> ghcr에서 pull만 하므로, 호스트는 Docker만 돌면 됨. 이 문서는 **AL2023 기준**이고
> Ubuntu 22.04를 쓰면 단계 3~5의 `ec2-user`→`ubuntu`, 단계 4의 `dnf`→`apt-get`만 바꾸면 됨
> (Ubuntu는 `docker-compose-plugin` 패키지가 apt에 있어 compose 수동설치 불필요).

> ⚠️ **아키텍처 주의:** t4g = **ARM64**. `deploy.yml`이 `linux/arm64`로 빌드하도록 이미 세팅됨.
> 나중에 t3(x86)로 바꾸면 `deploy.yml`의 `platforms`를 `linux/amd64`로 되돌릴 것.
> 요금: t4g.small ~$16 · t3.small ~$20 · t3.micro ~$10 /mo (도쿄, EBS 별도).

---

## 단계 2 — 네트워크: VPC · 고정 IP · 방화벽   `AWS · 10분`

- [ ] VPC/서브넷: **기본 VPC + 퍼블릭 서브넷**, 퍼블릭 IP 자동할당 **켜기**
- [ ] **탄력적 IP(EIP) 할당 → 인스턴스에 연결** (IP 고정)
- [ ] 인바운드: SSH(22)만 **My IP**. 대시보드 8080은 **열지 않음**(SSH 터널로 접근)
- [ ] 아웃바운드: 기본값(전체 허용) 그대로 둘 것

### 2-1. VPC — 기본 VPC로 충분

인스턴스 시작 화면의 Network settings 기본값(기본 VPC · 퍼블릭 서브넷 · 퍼블릭 IP 자동할당)
그대로 두면 됨. **NAT 게이트웨이·프라이빗 서브넷은 쓰지 말 것** — NAT만 월 ~$35로 인스턴스보다
비싸고, 봇은 아웃바운드(바이낸스 API)만 필요해서 이득이 없음.

### 2-2. 탄력적 IP — 붙여두는 게 이득

EC2 → Elastic IPs → Allocate → Associate to instance.

- **왜:** 인스턴스를 stop/start 하면 자동할당 퍼블릭 IP가 **바뀐다.** 바뀌면
  `VPS_HOST` 시크릿·SSH 접속·(단계 8) **바이낸스 API 키 IP 화이트리스트**가 전부 깨짐.
- **비용:** 2024년부터 퍼블릭 IPv4는 자동할당이든 EIP든 똑같이 ~$3.6/mo 과금 → **EIP로 바꿔도 추가비용 없음.**
- ⚠️ 인스턴스를 종료(terminate)하면 EIP는 **미연결 상태로도 계속 과금** → 안 쓸 땐 Release.

### 2-3. 보안 그룹 인바운드 — 22만, My IP

EC2 → 인스턴스 → Security → 보안 그룹 → **Edit inbound rules**

| 타입 | 포트 | 소스 | 비고 |
|---|---|---|---|
| SSH | 22 | **My IP** | 이것만 있으면 됨 |
| ~~Custom TCP~~ | ~~8080~~ | ~~My IP~~ | **불필요** — 아래 터널 방식 권장 |

> ⚠️ **절대 `0.0.0.0/0` 금지.** 대시보드에는 인증이 없고 봇 멈춤/재개·전략교체 버튼이 있음.
> 공개되면 아무나 내 봇을 조작함. 트레이더·수집기는 리스닝 포트가 없어 규칙 자체가 불필요.

### 2-4. 대시보드 접근 — SSH 터널 (권장)

8080을 인터넷에 아예 노출하지 않고, SSH로 포워딩해서 로컬 브라우저로 봄:

```bash
ssh -i ~/Downloads/mykey.pem -L 8080:localhost:8080 ec2-user@<EIP>
# 터널 유지한 채 브라우저: http://localhost:8080
```

- **장점:** 공격면이 22 하나로 줄고, **집 IP가 바뀌어도 보안그룹 규칙을 8080까지 두 번 고칠 필요 없음.**
- **대안(간편):** 8080에 My IP 규칙을 추가해 `http://<EIP>:8080` 직접 접속.
  카페·모바일 등 IP가 자주 바뀌면 그때마다 규칙 수정이 필요해 오히려 번거로움.
- 참고: `docker-compose.yml`이 대시보드를 호스트 8080에 바인드하므로 터널만으로 동작함.

### 2-5. 아웃바운드 — 기본값 유지

기본 보안 그룹은 아웃바운드 전체 허용. 봇이 필요로 하는 건 전부 아웃바운드다:
**바이낸스 REST(api.binance.com / fapi.binance.com, 443)** · **ghcr.io 이미지 pull** ·
**github.com git fetch** · (선택) 웹훅 알림. 아웃바운드를 굳이 좁히면 배포·시세수집이 깨짐.

---

## 단계 3 — SSH 키: 내 접속용 + 배포용(GitHub Actions)   `로컬+EC2 · 10분`

- [ ] EC2 시작 시 키페어(`.pem`) 받아 내 PC에서 접속 확인
- [ ] 배포 전용 키페어 생성 → 공개키를 EC2에 등록

**방법**
```bash
# 내 접속 확인 (AL2023 기본 유저 = ec2-user. Ubuntu면 ubuntu)
chmod 400 ~/Downloads/mykey.pem
ssh -i ~/Downloads/mykey.pem ec2-user@<EIP>

# 배포 전용 키 생성(암호 없이) — GitHub Actions가 쓸 별도 키
ssh-keygen -t ed25519 -f ~/.ssh/deploy_key -N ""

# 공개키를 EC2에 등록
ssh -i ~/Downloads/mykey.pem ec2-user@<EIP> \
  "echo '$(cat ~/.ssh/deploy_key.pub)' >> ~/.ssh/authorized_keys"
```
> `~/.ssh/deploy_key`(개인키)는 단계 5의 `VPS_SSH_KEY` 시크릿에 통째로 넣음.

---

## 단계 4 — EC2 초기 셋업: Docker · git · 스왑 2GB · 레포   `EC2 · 10분`

- [ ] Docker · compose · git 설치
- [ ] 스왑 2GB 설정 **(저사양 권장)**
- [ ] 레포 clone

**방법** *(Amazon Linux 2023 기준)*
```bash
# Docker/git
sudo dnf install -y docker git
sudo systemctl enable --now docker   # ★ AL2023은 설치해도 자동기동 안 함(Ubuntu와 차이)
sudo usermod -aG docker $USER        # 재로그인 후 sudo 없이 docker

# ★ compose v2 플러그인 — AL2023 리포에 패키지가 없어 수동 설치 (안 하면 `docker compose` 미작동)
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-aarch64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose   # t4g=ARM → aarch64. t3(x86)면 x86_64
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version               # v2.x 나오면 성공

# 스왑 2GB (1GB RAM 순간 피크 OOM 방지)
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h                            # Swap: 2.0Gi 확인

# 레포 clone (compose·프리셋·.env 용). 배포 경로 = VPS_PATH
git clone https://github.com/choozx/coin-coin_v2.git ~/auto_trading
cd ~/auto_trading
```
> EC2는 이미지를 **ghcr에서 pull** 하지만 `docker-compose.yml`·`presets/`·`.env` 는 이 clone에서 씀.
> Ubuntu를 쓴다면 위 Docker 블록만 `sudo apt-get install -y docker.io docker-compose-plugin git`
> 한 줄로 대체(자동기동 O, compose 수동설치 불필요). 스왑·clone은 동일.

---

## 단계 5 — 배포 폴러 설치 (풀 방식)   `EC2 · 5분`

- [ ] `deploy/install-poll-timer.sh` 실행 → systemd 타이머 등록

**왜 SSH 푸시가 아니라 풀인가**
CI가 EC2에 SSH로 들어와 배포하려면 **Actions 러너 IP를 보안그룹에 허용**해야 하는데,
그 대역이 **7000개 이상**(`api.github.com/meta`)이라 규칙 60개 한도로는 불가능하다.
22번을 `0.0.0.0/0`으로 여는 건 (곧 실거래 키가 들어올 서버라) 논외.
→ **서버가 먼저 물어보게** 한다. 부수효과로 GitHub에 서버 접속키를 안 맡겨도 된다.

**방법** (EC2에서)
```bash
cd ~/auto_trading
git fetch origin prod && git checkout -f prod    # 최초 1회 (clone은 main 기준)
./deploy/install-poll-timer.sh
systemctl list-timers deploy-poll                # NEXT/LAST 확인
```

- 2분마다 `git fetch origin prod` → **새 커밋 없으면 즉시 종료**(수 KB, 비용 사실상 0)
- 새 커밋이면 그 커밋의 **40자 SHA 태그 이미지**로 `.env`의 `IMAGE`를 고정하고 pull → `up -d`
- 이미지가 아직 빌드 중이면 조용히 다음 주기 재시도(**기존 컨테이너는 안 멈춤**)
- 로그: `journalctl -u deploy-poll -f` / 즉시 배포: `sudo systemctl start deploy-poll`

**매매 봇을 함부로 재시작하지 않는 두 장치**

| 장치 | 동작 |
|---|---|
| **영향받는 서비스만 교체** | 대시보드만 고친 커밋이면 `dashboard`만 재생성. 트레이더·수집기는 안 멈춤. 판단 근거는 `deploy/service-deps.conf`(import 폐포) — `tests/test_deploy_paths.py`가 실제 import 그래프와 대조해 드리프트를 막는다 |
| **포지션 가드** | 트레이더 교체가 필요해도 **포지션 보유 중이면 연기**. 무포지션이 되면 다음 주기에 자동 반영. 급하면 `sudo FORCE_TRADER=1 systemctl start deploy-poll` |

> 문서·테스트·CI만 바뀐 커밋은 **아무 컨테이너도 건드리지 않는다**(이미지 내용이 동일).
> 연기된 배포는 `.deploy-applied`에 기록되지 않아 매 주기 재시도된다 — 잊히지 않는다.

> ✅ **GitHub Secrets는 하나도 필요 없다.** `VPS_HOST`/`VPS_USER`/`VPS_SSH_KEY`/`VPS_PATH` 를
> 등록했다면 **삭제할 것**. EC2 `~/.ssh/authorized_keys` 의 배포용 공개키 줄도 지워도 된다.
> `GITHUB_TOKEN` 은 Actions가 ghcr push에 자동 사용(등록 불필요).

> **ghcr 접근:** 레포가 public이면 이미지 패키지도 public이라 EC2가 인증 없이 pull 된다.
> 레포를 private로 바꾸면 EC2에서 `read:packages` PAT로 `docker login ghcr.io` 를 1회 해둘 것.

---

## 단계 6 — EC2 `.env` 작성   `EC2 · 3분`

- [ ] `~/auto_trading/.env` 생성

**방법**
```bash
cd ~/auto_trading
cat > .env <<'ENV'
IMAGE=ghcr.io/choozx/coin-coin_v2:latest    # 배포 시 커밋 SHA로 자동 고정
PRESET=presets/examples/live-strategy.json  # 단계 0에서 커밋한 프리셋
INTERVAL=60
EQUITY=10000
COLLECT_SYMBOLS=BTCUSDC
NOTIFY_WEBHOOK=                              # (선택) Discord/Slack 웹훅
ENV
```

---

## 단계 7 — 첫 배포 실행 + 검증   `로컬+웹 · 10분`

- [ ] `prod` 브랜치로 push → 자동배포 트리거
- [ ] EC2에서 컨테이너 3개 확인
- [ ] 브라우저로 대시보드 접속

**방법**
```bash
# 로컬에서 배포 트리거
git push origin main:prod
# → GitHub Actions: 빌드 → ghcr push (3~20분. TA-Lib 재컴파일 없으면 짧음)
# → EC2의 deploy-poll 타이머가 2분 내에 알아서 가져감 (기다리기 싫으면 아래 systemctl start)

# EC2에서 확인 (동시에 대시보드 터널도 열어둠 — 단계 2-4)
ssh -i ~/.ssh/mykey.pem -L 8080:localhost:8080 ec2-user@<EIP>
cd ~/auto_trading
sudo systemctl start deploy-poll   # 2분 안 기다리고 즉시 배포하고 싶을 때
journalctl -u deploy-poll -n 20    # "배포: ... → ghcr.io/...:<sha>" 확인
docker compose ps               # trader/collector/dashboard Up
docker compose logs -f trader   # 워밍업·폴링 로그
docker stats --no-stream        # mem_limit 내 사용량

# 브라우저 (위 SSH 세션을 열어둔 채로)
http://localhost:8080           # 대시보드. 안 열리면 터널·`docker compose ps` 확인
```
> ✅ 봇은 **기본 '멈춤'** 으로 뜬다(커밋 `5a5c21b`). 대시보드에서 **재개**를 눌러야 새 진입 시작 —
> 로그에 신호가 안 보인다고 고장난 게 아님.

---

## 단계 8 — 실거래 전환   `테스트넷부터`

- [x] 리스크 가드레일 — 일일 손실 한도 · kill switch · 연속손실 차단
- [x] `LiveExecutor`(ccxt) 실구현 — 주문/체결/잔고·포지션 동기화 (`engine/binance_broker.py`)
- [ ] **8-1. 테스트넷(가짜돈)으로 며칠** — EC2 `.env` 에 `BINANCE_TESTNET=1` + 테스트넷 키,
      `TRADE_MODE=--live` → `docker compose up -d trader`. 확인할 것:
  - 진입/청산 알림이 오는가, 대시보드에 **'실거래(테스트넷)'** 로 뜨는가
  - `docker compose logs trader | grep 실거래` 로 preflight 통과(격리마진·원웨이) 확인
  - 백테스트 가정 vs 실제: **슬리피지 · maker 체결 비율 · 펀딩** 을 원장에서 비교
  - 컨테이너를 일부러 재시작해 **포지션 인계**가 되는지 (`data/live_position.json`)
- [ ] **8-2. 실돈 전환** — `.env` 에 `BINANCE_TESTNET=0` **그리고** `TRADE_MODE="--live --real-money"`.
      둘 중 하나만 바꾸면 봇이 기동을 거부한다(이중 잠금 — 의도적).
  - 바이낸스 API 키: **출금 비활성 + IP 화이트리스트**(단계 2-2의 **탄력적 IP**) 필수
    → EIP를 안 붙였으면 인스턴스 재시작마다 IP가 바뀌어 **화이트리스트가 깨지고 주문이 막힘**
  - 키는 **레포·compose에 절대 X** → EC2 `.env`(gitignore)/시크릿 매니저
  - 처음엔 **소액 + 낮은 레버리지**로. 최소 주문(BTC 0.001 ≈ 65 USDC)보다 사이징이 작으면
    진입이 계속 건너뛰어진다 — 로그에 `최소 명목가 미달` 이 반복되면 그 뜻
- [ ] 자동배포 안전장치: 나쁜 push가 실매매 봇 갈아치우지 않게 GitHub Environment 승인 규칙/테스트 게이트

---

**정리:** 코드/git(단계 0)은 완료. 남은 1~7은 AWS·GitHub·EC2 접근이 필요해 직접 진행.
막히면 그 단계 로그를 붙여주면 함께 디버깅.
