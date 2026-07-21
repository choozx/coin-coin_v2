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
