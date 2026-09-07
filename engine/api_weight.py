"""바이낸스가 알려주는 **IP 누적 weight** 를 기록한다 — 밴의 원인을 다음엔 추측하지 않기 위해.

왜 필요한가: 2026-08~09 에 -1003(IP 밴)을 두 번 맞았고 **두 번 다 원인을 못 밝혔다.**
트레이더의 요청 수만 세고 있었는데(`apiReq`), 밴은 **IP 단위**이고 같은 IP 를 trader ·
collector · dashboard · discordbot 이 함께 쓴다. 게다가 컬렉터는 ccxt 가 아니라 urllib 로
직접 치므로 그 계측에 아예 안 잡혔다. 즉 우리는 계좌를 반만 보고 원인을 추리하고 있었다.

핵심은 세는 방법을 늘리는 게 아니다. **바이낸스가 정답을 응답 헤더로 그냥 준다** —
`X-MBX-USED-WEIGHT-1M` 은 지금 이 IP 가 이번 1분에 쓴 누적 weight 이고, 밴 판정에 쓰이는
바로 그 값이다. 요청 수가 아니라 weight 가 기준이라(klines limit=1500 은 1회에 10)
우리가 세는 '요청 수'로는 애초에 환산이 안 된다. 한 서비스만 이 헤더를 읽어도 **다섯이
합산된 IP 전체 사용량**이 보인다.

★ **scope(호스트)를 반드시 나눈다.** 트레이더 하나가 두 호스트를 친다 —
주문·잔고는 ccxt 로 테스트넷(testnet.binancefuture.com), 캔들은 urllib 로 메인넷
(fapi.binance.com). **둘은 서로 다른 weight 카운터**이고 한도도 각각이다. 처음엔 이걸
한 통에 섞어 담았는데, 그러면 1407 이라는 숫자가 어느 호스트 것인지 알 수 없어 계측을
넣은 의미가 사라진다(컬렉터 20 vs 트레이더 1407 이 모순처럼 보이던 이유가 이것이다).

⚠️ **ccxt 경로의 값은 믿지 않는다(2026-09-07 실측).** ccxt 의 `last_response_headers` 에서
읽은 테스트넷 값이 1282~1806/2400 이라 '위험'을 계속 띄웠는데, 같은 시각 EC2 에서 직접
친 `curl testnet.binancefuture.com/fapi/v1/time` 의 헤더는 **1~2** 였다. 즉 IP 는 놀고
있었고 경보가 가짜였다. urllib 경로(binance_data)는 응답 헤더를 직접 읽으므로 실측과
맞는다(메인넷 10~30). 그래서 기록은 계속하되 **source=ccxt 는 판정에서 뺀다** — 틀린 걸
아는 지표로 경보를 울리면 계측이 없느니만 못하다. 원인 규명은 미해결.

저장은 (서비스, scope) 별 파일로 나눈다(`data/api_weight/<service>-<scope>.json`).
컨테이너가 다른 프로세스라 한 파일에 같이 쓰면 경합이 나는데, 각자 제 파일만 쓰면 경합
자체가 없다. 읽는 쪽(tools/report.py)이 scope 별로 묶어 본다.
"""
from __future__ import annotations

import json
import os
import time

# USDⓈ-M 선물 IP 한도(2026 기준). 넘으면 429 → 계속되면 418(밴).
LIMIT_1M = 2400
WARN_RATIO = 0.5             # 이 비율을 넘으면 '위험'으로 표시한다(여유를 두고 본다)

DEFAULT_DIR = os.environ.get("API_WEIGHT_DIR", "data/api_weight")
WRITE_EVERY_S = 10.0         # 매 요청마다 파일을 쓰면 그게 또 부하다 — 피크 갱신 or 주기적으로만

_HEADER = "x-mbx-used-weight-1m"

MAINNET = "mainnet"
TESTNET = "testnet"

