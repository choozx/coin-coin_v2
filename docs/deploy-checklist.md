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
- [ ] 인스턴스: **t4g.small** (2GB·ARM Graviton) · Ubuntu 22.04

**방법**
- **리전**(우상단): 페이퍼는 지연 무관 → 싼 `us-east-1`도 OK. 실거래 전환 시 `ap-northeast-1`(도쿄)
- **AMI**: Ubuntu Server 22.04 LTS — **Arm 아키텍처(64-bit ARM)** 선택 (t4g는 ARM!)
- **유형**: **`t4g.small`** (2vCPU·2GB·ARM, 도쿄 ~$16/mo) — 가성비 선택
  - x86 무수정 원하면 `t3.small`(2GB, ~$20) / 최저가는 `t3.micro`(1GB, ~$10)+스왑
- **스토리지**: 8GB → **20GB** (gp3, ~$2/mo)

> ⚠️ **아키텍처 주의:** t4g = **ARM64**. `deploy.yml`이 `linux/arm64`로 빌드하도록 이미 세팅됨.
> 나중에 t3(x86)로 바꾸면 `deploy.yml`의 `platforms`를 `linux/amd64`로 되돌릴 것.
> 요금: t4g.small ~$16 · t3.small ~$20 · t3.micro ~$10 /mo (도쿄, EBS 별도).

---

## 단계 2 — 보안 그룹(방화벽): 내 IP만 열기   `AWS · 5분`

- [ ] 인바운드: SSH(22)·대시보드(8080) 모두 **My IP** 로 제한

**방법** — EC2 → 인스턴스 → Security → 보안 그룹 → **Edit inbound rules**
- SSH · TCP **22** · Source **My IP**
- Custom TCP · **8080** · Source **My IP** (대시보드)

> ⚠️ **절대 `0.0.0.0/0` 금지.** 대시보드에 봇 멈춤/재개 버튼이 있어 공개되면 아무나 제어 가능.
> 집 IP가 바뀌면 규칙만 갱신하거나 SSH 터널 사용. 트레이더·수집기는 포트가 없어 규칙 불필요.

---

## 단계 3 — SSH 키: 내 접속용 + 배포용(GitHub Actions)   `로컬+EC2 · 10분`

- [ ] EC2 시작 시 키페어(`.pem`) 받아 내 PC에서 접속 확인
- [ ] 배포 전용 키페어 생성 → 공개키를 EC2에 등록

**방법**
```bash
# 내 접속 확인
chmod 400 ~/Downloads/mykey.pem
ssh -i ~/Downloads/mykey.pem ubuntu@<EC2_PUBLIC_IP>

# 배포 전용 키 생성(암호 없이) — GitHub Actions가 쓸 별도 키
ssh-keygen -t ed25519 -f ~/.ssh/deploy_key -N ""

# 공개키를 EC2에 등록
ssh -i ~/Downloads/mykey.pem ubuntu@<EC2_IP> \
  "echo '$(cat ~/.ssh/deploy_key.pub)' >> ~/.ssh/authorized_keys"
```
> `~/.ssh/deploy_key`(개인키)는 단계 5의 `VPS_SSH_KEY` 시크릿에 통째로 넣음.

---

## 단계 4 — EC2 초기 셋업: Docker · git · 스왑 2GB · 레포   `EC2 · 10분`

- [ ] Docker · compose · git 설치
- [ ] 스왑 2GB 설정 **(저사양 권장)**
- [ ] 레포 clone

**방법**
```bash
# Docker/git
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER      # 재로그인 후 sudo 없이 docker

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

---

## 단계 5 — GitHub 준비: Secrets · ghcr 패키지   `GitHub · 10분`

- [ ] Repo Secrets 등록 (Settings → Secrets and variables → Actions)
- [ ] ghcr 패키지 접근 결정: public 전환 **또는** GHCR_PAT

**Secrets 목록**
| 이름 | 값 |
|---|---|
| `VPS_HOST` | EC2 퍼블릭 IP |
| `VPS_USER` | `ubuntu` |
| `VPS_SSH_KEY` | `~/.ssh/deploy_key` **개인키 전체** (-----BEGIN…END-----) |
| `VPS_PATH` | `/home/ubuntu/auto_trading` |
| `VPS_PORT` | (선택) 기본 22 |
| `GHCR_PAT` | (이미지 private일 때만) read:packages PAT |

> ✅ `GITHUB_TOKEN` 은 등록 불필요 — Actions가 ghcr push에 자동 사용.

**ghcr 접근** (첫 배포 후 이미지 패키지 생김, 기본 private)
- **간단:** GitHub → Packages → 해당 패키지 → Settings → **Change visibility → Public**
  (코드가 아니라 빌드된 이미지만 공개) → EC2가 인증 없이 pull
- **private 유지:** Developer settings → Personal access token(classic, `read:packages`) 발급
  → `GHCR_PAT` 시크릿에 → deploy가 EC2에서 자동 `docker login ghcr.io`

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
# → GitHub Actions 탭: ① 빌드→ghcr push  ② EC2 SSH pull·재기동

# EC2에서 확인
ssh -i ~/Downloads/mykey.pem ubuntu@<EC2_IP>
cd ~/auto_trading
docker compose ps               # trader/collector/dashboard Up
docker compose logs -f trader   # 워밍업·폴링 로그
docker stats --no-stream        # mem_limit 내 사용량

# 브라우저
http://<EC2_IP>:8080            # 대시보드 (안 열리면 보안그룹 8080·내 IP 확인)
```

---

## 단계 8 — (나중) 실거래(실돈) 전환   `향후`

- [ ] 페이퍼로 며칠 실전 검증 (백테스트 가정 vs 실제 체결)
- [ ] 리스크 가드레일 — 일일 손실 한도 · kill switch · 연속손실 차단
- [ ] `LiveExecutor`(ccxt) 실구현 — 주문/체결/잔고 동기화
  - 바이낸스 API 키: **출금 비활성 + IP 화이트리스트**(EC2 IP) 필수
  - 키는 **레포·compose에 절대 X** → EC2 `.env`(gitignore)/시크릿 매니저
  - 재시작 시 포지션/잔고는 **거래소에서 읽어 동기화**(로컬 상태 맹신 금지)
  - 자동배포 안전장치: 나쁜 push가 실매매 봇 갈아치우지 않게 GitHub Environment 승인 규칙/테스트 게이트

---

**정리:** 코드/git(단계 0)은 완료. 남은 1~7은 AWS·GitHub·EC2 접근이 필요해 직접 진행.
막히면 그 단계 로그를 붙여주면 함께 디버깅.
