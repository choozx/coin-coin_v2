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
#   다음 주기에 다시 시도한다. .deploy-pull-fails 는 이미지 pull 연속 실패 횟수(둘 다 서버 로컬).
#
# PULL_FAIL_ALERT (기본 5 = 약 10분): 이미지를 이 횟수만큼 연속으로 못 받으면 디스코드로 알린다.
#   빌드는 보통 몇 분이면 끝나므로 그보다 길게 실패하면 정상 대기가 아니라 고장이다.
#
# 설치: deploy/install-poll-timer.sh (systemd 타이머, 2분 주기)
# 로그: journalctl -u deploy-poll -f
# 즉시: sudo systemctl start deploy-poll
set -euo pipefail

# 실행 중 git reset이 이 파일을 덮어써도 안전하도록 전체를 함수로 감싼다
# (bash는 함수 정의를 통째로 파싱한 뒤 실행 → 중간에 파일이 바뀌어도 영향 없음).
main() {
    cd "${REPO_DIR:-$HOME/auto_trading}"
    local state_file=".deploy-applied" fail_file=".deploy-pull-fails"

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

    # 아직 빌드 중이면 여기서 실패 → 다음 주기로. 기존 컨테이너는 계속 돈다.
    # ★ stderr 를 절대 버리지 않는다. 예전엔 `2>/dev/null` 로 삼키고 "빌드 중?" 이라고만 찍었는데,
    #   실제 사유는 compose 파일 파싱 오류(.env 의 DASH_BIND 오타)였고 그 한 줄이 안 보여서
    #   30일 동안 2분마다 실패하는 걸 아무도 몰랐다. 사유를 로그와 알림 양쪽에 그대로 싣는다.
    local fails pull_err pull_rc=0
    pull_err=$(docker compose pull -q 2>&1) || pull_rc=$?
    if [ "$pull_rc" -ne 0 ]; then
        fails=$(( $(read_count "$fail_file") + 1 ))
        echo "$fails" > "$fail_file"
        echo "이미지 pull 실패 ${image} — 다음 주기 재시도 (연속 ${fails}회)"
        echo "  사유: ${pull_err}"
        # -eq 로 임계값에 '도달한 그 주기'에만 발송 → 2분마다 같은 경고가 쌓이지 않는다.
        if [ "$fails" -eq "${PULL_FAIL_ALERT:-5}" ]; then
            # dnotify 는 큰따옴표·개행이 들어가면 JSON 이 깨진다 → 한 줄로 눌러 앞부분만 싣는다.
            local why; why=$(echo "$pull_err" | tr -d '"\\' | tr '\n' ' ' | cut -c1-160)
            dnotify "⚠️ 배포 정체 — ${remote:0:7} 를 ${fails}회(약 $((fails * 2))분) 못 받았습니다. 사유: ${why}"
        fi
        exit 0
    fi
    # 성공 — 정체를 알린 뒤였다면 회복도 알린다(워치독과 같은 '상태 전이에만 알림' 규칙).
    fails=$(read_count "$fail_file")
    if [ "$fails" -ge "${PULL_FAIL_ALERT:-5}" ]; then
        dnotify "✅ 이미지 수신 재개 — ${remote:0:7} pull 성공(${fails}회 실패 후). 배포를 계속합니다."
    fi
    rm -f "$fail_file"

    # ② 포지션 가드 — 돈이 걸려 있으면 트레이더만 빼고 배포한다.
    if [[ " $targets " == *" trader "* ]] && [ "${FORCE_TRADER:-0}" != "1" ] && has_open_position; then
        targets=$(echo "$targets" | tr ' ' '\n' | grep -vx trader | tr '\n' ' ')
        deferred=1
        echo "⏸ 트레이더 포지션 보유 중 — 교체 연기(무포지션 되면 자동 반영). 강제: FORCE_TRADER=1"
    fi

    if [ -n "${targets// /}" ]; then
        local from="${applied:0:7}"; [ -n "$from" ] || from="최초"   # 첫 배포면 앞이 비어 '배포 →abc' 로 보인다
        dnotify "🚀 배포 ${from}→${remote:0:7} 시작 · 대상: ${targets}${deferred:+ (트레이더 연기)}"
        echo "배포 → ${remote:0:7} | 대상: ${targets}"
        save_logs "$remote" "$targets"      # ★ 교체 전에 로그를 건진다(아래 주석 참조)
        # ★ 한 줄로 전부 올리지 않는다. compose 파일이 바뀌면 대상이 전 서비스가 되는데,
        #   그러면 trader·collector 가 **동시에** 부트스트랩하며 같은 IP 에서 캔들을 긁는다.
        #   2026-09-02 재시작 직후 -1003(IP 밴)이 났고, 그 시각 폴링 요청은 정상이었다(peak 7).
        #   순차 기동은 배포를 조금 늦출 뿐이고 밴은 봇을 통째로 멈춘다 — 교환비가 명백하다.
        local svc first=1
        for svc in $targets; do
            [ "$first" = 1 ] || sleep "${DEPLOY_STAGGER_SEC:-15}"
            first=0
            docker compose up -d --no-build "$svc"
        done
        docker image prune -f >/dev/null
    fi

    if [ -n "$deferred" ]; then
        echo "트레이더 미반영 → .deploy-applied 갱신 안 함(다음 주기 재시도)"
        dnotify "⏸ 트레이더 교체 연기(포지션 보유) → ${remote:0:7}. 무포지션 되면 자동 반영. 반영됨: ${targets:-없음}"
    else
        echo "$remote" > "$state_file"
        echo "배포 완료 ${remote:0:7}"
        dnotify "✅ 배포 완료 → ${remote:0:7} · 대상: ${targets:-없음}"
    fi
    docker compose ps --format 'table {{.Service}}\t{{.Status}}'
}

