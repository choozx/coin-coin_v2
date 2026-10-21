"""백테스트 화면 'AI 최적화' — 지금 폼의 프리셋을 보고 AI 가 탐색 범위를 고르고, 결과를 읽어 설명한다.

흐름: 화면이 '범위' 체크가 가능한 값 목록(후보)과 폼을 보냄 → Claude 가 후보 중 1~3개와 범위를 고름(propose)
→ 화면이 그 범위로 기존 범위 테스트(/api/optimize, aiOpt=true)를 돌림 → 끝나면 결과를 보내 설명을 받음(explain).
Claude 는 영상 분석과 같은 방식(로컬 Claude Code, 도구 없음)으로 부른다.

다중검정: AI 최적화를 같은 프리셋에 반복하면 실제로 시험한 조합은 누적인데 격자 하나만 세면 판정이 매번
느슨해진다 → 프리셋 이름별로 시험한 조합 수를 data/ai_optimize.json 에 쌓고 귀무 게이트의 N 으로 쓴다.
"""
from __future__ import annotations

import json
import os
import threading

from .optimize import expand_spec
from .video_analyze import _claude, _engine_capabilities

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(ROOT, "data", "ai_optimize.json")
MAX_COMBOS = 200
MAX_DIMS = 3
_LOCK = threading.Lock()

SYSTEM = (
    "너는 트레이딩 백테스트의 파라미터 최적화를 설계하고 결과를 해석하는 퀀트 개발자다. "
    "수익을 장담하지 않고, 과최적화와 운을 경계한다. 답은 한국어 해요체로 짧게."
)


# ---- 프리셋별 누적 시험 조합 수 ----
def _key(name) -> str:
    return (name or "").strip() or "(이름 없음)"


def tested(name) -> int:
    try:
        with open(LEDGER, encoding="utf-8") as f:
            return int(json.load(f).get(_key(name), 0))
    except (OSError, ValueError):
        return 0


