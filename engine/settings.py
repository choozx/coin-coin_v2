"""글로벌 설정 (data/settings.json) — 백테스트·라이브 봇이 공유하는 값.

현재: 잔고별 레버리지 티어(동적 레버리지). 한 번 정의하면 백테스트/라이브가 동일하게 쓴다.
앞으로 다른 글로벌 값이 생기면 여기에 추가.
"""
from __future__ import annotations

import json
import os

SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "data/settings.json")

# 티어 = [{maxBalance, leverage}...] 오름차순. maxBalance null = 그 이상(최상단).
DEFAULT_LEVERAGE_TIERS = [
    {"maxBalance": 1000.0, "leverage": 20},
    {"maxBalance": 5000.0, "leverage": 10},
    {"maxBalance": None, "leverage": 5},
]


def _read(path: str = SETTINGS_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write(d: dict, path: str = SETTINGS_PATH) -> dict:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, path)
    return d


def get_leverage_tiers(path: str = SETTINGS_PATH) -> list:
    """글로벌 레버리지 티어. 없으면 기본값 사본."""
    v = _read(path).get("leverageTiers")
    if isinstance(v, list) and v:
        return v
    return [dict(t) for t in DEFAULT_LEVERAGE_TIERS]


def set_leverage_tiers(tiers, path: str = SETTINGS_PATH) -> dict:
    """글로벌 레버리지 티어 저장 (백테스트·라이브 공통)."""
    clean = []
    for t in (tiers or []):
        try:
            mb = t.get("maxBalance")
            clean.append({"maxBalance": (None if mb in (None, "") else float(mb)),
                          "leverage": int(t["leverage"])})
        except Exception:
            continue
    d = _read(path)
    d["leverageTiers"] = clean
    return _write(d, path)


# 리스크 가드레일 — 계좌 안전장치(전략 무관). 라이브 봇이 강제(새 진입만 차단, 청산은 계속).
DEFAULT_GUARDRAILS = {
    "dailyLossLimit": {"enabled": False, "pct": 10.0},      # 오늘 실현손실이 잔고의 pct% 넘으면 정지
    # N연속 손절 시 정지. cooldownHours 뒤 자동 해제(0이면 수동 해제 전까지 유지).
    # ★ 쿨다운이 없으면 이 가드레일은 자기 해제가 불가능한 래치가 된다 — 진입이 막히면 새 트레이드가
    #   안 생겨 연속 기록이 영원히 그대로다. 그래서 기본값은 자동 해제 쪽이다.
    "maxConsecutiveLosses": {"enabled": False, "count": 5, "cooldownHours": 12.0},
    "killSwitch": False,                                     # 즉시 정지(마스터)
    # 사이징 하드 상한(항상 적용, off 없음): 오설정·침입으로 한 방에 계좌 날리는 걸 막는 천장.
    # 정상 매매(예: 10배·10%)엔 여유. 초과 설정은 거부가 아니라 이 값으로 클램프한다.
    "maxLeverage": 50,                                      # 레버리지 상한(초과 시 클램프)
    "maxAccountFractionPct": 90.0,                          # 한 진입 증거금 ≤ 잔고의 이 %(펀딩·수수료 여유)
}


def get_guardrails(path: str = SETTINGS_PATH) -> dict:
    """글로벌 리스크 가드레일 (기본값에 저장값 병합 → 누락 키 안전)."""
    v = _read(path).get("guardrails") or {}
    g = {k: (dict(x) if isinstance(x, dict) else x) for k, x in DEFAULT_GUARDRAILS.items()}
    if isinstance(v.get("dailyLossLimit"), dict):
        g["dailyLossLimit"].update({k: v["dailyLossLimit"][k] for k in ("enabled", "pct") if k in v["dailyLossLimit"]})
    if isinstance(v.get("maxConsecutiveLosses"), dict):
        g["maxConsecutiveLosses"].update({k: v["maxConsecutiveLosses"][k]
                                          for k in ("enabled", "count", "cooldownHours")
                                          if k in v["maxConsecutiveLosses"]})
    if "killSwitch" in v:
        g["killSwitch"] = bool(v["killSwitch"])
    if "maxLeverage" in v:
        try:
            g["maxLeverage"] = max(1, int(v["maxLeverage"]))
        except (TypeError, ValueError):
            pass
    if "maxAccountFractionPct" in v:
        try:
            g["maxAccountFractionPct"] = min(100.0, max(1.0, float(v["maxAccountFractionPct"])))
        except (TypeError, ValueError):
            pass
    return g


def set_guardrails(guardrails: dict, path: str = SETTINGS_PATH) -> dict:
    """글로벌 리스크 가드레일 저장."""
    d = _read(path)
    d["guardrails"] = dict(guardrails or {})
    return _write(d, path)


# 연속 손실 기준선 — 이 시각 **이후**에 닫힌 트레이드만 연속 기록에 센다.
# 쿨다운이 끝나거나 사용자가 수동 해제하면 여기를 '지금'으로 올려 회로를 다시 닫는다(half-open→closed).
# 설정(guardrails)이 아니라 상태라서 키를 분리한다 — 사용자가 저장한 값이 아니다.
def get_streak_reset_ms(path: str = SETTINGS_PATH) -> int:
    """연속 손실 기준선(ms). 없으면 0 = 전체 이력을 본다."""
    try:
        return int((_read(path).get("guardrailState") or {}).get("lossStreakResetMs") or 0)
    except (TypeError, ValueError):
        return 0


def set_streak_reset_ms(ms: int, path: str = SETTINGS_PATH) -> dict:
    """연속 손실 기준선을 올린다(쿨다운 만료 / 사용자 수동 해제)."""
    d = _read(path)
    st = d.get("guardrailState")
    d["guardrailState"] = {**(st if isinstance(st, dict) else {}), "lossStreakResetMs": int(ms)}
    return _write(d, path)
