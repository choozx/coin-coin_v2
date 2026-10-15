"""차트 패턴 조건 — 핀바(PIN_BAR) · 삼각형 수렴 돌파(TRIANGLE_BREAK). 네트워크 없음."""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import conditions as C                 # noqa: E402
from engine import indicators as ind               # noqa: E402
from engine.candles import Candles                 # noqa: E402
from engine.preset import Preset                   # noqa: E402


# ── 핀바 ─────────────────────────────────────────────────────────────────────
def _bars(rows):
    o, h, l, c = (np.array(x, dtype=float) for x in zip(*rows))
    return o, h, l, c


def test_pin_bar_long_lower_wick_is_bullish_and_mirror_is_bearish():
    o, h, l, c = _bars([
        (10.0, 10.2, 9.0, 10.1),      # 아래꼬리 1.0/1.2=83%, 몸통 8% → 강세 핀바
        (10.0, 11.0, 9.8, 9.9),       # 위꼬리 1.0/1.2=83%, 몸통 8% → 약세 핀바
        (10.0, 10.5, 9.5, 10.4),      # 몸통 80% → 아님
        (10.0, 10.0, 10.0, 10.0),     # 길이 0 → 아님
    ])
    assert ind.pin_bar(o, h, l, c).tolist() == [1, -1, 0, 0]


def test_pin_bar_thresholds_and_within_hold():
    o, h, l, c = _bars([(10.0, 10.2, 9.4, 10.1)] + [(10.0, 10.5, 9.5, 10.4)] * 4)   # 아래꼬리 0.6/0.8=75%
    assert ind.pin_bar(o, h, l, c, wick=80).tolist()[0] == 0                         # 기준 80% 엔 미달
    assert ind.pin_bar(o, h, l, c, wick=70).tolist()[0] == 1
    assert ind.pin_bar(o, h, l, c, within=3).tolist() == [1, 1, 1, 0, 0]             # 발생 봉 포함 3봉 유지


# ── 삼각형 수렴 돌파 ───────────────────────────────────────────────────────────
def _triangle_path(breakout_up=True):
    """고점은 낮아지고(120→114→107.5) 저점은 높아지는(80→86→92.5) 지그재그 — 추세선 안쪽에서 수렴 — 뒤 돌파."""
    knots = [(0, 100), (10, 120), (20, 80), (30, 114), (40, 86), (50, 107.5), (60, 92.5), (66, 100)]
    knots += [(75, 125)] if breakout_up else [(75, 70)]
    x = np.arange(76)
    c = np.interp(x, [k for k, _ in knots], [v for _, v in knots])
    return c + 0.3, c - 0.3, c


def test_triangle_breaks_up_once_after_converging_pivots():
    h, l, c = _triangle_path(True)
    sig = ind.triangle_breakout(h, l, c, pivot=3, lookback=80)
    fired = np.nonzero(sig)[0].tolist()
    assert len(fired) == 1 and sig[fired[0]] == 1
    t = fired[0]
    t_ev, side, (i1, h1, i2, h2), (j1, l1, j2, l2), U, L = ind._triangle_events(h, l, c, pivot=3, lookback=80)[0]
    assert t_ev == t and side == 1
    assert h2 < h1 and l2 > l1                               # 고점은 낮아지고 저점은 높아진 수렴
    su = (h2 - h1) / (i2 - i1)
    assert c[t] > U and c[t - 1] <= U - su                   # 이 봉에서 처음 위 추세선을 넘었다
    assert 60 <= t <= 75                                     # 마지막 수렴 뒤 위로 뚫을 때


def test_triangle_breakdown_is_minus_one():
    h, l, c = _triangle_path(False)
    sig = ind.triangle_breakout(h, l, c, pivot=3, lookback=80)
    assert set(sig[sig != 0].tolist()) == {-1.0}


def test_triangle_needs_converging_shape():
    x = np.arange(76)                                        # 고점·저점이 같이 올라가는 채널(수렴 아님)
    c = 100 + x * 0.5 + 10 * np.sin(x / 10 * 2 * np.pi)
    assert not ind.triangle_breakout(c + 0.3, c - 0.3, c, pivot=3, lookback=80).any()


def test_triangle_is_causal():
    rng = np.random.default_rng(3)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, 3000)))
    h, l = c * 1.002, c * 0.998
    full = ind.triangle_breakout(h, l, c, pivot=4, lookback=80)
    assert full.any()
    for cut in (500, 1234, 2500):
        assert np.array_equal(ind.triangle_breakout(h[:cut], l[:cut], c[:cut], pivot=4, lookback=80), full[:cut])


