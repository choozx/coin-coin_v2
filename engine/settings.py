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
