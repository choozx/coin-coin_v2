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


def observe(weight: int, *, scope: str = MAINNET, service: str = None,
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
        _write(service or service_name(), scope, dir_path or DEFAULT_DIR)
    return dict(st)


def snapshot(scope: str = MAINNET) -> dict:
    return dict(_states.get(scope) or {"last": 0, "peak": 0, "peakAt": 0, "at": 0})


def reset() -> None:
    """테스트용 — 모듈 전역을 초기 상태로."""
    _states.clear()
    _last_write.clear()


def _write(service: str, scope: str, dir_path: str) -> None:
    """실패해도 절대 예외를 올리지 않는다 — 관찰이 매매를 멈추면 안 된다."""
    try:
        os.makedirs(dir_path, exist_ok=True)
        path = os.path.join(dir_path, f"{service}-{scope}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"service": service, "scope": scope, "limit": LIMIT_1M,
                       **_states[scope]}, f)
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
                out.append(json.load(f))
        except Exception:
            continue
    return sorted(out, key=lambda r: -int(r.get("peak") or 0))


def by_scope(rows) -> dict:
    """scope -> 그 호스트의 최대 weight. **scope 를 넘어 합치면 안 된다**(별개 카운터)."""
    out = {}
    for r in rows:
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
        return ["관측 없음 — 아직 헤더를 한 번도 못 읽었다"]
    lines = []
    for sc, peak in sorted(peaks.items(), key=lambda kv: -kv[1]):
        pct = peak / LIMIT_1M * 100
        mark = "⚠️ 위험" if peak >= LIMIT_1M * WARN_RATIO else "여유"
        lines.append(f"{sc:8} 최대 {peak}/{LIMIT_1M} weight ({pct:.0f}%) · {mark}")
    return lines
