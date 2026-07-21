#!/usr/bin/env bash
# 풀 방식 배포 폴러를 systemd 타이머로 설치 (EC2에서 1회 실행).
#   cron 대신 systemd인 이유: 실행 로그가 journal에 남아 "왜 배포가 안 됐지"를 추적할 수 있다.
#     확인:  systemctl list-timers deploy-poll
#     로그:  journalctl -u deploy-poll -n 50
#     즉시 배포: sudo systemctl start deploy-poll
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/auto_trading}"
USER_NAME="${USER_NAME:-$(id -un)}"

sudo tee /etc/systemd/system/deploy-poll.service >/dev/null <<UNIT
[Unit]
Description=prod 브랜치 폴링 → 새 커밋이면 컨테이너 교체 (풀 방식 배포)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=${USER_NAME}
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/deploy/pull-deploy.sh
UNIT

sudo tee /etc/systemd/system/deploy-poll.timer >/dev/null <<'UNIT'
[Unit]
Description=배포 폴링 2분 주기

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
AccuracySec=30s

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now deploy-poll.timer
systemctl list-timers deploy-poll --no-pager