# service-deps.conf 의 <svc>_mods 키에서 서비스 이름을 뽑는다(하드코딩 대신 동적 → 새 서비스
# 추가 시 여기 안 고쳐도 자동 포함. 테스트 test_deploy_paths 가 conf↔import 일치를 지킨다).
all_services() {
    sed -n 's/^\([a-z][a-z0-9]*\)_mods=.*/\1/p' deploy/service-deps.conf | tr '\n' ' ' | sed 's/ *$//'
}

# 바뀐 경로 목록을 받아 재생성이 필요한 서비스 이름을 공백 구분으로 출력.
affected_services() {
    local changed="$1" conf="deploy/service-deps.conf" out=""
    [ "$changed" = "__ALL__" ] && { all_services; return; }
    [ -z "$changed" ] && { echo ""; return; }

    local all_extra svc mods extra pattern
    all_extra=$(conf_get "$conf" all_extra)
    for svc in $(all_services); do
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

# 교체 직전 로그를 파일로 건진다.
#
# 왜: `docker compose up -d` 는 컨테이너를 **재생성**하고, 그러면 그 컨테이너의 json-file
#   로그가 통째로 사라진다. 즉 배포가 사고의 증거를 지운다. 실제로 2026-09-02 사고에서
#   가드레일 발동 로그를 찾을 수 없었다 — 재시작 이전이 전부 없어졌기 때문이다.
#   journalctl(deploy-poll)은 살아남지만 그건 배포 로그일 뿐 봇 로그가 아니다.
save_logs() {
    local commit="$1" targets="$2" dir="logs/predeploy" f
    mkdir -p "$dir" 2>/dev/null || return 0
    f="${dir}/$(date -u +%Y%m%dT%H%M%SZ)-${commit:0:7}.log"
    # shellcheck disable=SC2086
    docker compose logs --no-color --timestamps --tail 3000 $targets > "$f" 2>&1 || true
    # 최근 20개만 남긴다(프리티어 디스크). 삭제 실패가 배포를 막지 않게 전부 || true.
    ls -1t "$dir" 2>/dev/null | tail -n +21 | while read -r old; do rm -f "${dir}/${old}" || true; done
    echo "  교체 전 로그 보관: ${f}"
}

# 카운터 파일 읽기 → 항상 숫자. 파일이 없거나 비었거나 깨졌으면 0.
# ★ set -e 아래에선 `[ "$x" -eq 1 ]` 에 숫자 아닌 값이 오면 스크립트가 통째로 죽는다.
read_count() {
    local n
    n=$(cat "$1" 2>/dev/null || true)
    if [[ "$n" =~ ^[0-9]+$ ]]; then echo "$n"; else echo 0; fi
}

conf_get() {                                    # conf 파일에서 key= 값 읽기
    sed -n "s/^$2=//p" "$1" | head -1
}

# 배포 상황을 디스코드로 알린다. .env 에서 값만 뽑아 curl(호스트에 파이썬/의존성 불필요).
# 알림 실패가 배포를 막으면 안 되므로 전부 '|| true'. 메시지엔 큰따옴표를 넣지 말 것(JSON 깨짐).
dnotify() {
    local msg="$1" hook token channel payload
    hook=$(sed -n 's/^NOTIFY_WEBHOOK=//p' .env 2>/dev/null | head -1)
    token=$(sed -n 's/^DISCORD_BOT_TOKEN=//p' .env 2>/dev/null | head -1)
    # 배포는 시스템 카테고리 → 시스템 채널(있으면), 없으면 기본 채널로 폴백.
    channel=$(sed -n 's/^DISCORD_CHANNEL_SYSTEM=//p' .env 2>/dev/null | head -1)
    [ -z "$channel" ] && channel=$(sed -n 's/^DISCORD_CHANNEL_ID=//p' .env 2>/dev/null | head -1)
    payload=$(printf '{"content":"%s"}' "$msg")
    if [ -n "$token" ] && [ -n "$channel" ]; then
        curl -sf -m 5 -X POST "https://discord.com/api/v10/channels/${channel}/messages" \
            -H "Authorization: Bot ${token}" -H "Content-Type: application/json" \
            -H "User-Agent: coin-coin-bot/1.0" -d "$payload" >/dev/null 2>&1 || true
    elif [ -n "$hook" ]; then
        curl -sf -m 5 -X POST "$hook" -H "Content-Type: application/json" \
            -H "User-Agent: coin-coin-bot/1.0" -d "$payload" >/dev/null 2>&1 || true
    fi
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
