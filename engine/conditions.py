"""프리셋 조건 트리 평가.

조건 트리(schema의 condition)를 신호 타임프레임 캔들 위에서 계산.
operand(상수/시세/지표)를 시계열로 변환해 캐시하고, 특정 인덱스 i에서
논리/비교/교차 노드를 평가한다. NaN(워밍업)은 항상 false.
"""
from __future__ import annotations

import json
import numpy as np

from . import indicators as ind
from .candles import Candles


class SeriesResolver:
    """operand → numpy 시계열. 지표는 키 기준 캐싱."""

    def __init__(self, candles: Candles):
        self.c = candles
        self._cache: dict = {}

    def _key(self, operand) -> str:
        return json.dumps(operand, sort_keys=True)

    def resolve(self, operand) -> np.ndarray:
        if isinstance(operand, (int, float)):
            return np.full(len(self.c), float(operand))
        key = self._key(operand)
        if key in self._cache:
            return self._cache[key]
        series = self._compute(operand)
        self._cache[key] = series
        return series

    def _compute(self, operand: dict) -> np.ndarray:
        c = self.c
        if "source" in operand:
            src = operand["source"]
            return {
                "open": c.open, "high": c.high, "low": c.low,
                "close": c.close, "price": c.close, "volume": c.volume,
            }[src]

        name = operand["indicator"]
        period = operand.get("period")
        params = operand.get("params", {})
        if name == "SMA":
            return ind.sma(c.close, period)
        if name == "EMA":
            return ind.ema(c.close, period)
        if name == "RSI":
            return ind.rsi(c.close, period or 14)
        if name in ("MACD", "MACD_signal", "MACD_hist"):
            line, sig, hist = ind.macd(c.close,
                                       int(params.get("fast", 12)),
                                       int(params.get("slow", 26)),
                                       int(params.get("signal", 9)))
            return {"MACD": line, "MACD_signal": sig, "MACD_hist": hist}[name]
        if name in ("BB_upper", "BB_mid", "BB_lower"):
            up, mid, lo = ind.bollinger(c.close, period or 20, float(params.get("stddev", 2.0)))
            return {"BB_upper": up, "BB_mid": mid, "BB_lower": lo}[name]
        if name == "ATR":
            return ind.atr(c.high, c.low, c.close, period or 14)
        if name == "ADX":
            return ind.adx(c.high, c.low, c.close, period or 14)
        if name == "PLUS_DI":
            return ind.plus_di(c.high, c.low, c.close, period or 14)
        if name == "MINUS_DI":
            return ind.minus_di(c.high, c.low, c.close, period or 14)
        if name in ("STOCH_K", "STOCH_D"):
            k, d = ind.stochastic(c.high, c.low, c.close, period or 14,
                                  int(params.get("smooth_k", 3)), int(params.get("smooth_d", 3)))
            return k if name == "STOCH_K" else d
        if name in ("STOCHRSI_K", "STOCHRSI_D"):
            p = period or 14
            k, d = ind.stoch_rsi(c.close, p, p,
                                 int(params.get("smooth_k", 3)), int(params.get("smooth_d", 3)))
            return k if name == "STOCHRSI_K" else d
        if name == "RVOL":
            return ind.rvol(c.volume, period or 20)
        if name == "CCI":
            return ind.cci(c.high, c.low, c.close, period or 20)
        if name == "MFI":
            return ind.mfi(c.high, c.low, c.close, c.volume, period or 14)
        if name == "VWAP":
            return ind.vwap(c.high, c.low, c.close, c.volume)
        if name in ("TAKER_DELTA", "TAKER_DELTA_RATIO", "CVD", "CVD_EMA"):
            if c.taker_buy is None:                 # 오더플로우 데이터 없음 → 항상 false
                return np.full(len(c), np.nan)
            if name == "TAKER_DELTA":
                return ind.taker_delta(c.volume, c.taker_buy)
            if name == "TAKER_DELTA_RATIO":
                return ind.taker_delta_ratio(c.volume, c.taker_buy)
            series = ind.cvd(c.volume, c.taker_buy)
            return series if name == "CVD" else ind.ema(series, period or 20)
        if name == "HAWKEYE":
            return ind.hawkeye(c.high, c.low, c.close, c.volume,
                               period or 200, float(params.get("divisor", 3.6)))
        if name in ("QQE_MOD", "QQE_RSI", "QQE_LINE"):
            rl = period or int(params.get("rsi_length", 6))
            sm = int(params.get("smoothing", 5))
            if name == "QQE_MOD":
                return ind.qqe_mod(c.close, rl, sm,
                                   float(params.get("factor_primary", 3.0)),
                                   float(params.get("factor_secondary", 1.61)),
                                   float(params.get("threshold", 3.0)),
                                   int(params.get("bb_length", 50)),
                                   float(params.get("bb_mult", 0.35)))
            line, rsi_ma = ind.qqe(c.close, rl, sm, float(params.get("factor_primary", 3.0)))
            return (rsi_ma - 50.0) if name == "QQE_RSI" else (line - 50.0)
        if name in ("SUPERTREND", "SUPERTREND_DIR"):
            line, d = ind.supertrend(c.high, c.low, c.close, period or 10,
                                     float(params.get("multiplier", 3.0)))
            return line if name == "SUPERTREND" else d
        # 캔들스틱 반전 패턴 (종합 강세/약세 먼저, 그 외 CDL_XXX는 개별 패턴)
        if name == "CDL_BULLREV":
            return ind.reversal(c.open, c.high, c.low, c.close, bull=True)
        if name == "CDL_BEARREV":
            return ind.reversal(c.open, c.high, c.low, c.close, bull=False)
        if name.startswith("CDL_"):
            return ind.candle(name[4:], c.open, c.high, c.low, c.close)
        raise ValueError(f"알 수 없는 지표: {name}")