# ── 엔진 조건·스키마 ───────────────────────────────────────────────────────────
def test_conditions_resolve_and_schema_accepts_both():
    h, l, c = _triangle_path(True)
    n = len(c)
    cd = Candles(open_time=np.arange(n, dtype=np.int64) * 300_000, open=c, high=h, low=l, close=c,
                 volume=np.ones(n), timeframe_min=5)
    tri = {"left": {"indicator": "TRIANGLE_BREAK", "period": 3, "params": {"lookback": 80, "within": 2}}, "cmp": ">", "right": 0}
    pin = {"left": {"indicator": "PIN_BAR", "params": {"wick": 66, "within": 1}}, "cmp": ">", "right": 0}
    res = C.SeriesResolver(cd)
    t = int(np.nonzero(ind.triangle_breakout(h, l, c, pivot=3, lookback=80))[0][0])
    assert [i for i in range(n) if C.evaluate(tri, res, i)] == [t, t + 1]        # within=2 → 돌파 봉과 다음 봉
    assert C.required_warmup_bars(tri) >= 240                                     # lookback 80 × 3
    Preset.from_dict({"schemaVersion": "1.0", "name": "t", "market": {"symbol": "BTCUSDT", "timeframe": "5m"},
                      "entry": {"op": "AND", "children": [tri, pin]}, "exit": {"timeStop": {"maxBars": 10}},
                      "sizing": {"leverage": 1, "marginMode": "isolated", "size": {"type": "equityPercent", "value": 10}}},
                     validate=True)


# ── 윌리엄스 프랙탈 · 눌림 → 프랙탈 → 재돌파 ─────────────────────────────────────
def _hl(c):
    c = np.array(c, dtype=float)
    return c + 0.1, c - 0.1, c


def test_fractal_fires_when_confirmed_not_at_center():
    h, l, c = _hl([105, 103, 101, 99, 101, 103, 105, 103, 101])
    sig = ind.fractal(h, l, n=2)
    assert sig[5] == 1 and sig[3] == 0                         # 저점(3)은 좌우 2봉이 닫힌 5에서 확정
    assert sig[6] == 0 and sig[8] == -1                        # 고점(6)도 중심이 아니라 2봉 뒤(8)에서 확정
    assert not (ind.fractal(h[:8], l[:8], n=2) < 0).any()     # 8번 봉이 없으면 고점 프랙탈은 아직 없다


def test_pullback_long_needs_dip_then_fractal_then_cross():
    ma = np.full(9, 100.0)
    h, l, c = _hl([102, 101, 99, 97, 95, 96, 98, 99, 101])     # 눌림(2~) → 저점 프랙탈(4, 6에서 확정) → 8에서 상향 돌파
    sig = ind.pullback_fractal(h, l, c, ma, n=2)
    assert np.nonzero(sig)[0].tolist() == [8] and sig[8] == 1
    h, l, c = _hl([102, 99, 98, 97, 101, 102])                 # 프랙탈 확정(5) 전에 돌파(4) → 신호 없음
    assert not ind.pullback_fractal(h, l, c, np.full(6, 100.0), n=2).any()


def test_pullback_short_is_mirror_and_window_expires():
    ma = np.full(9, 100.0)
    h, l, c = _hl([98, 99, 101, 103, 105, 104, 102, 101, 99])
    sig = ind.pullback_fractal(h, l, c, ma, n=2)
    assert np.nonzero(sig)[0].tolist() == [8] and sig[8] == -1
    h, l, c = _hl([102, 101, 99, 97, 95, 96, 98, 99, 101])
    assert not ind.pullback_fractal(h, l, c, np.full(9, 100.0), n=2, window=4).any()   # 눌림 6봉 > 창 4


def test_pullback_is_causal():
    rng = np.random.default_rng(5)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, 3000)))
    h, l = c * 1.001, c * 0.999
    ma = ind.ema(c, 20)
    full = ind.pullback_fractal(h, l, c, ma, n=3)
    assert (full > 0).any() and (full < 0).any()
    for cut in (700, 2001):
        assert np.array_equal(ind.pullback_fractal(h[:cut], l[:cut], c[:cut], ma[:cut], n=3), full[:cut])


