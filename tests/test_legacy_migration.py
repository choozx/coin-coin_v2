"""엔진에서 뺀 지표(MA_DIST·BODY_ATR)를 쓰는 저장 프리셋 — 읽을 때 값이 같은 거리 조건으로 바뀐다. 네트워크 없음.

서버 data/strategies 처럼 여기서 못 보는 프리셋이 재시작 때 깨지지 않게 하는 장치다.
값이 같다고 보장 못 하는 모양은 조용히 다른 조건으로 돌리지 않고 분명히 실패해야 한다.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import indicators as ind               # noqa: E402
from engine.candles import Candles                 # noqa: E402
from engine.conditions import SeriesResolver, dist_series   # noqa: E402
from engine.preset import Preset, migrate_legacy   # noqa: E402

M = 60_000


def _candles(n=800, seed=3):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, n)))
    op = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.001, n))
    high = np.maximum(op, close) * (1 + rng.uniform(0, 0.002, n))
    low = np.minimum(op, close) * (1 - rng.uniform(0, 0.002, n))
    return Candles(open_time=np.arange(n, dtype=np.int64) * M, open=op, high=high, low=low,
                   close=close, volume=rng.uniform(5, 50, n), timeframe_min=1)


def _preset(*conds):
    return {"schemaVersion": 1, "name": "legacy", "market": {"exchange": "binance-futures", "symbol": "BTCUSDT",
                                                               "timeframe": "1m", "direction": "long"},
            "entry": {"op": "AND", "children": list(conds)}, "exit": {"stopLoss": {"type": "percent", "value": 1}},
            "sizing": {"leverage": 1, "size": {"type": "equityPercent", "value": 10}}}


def test_ma_dist_becomes_same_values():
    """옛 MA_DIST 공식 (종가 − 이평) ÷ ATR(14) 와 바뀐 거리 조건 값이 같다."""
    c = _candles()
    for sma, ma in ((1, ind.sma), (0, ind.ema)):
        node = migrate_legacy({"entry": {"left": {"indicator": "MA_DIST", "period": 22, "params": {"sma": sma}},
                                         "cmp": "<", "right": -2}})["entry"]
        assert node["dist"] == "atr" and node["value"] == -2 and node["cmp"] == "<"
        want = (c.close - ma(c.close, 22)) / ind.atr(c.high, c.low, c.close, 14)
        np.testing.assert_allclose(dist_series(node, SeriesResolver(c)), want, equal_nan=True)


def test_body_atr_becomes_same_values():
    c = _candles()
    node = migrate_legacy({"entry": {"left": 1.5, "cmp": "<", "right": {"indicator": "BODY_ATR", "period": 14},
                                     "within": 3}})["entry"]
    assert node["dist"] == "absAtr" and node["cmp"] == ">" and node["value"] == 1.5 and node["within"] == 3   # 숫자가 왼쪽 → 뒤집기
    want = np.abs(c.close - c.open) / ind.atr(c.high, c.low, c.close, 14)
    np.testing.assert_allclose(dist_series(node, SeriesResolver(c)), want, equal_nan=True)


def test_preset_loads_and_validates():
    from engine import server
    form = {"symbol": "BTCUSDT", "timeframe": "1m", "days": "1", "equity": "10000", "slType": "percent", "slValue": "1",
            "tpType": "off", "leverage": "1", "sizeType": "equityPercent", "sizeValue": "10", "levTiers": [],
            "entryGroups": [{"side": "long", "conditions": [
                {"left": {"indicator": "MA_DIST", "period": 22}, "cmp": ">", "right": 2},
                {"left": {"indicator": "BODY_ATR"}, "cmp": ">=", "right": 1}]}]}
    p = Preset.from_dict(server._build_preset(form), validate=True)
    assert [ch["dist"] for ch in p.data["entryRules"][0]["when"]["children"]] == ["atr", "absAtr"]


@pytest.mark.parametrize("node", [
    {"left": {"indicator": "MA_DIST", "period": 22, "params": {"atr": 10}}, "cmp": "<", "right": -2},   # ATR 14 아님
    {"left": {"indicator": "MA_DIST", "period": 22, "params": {"htf": 60}}, "cmp": "<", "right": -2},  # 상위 TF
    {"cross": "crossUnder", "left": {"indicator": "MA_DIST", "period": 22}, "right": -2},              # 교차
    {"left": {"indicator": "BODY_ATR", "period": 10}, "cmp": ">", "right": 1},                        # ATR 14 아님
    {"left": {"indicator": "BODY_ATR"}, "cmp": "==", "right": 1},                                     # 거리 조건에 없는 비교
    {"left": {"indicator": "BODY_ATR"}, "cmp": ">", "right": {"indicator": "ATR", "period": 14}},     # 숫자 아님
], ids=range(6))
def test_unconvertible_fails_loudly(node):
    with pytest.raises(ValueError, match="엔진에서 빠졌어요"):
        Preset.from_dict(_preset(node), validate=False)


def test_untouched_when_absent():
    d = _preset({"left": {"indicator": "RSI", "period": 14}, "cmp": "<", "right": 30})
    d["name"] = "MA_DIST 실험"                     # 이름에 들어 있는 건 지표가 아니다
    assert migrate_legacy(d) is d
