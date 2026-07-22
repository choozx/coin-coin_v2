"""서비스 제어 — 대시보드 ↔ 봇/수집기가 파일(data/control.json)로 멈춤/재개 신호를 주고받는다.

포트 없이(공유 파일) 제어. 봇/수집기는 매 루프에서 자기 상태를 읽어:
- trader 'paused'   : 새 진입만 막음(기존 포지션 관리·청산은 계속 — 우아한 정지).
- collector 'paused': 캔들 수집을 건너뜀.
'running'(기본)이면 정상. 대시보드가 set_service로 control.json에 기록.
"""
from __future__ import annotations

import json
import os

DEFAULT_PATH = os.environ.get("CONTROL_PATH", "data/control.json")


def read_control(path: str = DEFAULT_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def service_state(service: str, path: str = DEFAULT_PATH) -> str:
    """서비스 상태 'running'(기본) 또는 'paused'."""
    return read_control(path).get(service, "running")


def _write(ctrl: dict, path: str) -> dict:
    """control dict를 원자적으로 기록."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ctrl, f, ensure_ascii=False)
    os.replace(tmp, path)
    return ctrl


def set_service(service: str, state: str, path: str = DEFAULT_PATH) -> dict:
    """control.json에 서비스 상태 기록(원자적). state = 'running' | 'paused'."""
    if state not in ("running", "paused"):
        raise ValueError("state는 running 또는 paused")
    ctrl = read_control(path)
    ctrl[service] = state
    return _write(ctrl, path)


def get_strategy(path: str = DEFAULT_PATH):
    """대시보드가 선택한 '원하는 전략' 프리셋 경로(없으면 None → 봇은 실행 시 프리셋 유지)."""
    return read_control(path).get("strategy")


def set_strategy(preset_path: str, path: str = DEFAULT_PATH) -> dict:
    """control.json에 '원하는 전략' 기록. 봇이 다음 폴링에 무포지션이면 그 전략으로 전환."""
    ctrl = read_control(path)
    ctrl["strategy"] = preset_path
    return _write(ctrl, path)


def get_symbols(path: str = DEFAULT_PATH):
    """수집기 워치리스트(대시보드 설정). 리스트 or None(미설정 → 수집기 시작인자 사용)."""
    v = read_control(path).get("collect_symbols")
    return v if isinstance(v, list) else None


def set_symbols(symbols, path: str = DEFAULT_PATH) -> dict:
    """control.json에 수집 심볼 기록. 수집기가 다음 루프에 다시 읽어 반영(재시작 불필요)."""
    ctrl = read_control(path)
    ctrl["collect_symbols"] = list(symbols)
    return _write(ctrl, path)


def get_bot_config(path: str = DEFAULT_PATH) -> dict:
    """봇 실행 설정 — 프리셋에서 안 가져오는 나머지(심볼·사이징·레버리지·실행·필터).
    대시보드 '봇 설정'이 기록. 없으면 {} → 봇은 프리셋 값 그대로 사용."""
    v = read_control(path).get("bot_config")
    return v if isinstance(v, dict) else {}


def set_bot_config(cfg: dict, path: str = DEFAULT_PATH) -> dict:
    """control.json에 봇 설정 기록. 봇이 다음 폴링에 무포지션이면 반영(재시작 불필요)."""
    ctrl = read_control(path)
    ctrl["bot_config"] = dict(cfg or {})
    return _write(ctrl, path)


def clean_symbols(raw) -> list:
    """입력 심볼 정리 — 대문자·영숫자만·중복제거(예: [' btcusdc '] → ['BTCUSDC'])."""
    out = []
    for s in raw or []:
        s = "".join(ch for ch in str(s).upper() if ch.isalnum())
        if s and s not in out:
            out.append(s)
    return out
