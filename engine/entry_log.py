"""진입 판정 로그 — "지금 왜 안 사는가" 를 매 폴링 남긴다.

봇이 조용히 아무것도 안 할 때, 그게 조건이 안 맞아서인지 멈춰서인지 쿨다운인지 워밍업인지
밖에서는 구별할 수 없었다. 상태 파일은 '지금 무포지션'만 알려주고 이유를 안 남긴다.
여기서 판정 한 번을 한 줄(JSON)로 적는다 — 조건 트리의 참/거짓, 지표 현재값, 막힌 사유.

두 종류가 섞여 쌓인다:
  decided=true   신호봉이 닫혀 **진짜 판정**이 일어난 순간(15m 프리셋이면 15분마다)
  decided=false  그 사이 폴링의 **미리보기** — 아직 안 닫힌 신호봉 위에서 지금 값을 본 것
미리보기를 남기는 이유: 진짜 판정만 남기면 15분에 한 줄이라 "지금 RSI 가 어디쯤인지"를
볼 수 없다. 다만 미완성 봉의 값은 확정이 아니므로 반드시 구분해서 적는다.

환경변수:
    ENTRY_LOG_PATH   기록 파일(기본 data/entry_log.jsonl). 빈 값이면 기록 끔.
    ENTRY_LOG_KEEP   보관할 줄 수(기본 3000 ≈ 폴링 1분 기준 이틀). 넘으면 오래된 줄부터 버린다.
"""
from __future__ import annotations

import os

from . import jsonl_log
from .conditions import explain, explain_lines, failed_leaves, indicator_snapshot

DEFAULT_PATH = os.environ.get("ENTRY_LOG_PATH", "data/entry_log.jsonl")
DEFAULT_KEEP = int(os.environ.get("ENTRY_LOG_KEEP", "3000") or 0)

_SIDE_KO = {1: "롱", -1: "숏"}


def build(preset, entry_rules, resolver, sb, bar_ms, at_ms, side, block, price, decided: bool) -> dict:
    """판정 하나를 기록용 dict 로. 조건 트리는 사람이 읽을 줄들로 펴서 담는다.

    rules 가 여러 개면(롱 그룹 / 숏 그룹) 전부 담는다 — '롱은 왜 안 되고 숏은 왜 안 되나'가
    따로 보여야 쓸모가 있다.
    """
    rules = []
    nodes = ([(r["side"], r["when"]) for r in entry_rules] if entry_rules
             else [("long" if preset.direction != "short" else "short", preset.entry)])
    for rside, node in nodes:
        exp = explain(node, resolver, sb)
        # lines = 트리 전체(사람이 읽는 용), failed = 거짓인 말단만(무엇이 막고 있나).
        # 둘을 나눠 두면 요약 한 줄과 jq 질의가 트리를 다시 파싱하지 않아도 된다.
        rules.append({"side": rside, "ok": exp["ok"], "lines": explain_lines(exp),
                      "failed": failed_leaves(exp)})

    snapshot = {}
    for _, node in nodes:
        snapshot.update(indicator_snapshot(node, resolver, sb))

    return {
        "at": int(at_ms), "bar": int(bar_ms), "decided": bool(decided),
        "symbol": preset.symbol, "timeframe": preset.timeframe,
        "price": None if price is None else round(float(price), 4),
        "entered": _SIDE_KO.get(side), "block": block,
        "rules": rules, "indicators": snapshot,
    }


SUMMARY_FAILS = 2          # 한 줄에 이름을 적을 미충족 조건 수(나머지는 '외 N개')


def summary(rec: dict, max_fails: int = SUMMARY_FAILS) -> str:
    """한 줄 요약 — 폴링 상태줄 뒤에 붙여 docker logs 에서 바로 보이게.

    '조건 미충족' 네 글자만 매분 반복하면 아무 정보가 없다. **무엇이** 안 맞는지를 값까지
    적는다 — 어차피 알고 싶은 건 그거고, 상세 트리는 같은 줄의 rules 에 그대로 있다.
    """
    if rec.get("entered"):
        return f"진입✓ {rec['entered']}"
    head = "판정" if rec.get("decided") else "미리보기"
    base = f"진입✗ [{head}] {rec.get('block') or '?'}"
    if rec.get("block") != "진입 조건 미충족":
        return base                              # 멈춤·쿨다운 등은 사유 자체가 이미 완결이다
    parts = []
    for r in rec.get("rules") or []:
        fails = r.get("failed") or []
        if not fails:
            continue
        shown = " / ".join(fails[:max_fails])
        if len(fails) > max_fails:
            shown += f" 외 {len(fails) - max_fails}개"
        parts.append(f"{r['side']} ✗ {shown}")
    return base + (" · " + " | ".join(parts) if parts else "")


def append(rec: dict, path: str = None, keep: int = None) -> None:
    """한 줄 추가(보관 한도 적용). 배관은 jsonl_log 공용 — 실패해도 매매를 막지 않는다."""
    jsonl_log.append(rec, DEFAULT_PATH if path is None else path,
                     DEFAULT_KEEP if keep is None else keep)


def tail(path: str = None, n: int = 50) -> list:
    """최근 n 줄을 dict 로 (대시보드·디버깅용). 파일이 없으면 빈 리스트."""
    return jsonl_log.tail(DEFAULT_PATH if path is None else path, n)
