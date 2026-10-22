"""백테스트 결과 랭킹 — 백테스트를 돌릴 때마다 자동 등록, 프리셋·설정·기간을 가로질러 비교.

저장: data/backtest_results.jsonl (한 줄 = 한 결과, 뒤에 쓴 줄이 이긴다 · 삭제는 {"key", "deleted": true} 줄).
키 = 설정(빌드된 프리셋, 이름·설명 제외) + 심볼·봉·기간 일수(반올림)·초기자본. 같은 설정을 같은 길이로 다시 돌리면
최신 결과로 덮어쓴다(데이터가 하루 늘어 끝 시각만 달라진 재실행이 줄을 늘리지 않게).
같은 프리셋 이름이라도 설정값이 다르면 다른 줄 — '프리셋마다 달라지는 셋팅값'까지 따로 순위가 매겨진다.

범위 테스트(최적화)의 조합은 등록하지 않는다 — 수백 개 중 1등은 운이 섞인 값이라 랭킹을 왜곡한다.
실제 데이터 기간이 MIN_DAYS(1년) 미만인 백테스트도 등록하지 않는다(봉 크기와 무관) — 사용자 기준(2026-10-15).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "backtest_results.jsonl")
_LOCK = threading.Lock()
MIN_DAYS = 365          # 랭킹 등록 최소 기간(일) — 반올림 기준이라 '365일' 입력(데이터 364.99일)도 들어온다


def _settings(preset: dict) -> dict:
    return {k: v for k, v in preset.items() if k not in ("name", "description")}


def settings_key(preset: dict) -> str:
    return hashlib.sha1(json.dumps(_settings(preset), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


# ---- 설정 요약(사람이 읽는 한 줄) ----
_SRC = {"close": "종가", "open": "시가", "high": "고가", "low": "저가", "price": "종가", "volume": "거래량"}


def _opnd(o) -> str:
    if isinstance(o, (int, float)):
        return f"{o:g}"
    if "source" in o:
        return _SRC.get(o["source"], o["source"])
    s = o["indicator"] + (f"({o['period']})" if o.get("period") else "")
    pr = o.get("params") or {}
    if pr:
        s += "{" + ",".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in pr.items()) + "}"
    return s


def _node(n) -> str:
    if not isinstance(n, dict):
        return "?"
    if "op" in n:
        sep = {"AND": " · ", "OR": " 또는 "}.get(n["op"], " ")
        inner = sep.join(_node(c) for c in n.get("children") or [])
        return f"NOT({inner})" if n["op"] == "NOT" else (f"({inner})" if n["op"] == "OR" else inner)
    if "cross" in n:
        return f"{_opnd(n['left'])} {'▲' if n['cross'] == 'crossOver' else '▼'} {_opnd(n['right'])}"
    return f"{_opnd(n['left'])} {n['cmp']} {_opnd(n['right'])}"


def _target(t: dict) -> str:
    ty = t.get("type")
    if ty == "percent":
        return f"{t['value']:g}%"
    if ty == "atrMultiple":
        return f"ATR×{t['value']:g}"
    if ty == "riskReward":
        return f"손절폭×{t['value']:g}"
    if ty in ("swing", "swingLow", "swingHigh"):
        return f"전저·전고 {t.get('lookback')}봉" + (f"±{t['bufferPercent']:g}%" if t.get("bufferPercent") else "")
    if ty == "indicator":
        return f"진입 때 {_opnd(t['indicator'])}" + (f"±{t['bufferPercent']:g}%" if t.get("bufferPercent") else "")
    return str(ty)


def describe(p: dict) -> dict:
    """빌드된 프리셋 → {entry, exit, sizing} 짧은 문장."""
    rules = p.get("entryRules") or [{"side": (p.get("market") or {}).get("direction", "long"), "when": p.get("entry")}]
    entry = " | ".join(f"{'롱' if r['side'] == 'long' else '숏'}: {_node(r['when'])}" for r in rules)
    ex = p.get("exit") or {}
    parts = []
    if ex.get("stopLoss"):
        parts.append("손절 " + _target(ex["stopLoss"]))
    if ex.get("takeProfit"):
        parts.append("익절 " + _target(ex["takeProfit"]))
    if ex.get("supertrendExit"):
        st = ex["supertrendExit"]
        parts.append(f"ST({st.get('period')},×{st.get('multiplier')}) 청산")
    if ex.get("trailing"):
        parts.append(f"트레일링 {ex['trailing'].get('callbackPercent'):g}%")
    if ex.get("breakeven"):
        be = ex["breakeven"]
        trig = f"{be.get('value'):g}R" if be.get("type", "riskReward") == "riskReward" else f"+{be.get('value'):g}%"
        parts.append(f"{trig} 에 본전 손절")
    if ex.get("condition"):
        parts.append("청산조건 " + _node(ex["condition"]))
    for side, n in (ex.get("conditionBySide") or {}).items():
        parts.append(f"{'롱' if side == 'long' else '숏'} 청산 " + _node(n))
    if ex.get("timeStop"):
        parts.append(f"{ex['timeStop']['maxBars']}봉 시간청산")
    sz = p.get("sizing") or {}
    size = sz.get("size") or {}
    lev = "동적 레버리지" if sz.get("leverageTiers") else f"{sz.get('leverage')}배"
    unit = {"equityPercent": "자본 {v:g}%", "riskPercent": "리스크 {v:g}%", "fixedQuote": "고정 ${v:g}",
            "fixedBase": "고정 {v:g}개"}.get(size.get("type"), "{v}")
    return {"entry": entry, "exit": " · ".join(parts) or "청산 없음",
            "sizing": unit.format(v=size.get("value", 0)) + " · " + lev}


# ---- 저장·조회 ----
def record(params: dict, result: dict, source: dict | None = None) -> str | None:
    """백테스트 한 번의 결과를 등록. 반환 key — 기간이 1년 미만이면 등록 안 하고 None.
    params 는 화면이 보낸 것(presetName·form 포함), result 는 _run_backtest 반환."""
    preset = result["preset"]
    m = result["metrics"]
    start, end = result["dataRange"]
    days = max((end - start) / 86_400_000, 1e-9)
    if round(days) < MIN_DAYS:
        return None
    ret = m["totalReturnPct"]
    cagr = ((1 + ret / 100) ** (365.25 / days) - 1) * 100 if ret > -100 else -100.0
    mdd = m["maxDrawdownPct"]
    v = result.get("verdict") or {}
    key = hashlib.sha1(f"{settings_key(preset)}|{preset['market']['symbol']}|{preset['market']['timeframe']}|"
                       f"{round(days)}|{m['initialEquity']}".encode()).hexdigest()[:16]
    row = {
        "key": key, "at": int(time.time() * 1000),
        "presetName": (params.get("presetName") or "").strip() or "(이름 없음)",
        "settingsKey": settings_key(preset),
        "symbol": preset["market"]["symbol"], "timeframe": preset["market"]["timeframe"],
        "start": start, "end": end, "days": round(days, 1), "equity": m["initialEquity"],
        "returnPct": ret, "cagrPct": round(cagr, 2), "mddPct": mdd,
        "calmar": round(cagr / mdd, 3) if mdd > 0 else None,
        "trades": m["numTrades"], "winRatePct": m["winRatePct"], "profitFactor": m["profitFactor"],
        "sharpe": m["sharpe"], "liquidations": m["numLiquidations"], "fees": m["totalFees"],
        "beatsRandom": bool(v.get("beatsNull")) and not v.get("negative") if "beatsNull" in v else None,
        "randomPercentile": v.get("percentile"),
        "buyHoldPct": result.get("buyHoldPct"),
        "summary": describe(preset),
        # 어디서 왔나 — None=직접 백테스트 · {"kind": "range", "rank", "combos", "params"}=범위 테스트 상위 조합(전체 기간 재실행)
        "source": source,
        "form": params.get("form"), "preset": preset,
    }
    if source is not None:
        prev = _load().get(key)
        if prev is not None and prev.get("source") is None:
            row["source"] = None        # 같은 설정을 직접 돌린 결과가 이미 있으면 '직접 백테스트'로 남긴다(범위 표시로 덮지 않음)
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with _LOCK, open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return key


def _load() -> dict:
    rows = {}
    if not os.path.exists(PATH):
        return rows
    with _LOCK, open(PATH, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("deleted"):
                rows.pop(r["key"], None)
            else:
                rows[r["key"]] = r
    return rows


def list_results() -> list:
    """랭킹 표용(무거운 form·preset 은 뺀다). 기준이 생기기 전에 들어간 1년 미만 결과는 숨긴다."""
    return [{k: v for k, v in r.items() if k not in ("form", "preset")} for r in _load().values()
            if round(r.get("days", 0)) >= MIN_DAYS]


def get(params: dict) -> dict:
    r = _load().get(params.get("key"))
    if not r:
        raise ValueError("결과를 찾을 수 없어요")
    return r


def delete(params: dict) -> dict:
    keys = params.get("keys") or [params.get("key")]
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with _LOCK, open(PATH, "a", encoding="utf-8") as f:
        for k in keys:
            if k:
                f.write(json.dumps({"key": k, "deleted": True}) + "\n")
    return {"ok": True, "deleted": len([k for k in keys if k])}
