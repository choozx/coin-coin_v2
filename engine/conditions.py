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
