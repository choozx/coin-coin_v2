"""프리셋 로드 + 스키마 검증.

프리셋 JSON은 schema/preset.schema.json 으로 검증된다. 검증 통과한 dict를
그대로 들고 다니되, 자주 쓰는 필드는 헬퍼로 접근.
"""
from __future__ import annotations

import copy
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
# 봇이 고를 수 있는 전략 =
#   presets/examples : git에 있는 것 → 이미지에 구워져 배포된다(재현 가능·버전 관리됨)
#   presets/saved    : GUI가 로컬에 저장한 개인 프리셋(gitignore, 컨테이너엔 없음)
#   data/strategies  : ★ 공유 볼륨. 배포와 무관하게 파일만 던져 넣으면 목록에 뜬다.
#     프로덕션에서 "코드 배포 없이 새 전략을 투입"하는 통로 —
#       scp my.json ec2:~/auto_trading/data/strategies/ → 대시보드에서 선택(무포지션이면 핫스왑)
#     ⚠️ git 밖이라 버전 관리가 안 된다. 계속 쓸 전략은 presets/examples 로 커밋할 것 —
#        원장(trades.db)의 strategy 컬럼이 이 경로를 가리키므로, 파일이 사라지면 나중에
#        "이 거래는 어떤 전략이 친 건가"를 재현할 수 없다(tools/fill_audit.py 도 이 경로를 쓴다).
STRATEGY_DIR_DATA = os.environ.get("STRATEGY_DIR_DATA", "data/strategies")
STRATEGY_DIRS = ("presets/examples", "presets/saved", STRATEGY_DIR_DATA)


# --- 라이브 봇: 프리셋(신호) + 봇 설정(실행/리스크) 병합 -----------------------
# 봇을 돌릴 때 프리셋에선 timeframe·진입(entry/entryRules)·청산(exit)·방향만 쓰고,
# 나머지(심볼·사이징·레버리지·실행·필터)는 대시보드 '봇 설정'으로 덮어쓴다.
# 백테스트는 이 병합을 쓰지 않고 프리셋 전체를 그대로 사용한다.
BOT_OVERRIDE_SECTIONS = ("sizing", "execution", "filter")


def merge_bot_config(preset_data: dict, bot_config: dict) -> dict:
    """프리셋 data에 봇 설정을 얹은 '유효 프리셋 data'를 반환(원본 불변).
    bot_config 키: symbol(str), sizing/execution/filter(dict, 얕은 병합).
    없는 키는 프리셋 값을 그대로 유지(폴백)."""
    if not bot_config:
        return preset_data
    merged = copy.deepcopy(preset_data)
    sym = bot_config.get("symbol")
    if sym:
        merged.setdefault("market", {})["symbol"] = sym
    for sec in BOT_OVERRIDE_SECTIONS:
        ov = bot_config.get(sec)
        if isinstance(ov, dict) and ov:
            base = dict(merged.get(sec) or {})
            base.update(ov)                     # 봇이 준 키만 덮어씀(나머지는 프리셋)
            merged[sec] = base
    return merged


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
                src = os.path.basename(d)
                name = pr.name
                # saved 프리셋은 GUI 저장 시 name이 "GUI 프리셋"으로 자동 지정돼 구분이 안 됨
                # → 사용자가 지은 파일명을 표시명으로 사용
                if not name or name == "GUI 프리셋":
                    name = os.path.splitext(os.path.basename(fp))[0]
                out.append({"path": fp, "name": name, "symbol": pr.symbol,
                            "timeframe": pr.timeframe, "source": src})
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
