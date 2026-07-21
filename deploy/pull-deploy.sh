#!/usr/bin/env bash
# 풀(pull) 방식 배포 — EC2가 스스로 prod 브랜치를 폴링해 새 커밋이면 컨테이너를 교체한다.
#
# 왜 푸시가 아니라 풀인가: GitHub Actions 러너의 IP는 7000개 대역이라 보안그룹(60규칙)으로
#   허용할 수 없다. 22번을 전체 공개하지 않으려면 서버가 먼저 물어보는 수밖에 없다.
#   부수효과로 GitHub에 서버 접속키를 안 맡겨도 된다(GitHub이 털려도 서버는 안전).
#
# 동작: git fetch → 새 커밋 없으면 즉시 종료(평소 경로, 수 KB) → 있으면 그 커밋의
#   SHA 태그 이미지로 .env의 IMAGE를 고정하고 pull → up -d.
#
# 설치: deploy/install-poll-timer.sh (systemd 타이머, 2분 주기)
# 로그: journalctl -u deploy-poll -f
set -euo pipefail

# 실행 중 git reset이 이 파일을 덮어써도 안전하도록 전체를 함수로 감싼다
# (bash는 함수 정의를 통째로 파싱한 뒤 실행 → 중간에 파일이 바뀌어도 영향 없음).
main() {
    cd "${REPO_DIR:-$HOME/auto_trading}"

    git fetch -q origin prod
    local remote image running
    remote=$(git rev-parse origin/prod)
    image="${REGISTRY:-ghcr.io/choozx/coin-coin_v2}:${remote}"      # 태그 = 40자 전체 SHA

    # 비교 대상은 git HEAD가 아니라 '실제 실행 중인 이미지'.
    # 이렇게 해야 이미지 빌드가 늦어 pull에 실패했던 커밋이 다음 주기에 자동 재시도된다
    # (git HEAD로 비교하면 reset은 이미 끝나 있어서 영영 재시도를 안 한다).
    running=$(docker compose ps -q 2>/dev/null | head -1 \
              | xargs -r docker inspect -f '{{.Config.Image}}' 2>/dev/null || true)
    [ "$running" = "$image" ] && exit 0                             # 평소 경로: 할 일 없음

    git reset --hard -q "$remote"                                   # compose·프리셋 갱신
    # .env는 gitignore라 reset에도 살아남는다. IMAGE만 이번 커밋으로 고정(불변 배포).
    if grep -q '^IMAGE=' .env; then
        sed -i "s|^IMAGE=.*|IMAGE=${image}|" .env
    else
        echo "IMAGE=${image}" >> .env
    fi

    # 아직 빌드 중이면 여기서 실패 → 조용히 다음 주기로. 기존 컨테이너는 계속 돈다.
    if ! docker compose pull -q 2>/dev/null; then
        echo "이미지 미준비(빌드 중?) ${image} — 다음 주기 재시도"
        exit 0
    fi

    echo "배포: ${running:-없음} → ${image}"
    docker compose up -d --no-build                                 # 바뀐 컨테이너만 교체
    docker image prune -f >/dev/null
    docker compose ps
}

main "$@"
