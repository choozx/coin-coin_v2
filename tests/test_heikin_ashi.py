"""영상 '하이킨아시 필승 단타' — 엔진 확장. 네트워크 없음.

HA_* 지표(시세·색·몸통·꼬리·연속·도지·꼬리 없는 봉·도지 직전 몸통, passes 2 = 울트라 근사)
· params.shift(k 봉 전 값) · 잎 조건 within(최근 N봉 중 한 번이라도).
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import indicators as ind               # noqa: E402
from engine import ranking, server, video_analyze  # noqa: E402
from engine.backtest import BacktestConfig, run    # noqa: E402
from engine.candles import Candles                 # noqa: E402
from engine.conditions import (SeriesResolver, evaluate, explain, operand_label,  # noqa: E402
                               required_warmup_bars, MIN_WARMUP_BARS, UNSTABLE_MULT)
from engine.preset import Preset                   # noqa: E402

M = 60_000


def _candles(n=1500, seed=7, vol=0.002):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    op = np.r_[close[0], close[:-1]]
    high = np.maximum(op, close) * (1 + rng.uniform(0, 0.002, n))
    low = np.minimum(op, close) * (1 - rng.uniform(0, 0.002, n))
    return Candles(open_time=np.arange(n, dtype=np.int64) * M, open=op, high=high, low=low,
                   close=close, volume=rng.uniform(5, 50, n), timeframe_min=1)


def _cut(c: Candles, k: int) -> Candles:
    return Candles(open_time=c.open_time[:k], open=c.open[:k], high=c.high[:k], low=c.low[:k],
                   close=c.close[:k], volume=c.volume[:k], timeframe_min=c.timeframe_min)


def _ha_ref(o, h, l, c):
    """교과서 공식 그대로(느린 참조 구현)."""
    n = len(c)
    hc = [(o[i] + h[i] + l[i] + c[i]) / 4 for i in range(n)]
    ho = [(o[0] + c[0]) / 2]
    for i in range(1, n):
        ho.append((ho[i - 1] + hc[i - 1]) / 2)
    hh = [max(h[i], ho[i], hc[i]) for i in range(n)]
    hl = [min(l[i], ho[i], hc[i]) for i in range(n)]
    return [np.array(x) for x in (ho, hh, hl, hc)]


def test_formula_matches_reference():
    c = _candles(300)
    got = ind.heikin_ashi(c.open, c.high, c.low, c.close)
    for g, r in zip(got, _ha_ref(c.open, c.high, c.low, c.close)):
        np.testing.assert_allclose(g, r, rtol=0, atol=1e-12)


def test_ultra_is_ha_of_ha():
    c = _candles(300)
    once = ind.heikin_ashi(c.open, c.high, c.low, c.close)
    np.testing.assert_allclose(ind.heikin_ashi(c.open, c.high, c.low, c.close, passes=2), ind.heikin_ashi(*once))


def test_streak_strong_doji_by_hand():
    #        양 양 양 음 음 0  음
    ho = np.array([1, 1, 1, 2, 2, 2, 2.0])
    hc = np.array([2, 2, 2, 1, 1, 2, 1.0])
    np.testing.assert_array_equal(ind.ha_streak(ho, hc), [1, 2, 3, -1, -2, 0, -1])
    # 꼬리 없는 양봉(저가 = 시가) · 아래꼬리 있는 양봉 · 위꼬리 없는 음봉
    ho, hc = np.array([10, 10, 12.0]), np.array([12, 12, 10.0])
    hh, hl = np.array([13, 13, 12.0]), np.array([10, 9, 9.0])
    np.testing.assert_array_equal(ind.ha_strong(ho, hh, hl, hc), [1, 0, -1])
    # 도지: 몸통 ≤ 30% · 위아래 꼬리 둘 다
    ho, hc = np.array([10, 10, 10, 10.0]), np.array([10.2, 12, 10.1, 10.2])
    hh, hl = np.array([11, 12.5, 10.1, 11]), np.array([9, 9.5, 9, 9])
    np.testing.assert_array_equal(ind.ha_doji(ho, hh, hl, hc), [1, 0, 0, 1])       # 3번째는 위꼬리 없음
    np.testing.assert_array_equal(ind.ha_doji(ho, hh, hl, hc, within=3), [1, 1, 1, 1])
    np.testing.assert_array_equal(ind.ha_doji(ho, hh, hl, hc, within=4), [1, 1, 1, 2])


def test_predoji_top():
    #  음봉(시가 12) → 도지 → 도지 → 양봉(돌파 봉) → … window 2 뒤엔 사라짐
    ho = np.array([11, 12, 10.0, 10.0, 10.0, 11, 11, 11])
    hc = np.array([12, 10, 10.1, 9.95, 12.5, 12, 12, 12])
    hh = np.array([12, 12, 11, 11, 12.6, 12, 12, 12])
    hl = np.array([11, 10, 9, 9, 10, 11, 11, 11])
    top = ind.ha_predoji(ho, hh, hl, hc, window=2, top=True)
    assert np.isnan(top[:3]).all()                # 도지가 확인되기 전(지금 봉은 안 본다)
    assert top[3] == 12 and top[4] == 12          # 첫 도지(2) 뒤 · 두 번째 도지(3) 뒤 — 묶음 앞 음봉의 시가
    assert top[5] == 12 and np.isnan(top[6])      # 마지막 도지(3)에서 2봉까지만
    assert np.isnan(ind.ha_predoji(ho, hh, hl, hc, window=2, top=False)).all()   # 앞 봉이 음봉 → 숏 기준선 없음


OPERANDS = [{"indicator": n} for n in ("HA_OPEN", "HA_HIGH", "HA_LOW", "HA_CLOSE", "HA_COLOR", "HA_BODY", "HA_UWICK",
                                       "HA_LWICK", "HA_STREAK", "HA_STRONG")] + [
    {"indicator": "HA_DOJI", "params": {"within": 4, "body": 40}},
    {"indicator": "HA_PREDOJI_TOP", "params": {"window": 5, "body": 40}},
    {"indicator": "HA_PREDOJI_BOT", "params": {"window": 5, "body": 40}},
    {"indicator": "HA_CLOSE", "params": {"passes": 2}},
    {"indicator": "HA_BODY", "params": {"shift": 1}},
    {"indicator": "RVOL", "period": 20, "params": {"shift": 3}},
    {"indicator": "HA_COLOR", "params": {"htf": 5, "shift": 1}},
]


@pytest.mark.parametrize("op", OPERANDS, ids=lambda o: o["indicator"] + str(o.get("params", "")))
def test_causal(op):
    """뒤에 봉을 붙여도 과거 값이 안 바뀐다 — 미래를 안 본다."""
    c = _candles()
    full = SeriesResolver(c).resolve(op)
    assert np.isfinite(full).sum() > 100
    for k in (300, 777, 1201):
        np.testing.assert_allclose(SeriesResolver(_cut(c, k)).resolve(op), full[:k], equal_nan=True)


def test_shift_is_k_bars_ago():
    c = _candles(200)
    r = SeriesResolver(c)
    base, sh = r.resolve({"indicator": "HA_BODY"}), r.resolve({"indicator": "HA_BODY", "params": {"shift": 2}})
    assert np.isnan(sh[:2]).all()
    np.testing.assert_array_equal(sh[2:], base[:-2])
    assert operand_label({"indicator": "HA_BODY", "params": {"shift": 2}}) == "HA_BODY[2]"


def test_within_any_of_last_n():
    c = _candles(600)
    r = SeriesResolver(c)
    leaf = {"left": {"indicator": "HA_STRONG"}, "cmp": ">", "right": 0}
    now = np.array([evaluate(leaf, r, i) for i in range(len(c))])
    w = dict(leaf, within=4)
    got = [evaluate(w, r, i) for i in range(len(c))]
    want = [bool(now[max(0, i - 3):i + 1].any()) for i in range(len(c))]
    assert got == want and sum(got) > sum(now)
    for i in range(len(c)):                         # 진단(explain)도 같은 결론
        assert explain(w, r, i)["ok"] == got[i]
    assert "최근 4봉 안" in explain(w, r, 300)["text"]


def test_within_and_shift_extend_warmup():
    base = {"left": {"indicator": "EMA", "period": 100}, "cmp": ">", "right": 0}
    assert required_warmup_bars(base) == max(MIN_WARMUP_BARS, 100 * UNSTABLE_MULT)
    assert required_warmup_bars(dict(base, within=50)) == (100 + 50) * UNSTABLE_MULT
    assert required_warmup_bars({"left": {"indicator": "RVOL", "period": 20, "params": {"shift": 120}},
                                 "cmp": "<", "right": 1}) == 120 * UNSTABLE_MULT


# ---- 영상 매매법을 프리셋으로 — 검증·백테스트·요약 ----
def _form(groups, **kw):
    p = {"symbol": "BTCUSDT", "timeframe": "1m", "days": "1", "equity": "10000", "slType": "swing", "slLookback": "10",
         "tpType": "riskReward", "tpValue": "2", "leverage": "1", "sizeType": "equityPercent", "sizeValue": "10",
         "cooldownBars": "0", "avoidFundingMin": "0", "entryType": "taker", "levTiers": [], "entryGroups": groups}
    p.update(kw)
    return p


HA = lambda name, **params: {"indicator": name, **({"params": params} if params else {})}     # noqa: E731
STRATEGIES = {
    # 색 전환 기본: 숏 = 음봉 전환 두 번째 봉 · 롱 = 최근 3봉 안 도지 뒤 몸통 큰 양봉
    "color": [{"side": "short", "conditions": [{"cross": "crossUnder", "left": HA("HA_STREAK"), "right": -1.5}]},
              {"side": "long", "conditions": [{"left": HA("HA_DOJI", within=3), "cmp": ">=", "right": 1},
                                              {"left": HA("HA_COLOR"), "cmp": ">", "right": 0},
                                              {"dist": "atr", "left": HA("HA_BODY"), "right": 0, "cmp": ">", "value": 0.5}]}],
    # 지지/저항 돌파: 도지 직전 음봉 몸통 돌파 + 전봉보다 큰 몸통 + 위꼬리 > 아래꼬리
    "breakout": [{"side": "long", "conditions": [
        {"left": HA("HA_CLOSE"), "cmp": ">", "right": HA("HA_PREDOJI_TOP", window=3)},
        {"left": HA("HA_BODY"), "cmp": ">", "right": HA("HA_BODY", shift=1)},
        {"left": HA("HA_UWICK"), "cmp": ">", "right": HA("HA_LWICK")}]}],
    # EMA20 눌림: 최근 5봉 안 EMA20 터치 · 도지 2개 이상 · 꼬리 없는 양봉 2연속
    "ema20": [{"side": "long", "conditions": [
        {"left": HA("HA_LOW"), "cmp": "<=", "right": {"indicator": "EMA", "period": 20}, "within": 5},
        {"left": HA("HA_CLOSE"), "cmp": ">", "right": {"indicator": "EMA", "period": 20}},
        {"left": HA("HA_DOJI", within=5, body=40), "cmp": ">=", "right": 2},
        {"left": HA("HA_STRONG"), "cmp": ">", "right": 0},
        {"left": HA("HA_STRONG", shift=1), "cmp": ">", "right": 0}]}],
    # 울트라(근사) 스위칭: 양봉 전환 롱 · 음봉 전환 숏
    "ultra": [{"side": "long", "conditions": [{"cross": "crossOver", "left": HA("HA_COLOR", passes=2), "right": 0}]},
              {"side": "short", "conditions": [{"cross": "crossUnder", "left": HA("HA_COLOR", passes=2), "right": 0}]}],
}


@pytest.mark.parametrize("name", STRATEGIES)
def test_video_strategies_validate_and_trade(name):
    c = _candles(6000, vol=0.003)
    exit_ha = '{"long": {"left": {"indicator": "HA_COLOR"}, "cmp": "<", "right": 0}, ' \
              '"short": {"left": {"indicator": "HA_COLOR"}, "cmp": ">", "right": 0}}'
    preset = Preset.from_dict(server._build_preset(_form(STRATEGIES[name], exitCustom=exit_ha)), validate=True)
    m = run(c, preset, BacktestConfig(initial_equity=10000.0))
    assert len(m.trades) > 0
    assert ranking.describe(preset.data)["entry"]


def test_schema_rejects_bad_within():
    bad = {"left": HA("HA_COLOR"), "cmp": ">", "right": 0, "within": 0}
    with pytest.raises(Exception):
        Preset.from_dict(server._build_preset(_form([{"side": "long", "conditions": [bad]}])), validate=True)


def test_draft_within_bars_to_form():
    """영상 초안의 within_bars → 엔진 노드 within · 화면 '직접 조건' within 칸."""
    cond = {"left": {"kind": "indicator", "indicator": "HA_LOW"}, "op": "<=",
            "right": {"kind": "indicator", "indicator": "EMA", "period": 20}, "within_bars": 5, "note": "EMA20 터치"}
    draft = {"name": "HA", "timeframe": "5m", "groups": [{"side": "long", "conds": [cond]}],
             "stop": {"type": "swing", "lookback": 10}, "take": {"type": "riskReward", "value": 2},
             "exit_long": [], "exit_short": [], "time_stop_bars": 0, "assumed": [], "excluded": []}
    form, params = video_analyze._check_draft(draft)
    assert params["entryGroups"][0]["conditions"][0]["within"] == 5
    assert form["entry"][0]["conds"][0]["within"] == "5"
    assert "HA_STREAK" in video_analyze._engine_capabilities() and "within" in video_analyze._engine_capabilities()
