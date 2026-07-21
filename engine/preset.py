"""프리셋 로드 + 스키마 검증.

프리셋 JSON은 schema/preset.schema.json 으로 검증된다. 검증 통과한 dict를
그대로 들고 다니되, 자주 쓰는 필드는 헬퍼로 접근.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

try:
    import jsonschema
except ImportError:  # 검증 없이도 동작은 하되 경고
    jsonschema = None

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "schema", "preset.schema.json")


def _load_schema():
    with open(os.path.abspath(_SCHEMA_PATH)) as f:
        return json.load(f)


@dataclass
class Preset:
    data: dict

    @classmethod
    def load(cls, path: str, validate: bool = True) -> "Preset":
        with open(path) as f:
            data = json.load(f)
        return cls.from_dict(data, validate=validate)

    @classmethod
    def from_dict(cls, data: dict, validate: bool = True) -> "Preset":
        if validate:
            if jsonschema is None:
                raise RuntimeError("jsonschema 미설치 — pip install jsonschema 또는 validate=False")
            jsonschema.validate(data, _load_schema())
        return cls(data)

    # --- 헬퍼 ---------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def symbol(self) -> str:
        return self.data["market"]["symbol"]

    @property
    def timeframe(self) -> str:
        return self.data["market"]["timeframe"]

    @property
    def direction(self) -> str:
        return self.data["market"].get("direction", "both")

    @property
    def entry(self) -> dict:
        return self.data["entry"]

    @property
    def exit(self) -> dict:
        return self.data.get("exit", {})

    @property
    def sizing(self) -> dict:
        return self.data["sizing"]

    @property
    def filter(self) -> dict:
        return self.data.get("filter", {})


# --- 전략 선택 (매매 봇이 어떤 프리셋으로 돌지) --------------------------------
# 봇이 고를 수 있는 전략 = presets/examples + presets/saved 의 프리셋들.
STRATEGY_DIRS = ("presets/examples", "presets/saved")


def load_preset_file(path: str, validate: bool = True) -> "Preset":
    """프리셋 파일 로드. presets/saved/ 는 {name, form, params, preset} 래퍼 → preset 키 사용."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return Preset.from_dict(raw.get("preset", raw), validate=validate)


def list_strategies(dirs=STRATEGY_DIRS) -> list:
    """봇이 선택 가능한 전략 목록. 반환: [{path, name, symbol, timeframe, source}, ...]."""
    import glob
    out = []
    for d in dirs:
        for fp in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                pr = load_preset_file(fp, validate=False)
                out.append({"path": fp, "name": pr.name, "symbol": pr.symbol,
                            "timeframe": pr.timeframe, "source": os.path.basename(d)})
            except Exception:
                continue                     # 깨진/래퍼-only 파일은 건너뜀
    return out


def select_strategy(path: str):
    """검증 후 control.json에 '원하는 전략'으로 기록. 봇이 다음 폴링에 무포지션이면 전환.
    알 수 없는 경로/검증 실패면 예외 → 잘못된 선택이 control에 안 써진다."""
    valid = {s["path"] for s in list_strategies()}
    if path not in valid:
        raise ValueError(f"알 수 없는 전략 경로: {path}")
    load_preset_file(path, validate=True)    # 스키마 검증(실패 시 예외)
    from . import control
    return control.set_strategy(path)
