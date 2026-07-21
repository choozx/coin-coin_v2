#!/usr/bin/env bash
# 풀(pull) 방식 배포 — EC2가 스스로 prod 브랜치를 폴링해 새 커밋이면 컨테이너를 교체한다.
#
# 왜 푸시가 아니라 풀인가: GitHub Actions 러너의 IP는 7000개 대역이라 보안그룹(60규칙)으로
#   허용할 수 없다. 22번을 전체 공개하지 않으려면 서버가 먼저 물어보는 수밖에 없다.
#   부수효과로 GitHub에 서버 접속키를 안 맡겨도 된다(GitHub이 털려도 서버는 안전).
#
# 두 가지 안전장치:
#   ① 영향받는 서비스만 교체 — 대시보드만 고쳤으면 매매 봇·수집기는 안 건드린다.
#      판단 근거는 deploy/service-deps.conf (import 폐포 기반, 테스트로 드리프트 감시).
#   ② 포지션 보유 중이면 트레이더 교체를 미룬다 — 청산되고 무포지션이 될 때까지 기다렸다가
#      다음 주기에 교체. 돈이 걸린 상태에서 프로세스를 죽이지 않는 게 우선.
#      급하면: FORCE_TRADER=1 sudo -E systemctl start deploy-poll  (또는 아래 수동 명령)
#
# 상태: .deploy-applied 에 '완전히 반영된 커밋'을 기록. 트레이더가 미뤄지면 기록하지 않아
#   다음 주기에 다시 시도한다.
#
# 설치: deploy/install-poll-timer.sh (systemd 타이머, 2분 주기)
# 로그: journalctl -u deploy-poll -f
# 즉시: sudo systemctl start deploy-poll
set -euo pipefail

# 실행 중 git reset이 이 파일을 덮어써도 안전하도록 전체를 함수로 감싼다
# (bash는 함수 정의를 통째로 파싱한 뒤 실행 → 중간에 파일이 바뀌어도 영향 없음).
main() {
    cd "${REPO_DIR:-$HOME/auto_trading}"
    local state_file=".deploy-applied"

    git fetch -q origin prod
    local remote applied image
    remote=$(git rev-parse origin/prod)
    applied=$(cat "$state_file" 2>/dev/null || true)
    [ "$applied" = "$remote" ] && exit 0                # 평소 경로: 새 커밋 없음 → 즉시 종료

    local changed
    if [ -n "$applied" ] && git cat-file -e "$applied^{commit}" 2>/dev/null; then
        changed=$(git diff --name-only "$applied" "$remote")
    else
        changed="__ALL__"                               # 최초 실행/이력 유실 → 전부 대상
    fi

    git reset --hard -q "$remote"                       # compose·프리셋·이 스크립트 갱신
    image="${REGISTRY:-ghcr.io/choozx/coin-coin_v2}:${remote}"   # 태그 = 40자 전체 SHA

    local targets deferred=""
    targets=$(affected_services "$changed")

    if [ -z "$targets" ]; then
        # 문서·테스트·CI 설정만 바뀐 경우. 이미지 내용도 컨테이너 설정도 동일 → 건드릴 것 없음.
        echo "컨테이너에 영향 없는 변경 — 재시작 없음 (${applied:0:7}→${remote:0:7})"
        echo "$remote" > "$state_file"
        exit 0
    fi

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

    # ② 포지션 가드 — 돈이 걸려 있으면 트레이더만 빼고 배포한다.
    if [[ " $targets " == *" trader "* ]] && [ "${FORCE_TRADER:-0}" != "1" ] && has_open_position; then
        targets=$(echo "$targets" | tr ' ' '\n' | grep -vx trader | tr '\n' ' ')
        deferred=1
        echo "⏸ 트레이더 포지션 보유 중 — 교체 연기(무포지션 되면 자동 반영). 강제: FORCE_TRADER=1"
    fi

    if [ -n "${targets// /}" ]; then
        echo "배포 → ${remote:0:7} | 대상: ${targets}"
        # shellcheck disable=SC2086
        docker compose up -d --no-build $targets
        docker image prune -f >/dev/null
    fi

    if [ -n "$deferred" ]; then
        echo "트레이더 미반영 → .deploy-applied 갱신 안 함(다음 주기 재시도)"
    else
        echo "$remote" > "$state_file"
        echo "배포 완료 ${remote:0:7}"
    fi
    docker compose ps --format 'table {{.Service}}\t{{.Status}}'
}

# 바뀐 경로 목록을 받아 재생성이 필요한 서비스 이름을 공백 구분으로 출력.
affected_services() {
    local changed="$1" conf="deploy/service-deps.conf" out=""
    [ "$changed" = "__ALL__" ] && { echo "trader collector dashboard"; return; }
    [ -z "$changed" ] && { echo ""; return; }

    local all_extra svc mods extra pattern
    all_extra=$(conf_get "$conf" all_extra)
    for svc in trader collector dashboard; do
        mods=$(conf_get "$conf" "${svc}_mods")
        extra=$(conf_get "$conf" "${svc}_extra")
        # 모듈 목록 → ^engine/(a|b|c)\.py$
        pattern="^engine/($(echo "$mods" | tr ' ' '|'))\.py$"
        [ -n "$extra" ] && pattern="${pattern}|${extra}"
        [ -n "$all_extra" ] && pattern="${pattern}|${all_extra}"
        if grep -qE "$pattern" <<<"$changed"; then
            out="${out}${svc} "
        fi
    done
    echo "${out% }"
}

conf_get() {                                    # conf 파일에서 key= 값 읽기
    sed -n "s/^$2=//p" "$1" | head -1
}

# 트레이더가 포지션을 들고 있는가. state.json은 트레이더가 매 루프(기본 60초) 갱신한다.
#   - 파일 없음/파싱 실패  → 없음으로 간주(막 띄웠거나 아직 상태를 안 씀)
#   - 트레이더가 안 돌고 있음 → 지킬 대상이 없으므로 없음으로 간주
#     (크래시로 state.json에 포지션이 박제된 채 영영 배포가 막히는 걸 방지)
has_open_position() {
    docker compose ps -q trader 2>/dev/null | grep -q . || return 1
    python3 - <<'PY'
import json, sys, pathlib
p = pathlib.Path("data/state.json")
try:
    sys.exit(0 if json.loads(p.read_text()).get("position") else 1)
except Exception:
    sys.exit(1)
PY
}

main "$@"
