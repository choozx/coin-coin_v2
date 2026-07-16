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