def add_tested(name, n: int) -> int:
    with _LOCK:
        try:
            with open(LEDGER, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        d[_key(name)] = int(d.get(_key(name), 0)) + int(n)
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        return d[_key(name)]


# ---- 범위 고르기 ----
def _check(sweep: list, cands: dict) -> dict:
    """AI 가 낸 범위 → {key: {min,max,step}}. 후보에 없거나, 정수 자리에 소수거나, 조합이 너무 많으면 ValueError."""
    if not 1 <= len(sweep) <= MAX_DIMS:
        raise ValueError(f"탐색 값은 1~{MAX_DIMS}개여야 해요({len(sweep)}개)")
    out, combos = {}, 1
    for s in sweep:
        k = s.get("key")
        if k not in cands:
            raise ValueError(f"'{k}' 는 범위를 켤 수 있는 값이 아니에요")
        if k in out:
            raise ValueError(f"'{cands[k]['label']}' 를 두 번 골랐어요")
        lo, hi, st = float(s["min"]), float(s["max"]), float(s["step"])
        if st <= 0 or hi <= lo:
            raise ValueError(f"'{cands[k]['label']}' — min < max, step > 0 이어야 해요")
        if cands[k].get("int") and not (lo.is_integer() and st.is_integer()):
            raise ValueError(f"'{cands[k]['label']}' 는 정수 값이에요")
        spec = {"min": lo, "max": hi, "step": st}
        n = len(expand_spec(spec))
        if n < 2:
            raise ValueError(f"'{cands[k]['label']}' 범위의 값이 하나뿐이에요")
        combos *= n
        out[k] = spec
    if combos > MAX_COMBOS:
        raise ValueError(f"조합이 {combos}개예요 — {MAX_COMBOS}개 이하로")
    return out


def _schema(keys: list) -> dict:
    return {"type": "object", "properties": {
        "reply": {"type": "string", "description": "무엇을 왜 이 범위로 탐색하는지 2~4줄"},
        "sweep": {"type": "array", "items": {"type": "object", "properties": {
            "key": {"type": "string", "enum": keys}, "min": {"type": "number"},
            "max": {"type": "number"}, "step": {"type": "number"}},
            "required": ["key", "min", "max", "step"]}}},
        "required": ["reply", "sweep"]}


def propose(params: dict) -> dict:
    """{presetName, candidates: [{key,label,value,def:[min,max,step],int}], params(폼 collect)} → {reply, sweep, combos, testedBefore}."""
    cands = {c["key"]: c for c in params.get("candidates") or []}
    if not cands:
        raise ValueError("범위를 켤 수 있는 값이 없어요 — 진입 조건을 먼저 넣어 주세요")
    from .server import _build_preset
    preset = _build_preset(params.get("params") or {})
    before = tested(params.get("presetName"))
    prompt = (
        f"프리셋 이름: {_key(params.get('presetName'))}\n"
        f"프리셋(JSON):\n{json.dumps(preset, ensure_ascii=False, indent=1)}\n\n"
        f"{_engine_capabilities()}\n\n"
        "범위를 켤 수 있는 값(key · 이름 · 지금 값 · 화면 기본 범위[min,max,step] · 정수 여부):\n"
        + "\n".join(f"- {k} · {c.get('label')} · {c.get('value')} · {c.get('def')} · {'정수' if c.get('int') else '실수'}"
                    for k, c in cands.items())
        + f"\n\n이 프리셋으로 지금까지 AI 최적화가 시험한 조합: {before}개\n\n"
        "할 일: 결과를 가장 크게 좌우할 값 1~3개를 골라 탐색 범위를 정하라.\n"
        f"- 곱한 조합 수 {MAX_COMBOS}개 이하. 정수 값은 정수 min·step. 지금 값이 범위 안에 들게, 지표 의미상 말이 되는 범위로.\n"
        "- 레버리지·사이징은 고르지 마라 — 신호가 아니라 노출 크기라 고르면 수익만 부풀거나 줄인다.\n"
        "- 판정은 앞 70% 에서 고르고 뒤 30% 에서 'N개 중 최고' 랜덤 기준과 비교한다(N=누적 시험 조합). "
        "조합을 늘릴수록 기준이 올라가니 넓게 훑기보다 핵심만."
    )
    out, mc = _claude(prompt, _schema(list(cands)), SYSTEM, timeout=300)
    try:
        sweep = _check(out.get("sweep") or [], cands)
    except ValueError as e:                                         # 한 번만 고쳐 달라고
        out, mc2 = _claude(prompt + f"\n\n[너의 직전 답]\n{json.dumps(out, ensure_ascii=False)}\n\n그 범위가 안 된다: {e}\n고쳐서 다시.",
                           _schema(list(cands)), SYSTEM, timeout=300)
        mc["cost"] = (mc["cost"] or 0) + (mc2["cost"] or 0)
        sweep = _check(out.get("sweep") or [], cands)
    combos = 1
    for s in sweep.values():
        combos *= len(expand_spec(s))
    return {"reply": out.get("reply", ""), "sweep": sweep, "combos": combos, "testedBefore": before,
            "costUsd": mc["cost"]}


# ---- 결과 설명 ----
def explain(params: dict) -> dict:
    """{presetName, labels: {key: 이름}, reason, result(최적화 done 이벤트)} → {reply}."""
    r = params.get("result") or {}
    labels = params.get("labels") or {}
    top = [{"값": {labels.get(k, k): v for k, v in (t.get("params") or {}).items()},
            "앞70%": t.get("is"), "뒤30%": t.get("oos"),
            "랜덤판정": {k: (t.get("null") or {}).get(k) for k in
                      ("strategyPct", "bestOfNP95", "nullMedian", "percentile", "beatsBestOfN", "negative", "error")}}
           for t in (r.get("top") or [])[:8]]
    prompt = (
        f"프리셋: {_key(params.get('presetName'))}\n범위를 고른 이유(네가 앞서 쓴 것): {params.get('reason', '')}\n\n"
        f"최적화 결과 — 이번 {r.get('totalCombos')}조합, 최소 거래 수 넘은 {r.get('passed')}개, "
        f"판정 N(누적 시험 조합) {r.get('gateCombos') or r.get('totalCombos')}개. 앞70% Calmar 순 상위:\n"
        f"{json.dumps(top, ensure_ascii=False, indent=1)}\n\n"
        "할 일: 결과를 사용자에게 설명하라. 통과 = 뒤30% 수익이 양수이고 bestOfNP95 를 넘음(beatsBestOfN 이고 negative 아님). "
        "앞70% 수익은 고른 값이라 판정에 안 쓴다. 통과가 없으면 솔직하게. 값이 범위 끝에 몰렸는지, 이웃 값도 비슷한지(봉우리가 넓은지) 짚고, "
        "범위를 넓혀 다시 돌리면 N 이 늘어 기준이 올라간다는 점은 필요할 때만. 4~8줄."
    )
    out, mc = _claude(prompt, {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"]},
                      SYSTEM, timeout=300)
    return {"reply": out.get("reply", ""), "costUsd": mc["cost"]}