_states = {}                 # scope -> {"last","peak","peakAt","at"}
_endpoints = {}              # scope -> {label: {"n","max","sum"}}  누가 비싼가
_last_write = {}             # scope -> epoch초


def service_name() -> str:
    """이 프로세스가 어느 서비스인가. compose 가 SERVICE_NAME 으로 넣어준다."""
    return os.environ.get("SERVICE_NAME") or "unknown"


def header_weight(headers) -> int:
    """응답 헤더에서 누적 weight 를 뽑는다. 없으면 0.

    헤더 이름의 대소문자는 서버·클라이언트마다 다르다(urllib 은 원본, ccxt 는 제각각) →
    항상 소문자로 맞춰 찾는다. 못 찾으면 0 = '이번엔 모른다'이지 '0 을 썼다'가 아니다.
    """
    if not headers:
        return 0
    try:
        items = headers.items()
    except AttributeError:
        return 0
    for k, v in items:
        if str(k).lower() == _HEADER:
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0
    return 0


HTTP = "http"                # 응답 헤더 직독 — 실측과 일치, 신뢰
CCXT = "ccxt"                # ccxt last_response_headers — 실측과 불일치, 미검증


def observe(weight: int, *, scope: str = MAINNET, service: str = None, source: str = HTTP,
            dir_path: str = None, now: float = None) -> dict:
    """weight 한 건 관측. 0 이하는 '모름'이라 무시한다(피크를 0 으로 덮지 않게).

    scope = 어느 호스트의 카운터인가. 섞으면 숫자가 무의미해진다(모듈 주석 참조).
    """
    st = _states.setdefault(scope, {"last": 0, "peak": 0, "peakAt": 0, "at": 0})
    w = int(weight or 0)
    if w <= 0:
        return dict(st)
    now = time.time() if now is None else now
    st["last"] = w
    st["at"] = int(now * 1000)
    fresh_peak = w > st["peak"]
    if fresh_peak:
        st["peak"] = w
        st["peakAt"] = st["at"]
    if fresh_peak or (now - _last_write.get(scope, 0.0)) >= WRITE_EVERY_S:
        _last_write[scope] = now
        _write(service or service_name(), scope, source, dir_path or DEFAULT_DIR)
    return dict(st)


def charge(label: str, weight: int, *, scope: str = MAINNET, service: str = None,
           source: str = HTTP, dir_path: str = None, now: float = None) -> int:
    """엔드포인트 하나의 **비용**을 귀속시킨다. 반환: 직전 관측 대비 증가분.

    weight 헤더는 1분 누계라 값 하나로는 '누가 썼나'를 못 가른다. 우리 호출 **직전/직후의
    차이**를 그 호출에 귀속시키면 어느 엔드포인트가 비싼지 바로 보인다.

    ⚠️ 같은 IP 의 다른 프로세스가 그 사이에 쓴 것도 이 차이에 섞인다 — 그래서 이건 정밀
    측정이 아니라 **용의자 지목**이다. 한 엔드포인트에만 큰 값이 몰리면 그게 범인이고,
    전부에 고르게 퍼져 있으면 범인은 우리가 아니라 밖에 있다. 그 구별이 목적이다.
    """
    st = _states.get(scope) or {}
    prev, prev_at = int(st.get("last") or 0), int(st.get("at") or 0)
    now = time.time() if now is None else now
    w = int(weight or 0)
    delta = 0
    if w > 0 and label:
        # 분이 바뀌면 카운터가 0 으로 리셋된다 → 그 경계의 음수 차이는 버린다(비용이 아니다).
        same_minute = prev > 0 and w >= prev and (now * 1000 - prev_at) < 60_000
        delta = w - prev if same_minute else 0
        e = _endpoints.setdefault(scope, {}).setdefault(label, {"n": 0, "max": 0, "sum": 0})
        e["n"] += 1
        e["max"] = max(e["max"], delta)
        e["sum"] += delta
    # ★ 귀속을 **먼저** 갱신하고 나서 observe 한다. observe 안에서 파일을 쓰므로 순서가
    #   반대면 기록이 늘 한 박자 뒤처져, 방금 비싼 호출이 파일에 안 들어간다.
    observe(weight, scope=scope, service=service, source=source, dir_path=dir_path, now=now)
    return delta


