"""영상 분석의 '🛠 구현 가능' 규칙을 엔진에 넣은 것들 — 값(무식한 계산과 대조)·인과성·프리셋 배선·본전 손절. 네트워크 없음.

PREV_HIGH/LOW · RSI_PREV_HIGH/LOW(지표 수평선 돌파) · PIVOT_LOW/HIGH(쌍바닥) · VP_ABOVE/BELOW(매물대 두께)
· BB_TOUCH_UP/DN(상단 터치 횟수) · BB_PULLBACK(하단 터치→중간선→고점 상태) · exit.breakeven(본전 손절).
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import indicators as ind               # noqa: E402
from engine import ranking, server                 # noqa: E402
from engine.backtest import BacktestConfig, run    # noqa: E402
from engine.candles import Candles                 # noqa: E402
from engine.conditions import SeriesResolver       # noqa: E402
from engine.preset import Preset                   # noqa: E402

M = 60_000


def _candles(n=800, seed=3, vol=0.002):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    op = np.r_[close[0], close[:-1]]
    high = np.maximum(op, close) * (1 + rng.uniform(0, 0.002, n))
    low = np.minimum(op, close) * (1 - rng.uniform(0, 0.002, n))
    return Candles(open_time=np.arange(n, dtype=np.int64) * M, open=op, high=high, low=low,
                   close=close, volume=rng.uniform(5, 50, n), timeframe_min=1)


def _cut(c: Candles, k: int) -> Candles:
    return Candles(open_time=c.open_time[:k], open=c.open[:k], high=c.high[:k], low=c.low[:k],
                   close=c.close[:k], volume=c.volume[:k], timeframe_min=1)


OPERANDS = [
    {"indicator": "PREV_HIGH", "period": 1}, {"indicator": "PREV_LOW", "period": 20},
    {"indicator": "RSI_PREV_HIGH", "period": 20, "params": {"rsi_length": 14}},
    {"indicator": "RSI_PREV_LOW", "period": 10, "params": {"rsi_length": 7}},
    {"indicator": "PIVOT_LOW", "period": 3, "params": {"nth": 0}}, {"indicator": "PIVOT_LOW", "period": 3, "params": {"nth": 1}},
    {"indicator": "PIVOT_HIGH", "period": 2, "params": {"nth": 0}},
    {"indicator": "VP_ABOVE", "period": 50, "params": {"band": 1.0}}, {"indicator": "VP_BELOW", "period": 50, "params": {"band": 0.5}},
    {"indicator": "BB_TOUCH_UP", "period": 20, "params": {"stddev": 2, "window": 30}},
    {"indicator": "BB_TOUCH_DN", "period": 20, "params": {"stddev": 2, "window": 30}},
    {"indicator": "BB_PULLBACK", "period": 20, "params": {"stddev": 2, "n": 2, "window": 60}},
    {"indicator": "BIG_CANDLE", "period": 14, "params": {"mult": 1.5, "within": 10}},
]


@pytest.mark.parametrize("op", OPERANDS, ids=lambda o: o["indicator"] + str(o.get("params", "")))
def test_causal(op):
    """뒤에 봉을 붙여도 과거 값이 안 바뀐다 — 미래를 안 본다."""
    c = _candles()
    full = SeriesResolver(c).resolve(op)
    assert np.isfinite(full).sum() > 100 or op["indicator"] == "BB_PULLBACK"
    for k in (150, 333, 600):
        part = SeriesResolver(_cut(c, k)).resolve(op)
        np.testing.assert_array_equal(part, full[:k])


def test_prev_extreme_matches_brute():
    c = _candles(300)
    for n in (1, 5, 20):
        h = ind.prev_extreme(c.high, n, True)
        lo = ind.prev_extreme(c.low, n, False)
        assert np.isnan(h[:n]).all()
        for t in range(n, len(c)):
            assert h[t] == c.high[t - n:t].max() and lo[t] == c.low[t - n:t].min()
    np.testing.assert_array_equal(ind.prev_extreme(c.high, 1)[1:], c.high[:-1])     # period 1 = 직전 봉


def test_rsi_prev_high_is_horizontal_line():
    c = _candles(400)
    r = SeriesResolver(c)
    rsi = r.resolve({"indicator": "RSI", "period": 14})
    line = r.resolve({"indicator": "RSI_PREV_HIGH", "period": 20, "params": {"rsi_length": 14}})
    for t in range(40, len(c)):
        assert line[t] == rsi[t - 20:t].max()


def test_pivot_level_brute():
    c = _candles(400)
    lows, _ = ind.fractal_points(c.high, c.low, 3)
    p0 = ind.pivot_level(c.high, c.low, 3, 0)
    p1 = ind.pivot_level(c.high, c.low, 3, 1)
    for t in range(len(c)):
        conf = [j for j in lows if j + 3 <= t]                     # t 에서 이미 확정된 저점만
        assert (np.isnan(p0[t]) if not conf else p0[t] == c.low[conf[-1]])
        assert (np.isnan(p1[t]) if len(conf) < 2 else p1[t] == c.low[conf[-2]])


def test_vp_share_brute():
    c = _candles(120)
    w, band = 30, 1.0
    up = ind.vp_share(c.high, c.low, c.close, c.volume, w, band, True)
    dn = ind.vp_share(c.high, c.low, c.close, c.volume, w, band, False)
    assert np.isnan(up[:w - 1]).all()
    for t in range(w - 1, len(c)):
        cur, acc_u, acc_d = c.close[t], 0.0, 0.0
        for j in range(t - w + 1, t + 1):
            hi, lo, v = c.high[j], c.low[j], c.volume[j]
            acc_u += v * max(0.0, min(hi, cur * 1.01) - max(lo, cur)) / (hi - lo)
            acc_d += v * max(0.0, min(hi, cur) - max(lo, cur * 0.99)) / (hi - lo)
        tot = c.volume[t - w + 1:t + 1].sum()
        assert up[t] == pytest.approx(100 * acc_u / tot) and dn[t] == pytest.approx(100 * acc_d / tot)


def test_bb_touches_brute():
    c = _candles(300, vol=0.004)
    upb, _, lob = ind.bollinger(c.close, 20, 2.0)
    cnt = ind.bb_touches(c.high, c.low, c.close, 20, 2.0, 30, True)
    assert np.isnan(cnt[:19]).all()
    for t in range(19, len(c)):
        a = max(0, t - 29)
        exp = sum(1 for j in range(a, t + 1) if upb[j] == upb[j] and c.high[j] >= upb[j])
        assert cnt[t] == exp
    assert np.nanmax(cnt) >= 2


def _seq(prices):
    """종가 경로 → 봉(고·저는 종가 ±0.05)."""
    cl = np.asarray(prices, float)
    return Candles(open_time=np.arange(len(cl), dtype=np.int64) * M, open=cl.copy(), high=cl + 0.05, low=cl - 0.05,
                   close=cl, volume=np.full(len(cl), 10.0), timeframe_min=1)


def test_bb_pullback_sequence():
    """평평 → 급락(하단 터치) → 중간선까지 반등 → 꺾임(고점 프랙탈 확정) → 그 고점 돌파 순서로 +1 이 켜졌다 꺼진다."""
    flat = [100 + 0.1 * np.sin(i) for i in range(40)]
    drop = [99.0, 98.0, 97.5]                                      # 하단 터치
    up = [98.0, 98.8, 99.6, 100.2]                                 # 중간선 위로 반등
    down = [99.8, 99.4, 99.2]                                      # 꺾임 → 100.2 가 좌우 2봉 고점으로 확정
    brk = [99.6, 100.6, 101.0]                                     # 고점(100.25) 돌파
    c = _seq(flat + drop + up + down + brk)
    s = ind.bb_pullback(c.high, c.low, c.close, 20, 2.0, 2, 60)
    peak = len(flat) + len(drop) + 3                               # 100.2 봉
    conf = peak + 2
    assert (s[:conf] == 0).all()                                   # 확정 전엔 꺼져 있다
    assert s[conf] == 1
    first_break = next(t for t in range(conf, len(c)) if c.close[t] > c.high[peak])
    assert (s[conf:first_break + 1] == 1).all()                    # 돌파 봉까지 켜짐
    assert (s[first_break + 1:] == 0).all()                        # 돌파 뒤엔 꺼짐


def test_bb_pullback_expires_after_window():
    """같은 경로라도 터치부터 고점 확정까지가 window 봉을 넘으면 버린다."""
    flat = [100 + 0.1 * np.sin(i) for i in range(40)]
    c = _seq(flat + [99.0, 98.0, 97.5, 98.0, 98.8, 99.6, 100.2, 99.8, 99.4, 99.2, 99.6, 100.6, 101.0])
    assert ind.bb_pullback(c.high, c.low, c.close, 20, 2.0, 2, 60).max() == 1
    assert (ind.bb_pullback(c.high, c.low, c.close, 20, 2.0, 2, 3) == 0).all()


def _form(**kw):
    p = {"symbol": "BTCUSDT", "timeframe": "1m", "days": "1", "equity": "10000", "slType": "percent", "slValue": "1",
         "tpType": "off", "leverage": "1", "sizeType": "equityPercent", "sizeValue": "10", "cooldownBars": "100",
         "avoidFundingMin": "0", "entryType": "taker", "levTiers": [],
         "entryGroups": [{"side": "long", "conditions": [{"cross": "crossOver", "left": {"source": "close"}, "right": 100.5}]}]}
    p.update(kw)
    return p


def _bt(prices, **kw):
    preset = Preset.from_dict(server._build_preset(_form(**kw)), validate=True)
    return run(_seq(prices), preset, BacktestConfig(initial_equity=10000.0, taker_fee=0.0, maker_fee=0.0))


def test_breakeven_moves_stop_to_entry():
    # 100 → 101 진입(손절 1% ≈ 99.99) → 102.2 까지(+1R 넘음) → 99.5 로 급락
    prices = [100.0] * 5 + [101.0, 101.5, 102.2, 101.8, 99.5, 99.0, 99.0]
    base = _bt(prices)
    assert len(base.trades) == 1 and base.trades[0].exit_reason == "stop_loss"
    be = _bt(prices, beEnabled=True, beType="riskReward", beValue="1", beOffset="0")
    tr = be.trades[0]
    assert tr.exit_reason == "breakeven" and tr.exit_price == pytest.approx(tr.entry_price)
    assert be.trades[0].pnl > base.trades[0].pnl
    off = _bt(prices, beEnabled=True, beType="percent", beValue="0.5", beOffset="0.1").trades[0]
    assert off.exit_reason == "breakeven" and off.exit_price == pytest.approx(off.entry_price * 1.001)


def test_breakeven_not_triggered_below_target_and_not_same_bar():
    # 트리거(+1R ≈ 102.01)에 못 미침 → 원래 손절
    t1 = _bt([100.0] * 5 + [101.0, 101.5, 101.9, 99.5, 99.0], beEnabled=True, beValue="1").trades[0]
    assert t1.exit_reason == "stop_loss"
    # 한 봉 안에서 고가가 트리거를 넘고 저가가 진입가 아래 — 같은 봉엔 안 옮긴다(낙관 금지): 그 봉의 저가는 원래 손절 위라 버티고,
    # 다음 봉부터 본전 손절이 걸린다
    c = _seq([100.0] * 5 + [101.0, 101.0, 100.8, 100.8])
    c.high[6], c.low[6], c.close[6] = 102.5, 100.5, 101.3
    preset = Preset.from_dict(server._build_preset(_form(beEnabled=True, beValue="1")), validate=True)
    m = run(c, preset, BacktestConfig(initial_equity=10000.0, taker_fee=0.0, maker_fee=0.0))
    tr = m.trades[0]
    assert tr.exit_reason == "breakeven" and tr.exit_time == int(c.open_time[7])


def test_breakeven_preset_schema_and_ranking():
    p = server._build_preset(_form(beEnabled=True, beType="percent", beValue="0.8", beOffset="0.05"))
    assert p["exit"]["breakeven"] == {"type": "percent", "value": 0.8, "offsetPercent": 0.05}
    Preset.from_dict(p, validate=True)
    assert "+0.8% 에 본전 손절" in str(ranking.describe(p))
    assert "breakeven" not in server._build_preset(_form())["exit"]


def test_breakeven_saves_live_sidecar():
    """라이브 실행기는 옮긴 손절을 사이드카에 바로 저장해야 재시작 뒤에도 본전 손절이 남는다."""
    from engine.backtest import Stepper
    from engine.executor import PaperExecutor
    from engine.candles import resample
    saved = []
    ex = PaperExecutor(equity=10000.0, taker_fee=0.0, maker_fee=0.0)
    ex._save_position = lambda: saved.append(ex.position.stop_price if ex.position else None)
    preset = Preset.from_dict(server._build_preset(_form(beEnabled=True, beValue="1")), validate=True)
    c = _seq([100.0] * 5 + [101.0, 101.5, 102.2, 101.8, 101.6])
    cfg = BacktestConfig(initial_equity=10000.0, taker_fee=0.0, maker_fee=0.0)
    st = Stepper(preset, cfg, ex)
    sig = resample(c, 1)
    res = SeriesResolver(sig)
    atr = res.resolve({"indicator": "ATR", "period": 14})
    for t in range(len(c)):
        st.step(c, sig, np.arange(len(c)), np.ones(len(c), bool), atr, res, t)
    assert ex.position is not None and ex.position.be_moved
    assert saved and saved[-1] == pytest.approx(ex.position.entry_price)


@pytest.mark.parametrize("p", [
    {"left": {"indicator": "PIVOT_LOW", "period": 5, "params": {"nth": 0}}, "cmp": "<",
     "right": {"indicator": "PIVOT_LOW", "period": 5, "params": {"nth": 1}}},
    {"cross": "crossOver", "left": {"indicator": "RSI", "period": 14},
     "right": {"indicator": "RSI_PREV_HIGH", "period": 20, "params": {"rsi_length": 14}}},
    {"left": {"source": "high"}, "cmp": ">", "right": {"indicator": "PREV_HIGH", "period": 1}},
    {"left": {"indicator": "VP_ABOVE", "period": 200, "params": {"band": 1}}, "cmp": "<", "right": 10},
    {"left": {"indicator": "BB_TOUCH_UP", "period": 20, "params": {"stddev": 2, "window": 30}}, "cmp": ">=", "right": 3},
    {"left": {"indicator": "BB_PULLBACK", "period": 20, "params": {"stddev": 2, "n": 2, "window": 60}}, "cmp": ">", "right": 0},
])
def test_presets_validate_and_run(p):
    """GUI 블록이 만드는 노드 모양 그대로 스키마 통과 + 백테스트가 돈다."""
    preset = Preset.from_dict(server._build_preset(_form(entryGroups=[{"side": "long", "conditions": [p]}])), validate=True)
    run(_candles(1500, vol=0.003), preset, BacktestConfig(initial_equity=10000.0))


def test_big_candle_holds_within():
    c = _seq([100.0] * 30 + [103.0] + [102.8] * 15)
    c.open[30] = 100.0                                              # 몸통 3 — ATR 보다 훨씬 큼
    s = ind.big_candle(c.open, c.high, c.low, c.close, 14, 2.0, 5)
    assert (s[:30] == 0).all() and (s[30:35] == 1).all() and (s[35:] == 0).all()


def _leaf(ind, op, val, period=14):
    return {"left": {"kind": "indicator", "indicator": ind, "period": period}, "op": op,
            "right": {"kind": "number", "value": val}, "note": ""}


def _draft(**kw):
    d = {"name": "t", "timeframe": "15m", "stop": {"type": "swing", "lookback": 10}, "take": {"type": "off"},
         "groups": [{"side": "long", "conds": [_leaf("RSI", ">", 50)]}], "exit_long": [], "exit_short": [],
         "time_stop_bars": 0, "assumed": [], "excluded": []}
    d.update(kw)
    return d


def test_draft_exit_groups_and_trailing():
    """영상 초안: 청산 그룹 안은 AND(예: 횡보일 때만 RSI 저점 이탈 청산), 그룹끼리 OR · 트레일링 칸 · 예전 초안(맨 조건)도 읽힘."""
    from engine import video_analyze as va
    d = _draft(exit_long=[{"conds": [_leaf("ADX", "<", 20), _leaf("RSI", "<", 40)]}, {"conds": [_leaf("RSI", ">", 80)]}],
               exit_short=[_leaf("RSI", "<", 20)],                                           # 예전 모양
               trailing={"callback_pct": 2, "activation_pct": 0.5})
    form, params = va._check_draft(d)
    xc = json.loads(form["exitCustom"])
    assert xc["long"]["op"] == "OR"
    first, second = xc["long"]["children"]
    assert first["op"] == "AND" and [c["left"]["indicator"] for c in first["children"]] == ["ADX", "RSI"]
    assert second["left"]["indicator"] == "RSI" and xc["short"]["left"]["indicator"] == "RSI"
    assert form["trailingEnabled"] and form["trailingCallback"] == "2" and form["trailingActivation"] == "0.5"
    ex = server._build_preset(params)["exit"]
    assert ex["trailing"]["callbackPercent"] == 2.0 and ex["conditionBySide"]["long"]["op"] == "OR"
    form2, _ = va._check_draft(_draft())
    assert not form2["trailingEnabled"] and form2["exitCustom"] == ""