def test_indicator_stop_uses_signal_bar_value_with_buffer():
    from engine.backtest import _exit_level, _valid_level
    c = np.linspace(100, 110, 50)
    cd = Candles(open_time=np.arange(50, dtype=np.int64) * 60_000, open=c, high=c + 0.5, low=c - 0.5, close=c,
                 volume=np.ones(50), timeframe_min=1)
    res = C.SeriesResolver(cd)
    cfg = {"type": "indicator", "indicator": {"indicator": "EMA", "period": 10}, "bufferPercent": 1.0}
    ema = ind.ema(c, 10)
    long_stop = _exit_level(cfg, c[40], -1, np.nan, cd, 40, res)
    assert np.isclose(long_stop, ema[40] * 0.99)                # 롱 손절 = EMA 아래 1%
    assert np.isclose(_exit_level(cfg, c[40], +1, np.nan, cd, 40, res), ema[40] * 1.01)
    assert np.isnan(_valid_level(_exit_level(cfg, ema[40] * 0.9, -1, np.nan, cd, 40, res), ema[40] * 0.9, -1))  # EMA 가 진입가 위면 무효
    Preset.from_dict({"schemaVersion": "1.0", "name": "t", "market": {"symbol": "BTCUSDT", "timeframe": "1m"},
                      "entry": {"left": {"indicator": "PULLBACK_FRACTAL", "period": 20, "params": {"sma": 0, "n": 7, "window": 60}},
                                "cmp": ">", "right": 0},
                      "exit": {"stopLoss": cfg, "takeProfit": {"type": "riskReward", "value": 1.5}},
                      "sizing": {"leverage": 1, "marginMode": "isolated", "size": {"type": "equityPercent", "value": 10}}},
                     validate=True)


def test_form_indicator_stop_builds_preset():
    from engine.server import _price_target
    assert _price_target({"slType": "indicator", "slInd": "EMA", "slIndPeriod": "50", "slBuffer": "0.1"}, "sl") == \
        {"type": "indicator", "indicator": {"indicator": "EMA", "period": 50}, "bufferPercent": 0.1}


# ── 이평 각도(MA_ANGLE) · 이평 엉킴(MA_CROSSES) ──────────────────────────────────
def test_ma_angle_uses_atr_scale():
    t = np.arange(300, dtype=float)
    c = 100 + 0.1 * t                                          # 봉당 +0.1, 봉 길이 1 → ATR 1
    h, l = c + 0.5, c - 0.5
    a = ind.ma_angle(h, l, c, period=20, lookback=5)
    assert np.isclose(a[-1], np.degrees(np.arctan(0.1 * 4.5)))   # ≈ 24.2°
    assert np.isclose(ind.ma_angle(h[::-1], l[::-1], c[::-1], period=20)[-1], -a[-1])   # 내림은 음수
    assert np.isclose(ind.ma_angle(h, l, c, period=20, scale=10)[-1], 45.0)           # 봉당 0.1 ATR × 10 = 45°


def test_ma_angle_is_causal_and_conditions_validate():
    rng = np.random.default_rng(7)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, 1500)))
    h, l = c * 1.002, c * 0.998
    full = ind.ma_angle(h, l, c, period=75)
    for cut in (400, 1100):
        assert np.allclose(ind.ma_angle(h[:cut], l[:cut], c[:cut], period=75), full[:cut], equal_nan=True)
    ang = {"left": {"indicator": "MA_ANGLE", "period": 75, "params": {"sma": 1, "lookback": 5}}, "cmp": ">=", "right": 20}
    tng = {"left": {"indicator": "MA_CROSSES", "period": 25, "params": {"fast": 5, "window": 30, "ma_only": 1}}, "cmp": "<", "right": 3}
    n = len(c)
    cd = Candles(open_time=np.arange(n, dtype=np.int64) * 300_000, open=c, high=h, low=l, close=c,
                 volume=np.ones(n), timeframe_min=5)
    res = C.SeriesResolver(cd)
    assert [C.evaluate(ang, res, i) for i in range(n)] == [bool(np.isfinite(v) and v >= 20) for v in full]
    Preset.from_dict({"schemaVersion": "1.0", "name": "t", "market": {"symbol": "BTCUSDT", "timeframe": "5m"},
                      "entry": {"op": "AND", "children": [ang, tng]}, "exit": {"timeStop": {"maxBars": 10}},
                      "sizing": {"leverage": 1, "marginMode": "isolated", "size": {"type": "equityPercent", "value": 10}}},
                     validate=True)