_CMP = {
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


def evaluate(node: dict, resolver: SeriesResolver, i: int) -> bool:
    """조건 노드를 인덱스 i에서 평가. NaN 포함 시 false."""
    if node is None:
        return False

    # 논리 노드
    if "op" in node:
        op = node["op"]
        children = node["children"]
        if op == "AND":
            return all(evaluate(ch, resolver, i) for ch in children)
        if op == "OR":
            return any(evaluate(ch, resolver, i) for ch in children)
        if op == "NOT":
            return not evaluate(children[0], resolver, i)
        raise ValueError(f"알 수 없는 op: {op}")

    # 교차 노드
    if "cross" in node:
        if i < 1:
            return False
        left = resolver.resolve(node["left"])
        right = resolver.resolve(node["right"])
        a0, a1 = left[i - 1], left[i]
        b0, b1 = right[i - 1], right[i]
        if np.isnan([a0, a1, b0, b1]).any():
            return False
        if node["cross"] == "crossOver":
            return a0 <= b0 and a1 > b1
        else:  # crossUnder
            return a0 >= b0 and a1 < b1

    # 비교 노드
    left = resolver.resolve(node["left"])[i]
    right = resolver.resolve(node["right"])[i]
    if np.isnan(left) or np.isnan(right):
        return False
    return bool(_CMP[node["cmp"]](left, right))


# ── 설명(진단) ────────────────────────────────────────────────────────────────
# evaluate 는 참/거짓만 준다. 그것만으로는 "왜 진입을 안 했나"에 답할 수 없다 —
# 어느 조건이 걸렸고 지표가 지금 얼마인지가 안 보인다. explain 은 같은 트리를 같은 규칙으로
# 걸으면서 그 과정을 남긴다. **판정 로직을 복제하지 않는다** — 리프에서 실제 비교를 수행하고
# 논리 노드는 자식 결과를 합치므로, evaluate 와 결론이 갈릴 수 없다(테스트로 고정).

_CMP_KO = {"<": "<", "<=": "≤", ">": ">", ">=": "≥", "==": "=", "!=": "≠"}


def fmt_value(v) -> str:
    """지표 값을 사람이 읽을 자릿수로. NaN 은 워밍업(데이터 부족)이라 그렇게 적는다."""
    if v is None:
        return "-"
    v = float(v)
    if np.isnan(v):
        return "NaN(워밍업)"
    a = abs(v)
    s = f"{v:.2f}" if a >= 100 else (f"{v:.3f}" if a >= 1 else f"{v:.5f}")
    return s.rstrip("0").rstrip(".") if "." in s else s


def operand_label(operand) -> str:
    """operand → 'RSI(14)' / '종가' / '30' 같은 짧은 이름."""
    if isinstance(operand, (int, float)):
        return fmt_value(operand)
    if "source" in operand:
        return {"open": "시가", "high": "고가", "low": "저가", "close": "종가",
                "price": "가격", "volume": "거래량"}.get(operand["source"], operand["source"])
    name = operand["indicator"]
    # 파라미터는 이름 없이 값만 — 'SUPERTREND_DIR(10,3.0)' / 'MACD(12,26,9)' 처럼
    # 차트에서 쓰는 표기와 같다. 'multiplier=3.0' 은 정확하지만 매분 찍히는 줄에선 자리만 먹는다.
    parts = [str(operand["period"])] if operand.get("period") else []
    parts += [str(v) for v in (operand.get("params") or {}).values()]
    return f"{name}({','.join(parts)})" if parts else name


def collect_operands(node: dict, out: list = None) -> list:
    """조건 트리에 등장하는 지표/시세 operand 를 중복 없이 모은다(지표 현황 스냅샷용)."""
    out = [] if out is None else out
    if not isinstance(node, dict):
        return out
    if "op" in node:
        for ch in node["children"]:
            collect_operands(ch, out)
        return out
    for side in ("left", "right"):
        o = node.get(side)
        if isinstance(o, dict) and o not in out:
            out.append(o)
    return out


def indicator_snapshot(node: dict, resolver: SeriesResolver, i: int) -> dict:
    """조건에 쓰인 지표들의 현재 값 {'RSI(14)': 52.31, ...}. 상수는 뺀다(볼 이유가 없다)."""
    snap = {}
    for o in collect_operands(node):
        try:
            v = float(resolver.resolve(o)[i])
        except Exception:
            continue
        snap[operand_label(o)] = None if np.isnan(v) else round(v, 6)
    return snap


def explain(node: dict, resolver: SeriesResolver, i: int) -> dict:
    """조건 노드 → {'ok': bool, 'text': 한 줄, 'children': [...]}. evaluate 와 같은 규칙."""
    if node is None:
        return {"ok": False, "text": "조건 없음", "children": []}

    if "op" in node:
        op = node["op"]
        kids = [explain(ch, resolver, i) for ch in node["children"]]
        if op == "AND":
            ok = all(k["ok"] for k in kids)
        elif op == "OR":
            ok = any(k["ok"] for k in kids)
        elif op == "NOT":
            ok = not kids[0]["ok"]
        else:
            raise ValueError(f"알 수 없는 op: {op}")
        met = sum(1 for k in kids if k["ok"])
        head = {"AND": "모두", "OR": "하나라도", "NOT": "아님"}[op]
        return {"ok": ok, "text": f"{head} ({met}/{len(kids)} 충족)", "children": kids}

    if "cross" in node:
        ln, rn = operand_label(node["left"]), operand_label(node["right"])
        arrow = "상향돌파" if node["cross"] == "crossOver" else "하향돌파"
        if i < 1:
            return {"ok": False, "text": f"{ln} {arrow} {rn} — 직전 봉 없음", "children": []}
        left, right = resolver.resolve(node["left"]), resolver.resolve(node["right"])
        a0, a1, b0, b1 = left[i - 1], left[i], right[i - 1], right[i]
        # 상수 기준선은 '0' 으로만 — 움직이지 않는 값에 '0 0→0' 을 붙이면 읽기만 어렵다.
        lt = f"{ln} {fmt_value(a0)}→{fmt_value(a1)}"
        rt = rn if isinstance(node["right"], (int, float)) else f"{rn} {fmt_value(b0)}→{fmt_value(b1)}"
        text = f"{lt} {arrow} {rt}"
        if np.isnan([a0, a1, b0, b1]).any():
            return {"ok": False, "text": text, "children": []}      # 값에 NaN 이 그대로 보인다
        ok = (a0 <= b0 and a1 > b1) if node["cross"] == "crossOver" else (a0 >= b0 and a1 < b1)
        return {"ok": bool(ok), "text": text, "children": []}

    lv, rv = resolver.resolve(node["left"])[i], resolver.resolve(node["right"])[i]
    # 상수는 '30' 으로, 지표는 'RSI(14)=27.4' 로 — 상수에 '30=30' 을 붙이면 읽기만 어렵다.
    lt = fmt_value(lv) if isinstance(node["left"], (int, float)) else f"{operand_label(node['left'])}={fmt_value(lv)}"
    rt = fmt_value(rv) if isinstance(node["right"], (int, float)) else f"{operand_label(node['right'])}={fmt_value(rv)}"
    text = f"{lt} {_CMP_KO[node['cmp']]} {rt}"
    if np.isnan(lv) or np.isnan(rv):
        return {"ok": False, "text": text, "children": []}          # fmt_value 가 이미 NaN(워밍업)
    return {"ok": bool(_CMP[node["cmp"]](lv, rv)), "text": text, "children": []}


def failed_leaves(exp: dict) -> list:
    """explain 트리에서 **거짓인 말단 조건**의 텍스트만. "무엇이 막고 있나"의 답.

    논리 노드('모두 (1/2 충족)')는 뺀다 — 그건 요약이지 원인이 아니다. 사람이 알고 싶은 건
    'RVOL(20)=1.399 > 1.5' 처럼 지금 값이 어디서 모자라는가다.
    OR 안의 거짓 가지도 담는다: OR 은 하나만 참이면 되지만, 다 거짓이면 전부가 원인이다.
    """
    if exp["ok"]:
        return []
    kids = exp.get("children") or []
    if not kids:
        return [exp["text"]]
    out = []
    for ch in kids:
        out += failed_leaves(ch)
    return out


def explain_lines(exp: dict, depth: int = 0) -> list:
    """explain 트리 → 들여쓴 사람용 줄 목록(로그 출력용)."""
    mark = "✓" if exp["ok"] else "✗"
    lines = [f"{'  ' * depth}{mark} {exp['text']}"]
    for ch in exp.get("children") or []:
        lines += explain_lines(ch, depth + 1)
    return lines