def endpoints(scope: str = MAINNET) -> dict:
    return {k: dict(v) for k, v in (_endpoints.get(scope) or {}).items()}


def snapshot(scope: str = MAINNET) -> dict:
    return dict(_states.get(scope) or {"last": 0, "peak": 0, "peakAt": 0, "at": 0})


def reset() -> None:
    """테스트용 — 모듈 전역을 초기 상태로."""
    _states.clear()
    _endpoints.clear()
    _last_write.clear()


def _write(service: str, scope: str, source: str, dir_path: str) -> None:
    """실패해도 절대 예외를 올리지 않는다 — 관찰이 매매를 멈추면 안 된다."""
    try:
        os.makedirs(dir_path, exist_ok=True)
        path = os.path.join(dir_path, f"{service}-{scope}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"service": service, "scope": scope, "source": source,
                       "limit": LIMIT_1M,
                       **_states[scope],
                       # 비싼 순 상위 8개만 — 파일이 커지면 매 10초 쓰기가 그 자체로 부하다.
                       "endpoints": dict(sorted((_endpoints.get(scope) or {}).items(),
                                                key=lambda kv: -kv[1]["max"])[:8])}, f)
        os.replace(tmp, path)
    except Exception:
        pass


def read_all(dir_path: str = None) -> list:
    """(서비스, scope) 기록을 모아 peak 내림차순으로. 읽기 실패한 파일은 건너뛴다."""
    dir_path = dir_path or DEFAULT_DIR
    out = []
    try:
        names = sorted(os.listdir(dir_path))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(dir_path, name), encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        # scope 도입 전에 쓰인 파일은 테스트넷·메인넷이 **섞인** 값이라 해석이 불가능하다.
        # mainnet 으로 간주해 보여주면 있지도 않은 '메인넷 75% 위험'이 만들어진다 → 버린다.
        if not rec.get("scope"):
            continue
        out.append(rec)
    return sorted(out, key=lambda r: -int(r.get("peak") or 0))


def by_scope(rows, trusted_only: bool = True) -> dict:
    """scope -> 그 호스트의 최대 weight. **scope 를 넘어 합치면 안 된다**(별개 카운터).

    trusted_only: source=ccxt 는 뺀다(모듈 주석의 2026-09-07 실측 참조).
    """
    out = {}
    for r in rows:
        if trusted_only and r.get("source") == CCXT:
            continue
        sc = r.get("scope") or MAINNET
        out[sc] = max(out.get(sc, 0), int(r.get("peak") or 0))
    return out


def verdict(rows) -> list:
    """scope 별 한 줄 판정.

    같은 scope 안에서는 **서비스별로 더하지 않는다** — 헤더가 이미 그 호스트의 IP 합산이라
    더하면 이중 계산이다. 다른 scope 끼리도 더하지 않는다 — 카운터도 한도도 별개다.
    """
    peaks = by_scope(rows)
    if not peaks:
        untrusted = [r for r in rows if r.get("source") == CCXT]
        if untrusted:
            return ["신뢰 가능한 관측 없음 — ccxt 경로 값은 실측과 어긋나 판정에서 제외한다"]
        return ["관측 없음 — 아직 헤더를 한 번도 못 읽었다"]
    lines = []
    for sc, peak in sorted(peaks.items(), key=lambda kv: -kv[1]):
        pct = peak / LIMIT_1M * 100
        mark = "⚠️ 위험" if peak >= LIMIT_1M * WARN_RATIO else "여유"
        lines.append(f"{sc:8} 최대 {peak}/{LIMIT_1M} weight ({pct:.0f}%) · {mark}")
    return lines
