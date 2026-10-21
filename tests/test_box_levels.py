"""BOX_HIGH·BOX_LOW (직전 박스 고·저, 영상 '5봉 박스 돌파') — 값·인과성·창 시작 위치 무관·데이터 구멍·프리셋 배선. 네트워크 없음.

박스 = 시간 경계로 n 봉씩 끊은 블록. 1분봉 n=5 면 '직전 5분봉'의 고·저와 같아야 한다.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import indicators as ind               # noqa: E402
from engine import server                          # noqa: E402
from engine.backtest import BacktestConfig, run    # noqa: E402
from engine.candles import Candles, resample       # noqa: E402
from engine.conditions import SeriesResolver       # noqa: E402
from engine.preset import Preset                   # noqa: E402

M = 60_000
T0 = 1_700_000_100_000 - (1_700_000_100_000 % (60 * M))    # 정시(5분·1시간 경계 모두 맞음)


def _candles(n=600, seed=1, t0=T0):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    high = close * (1 + rng.uniform(0, 0.002, n))
    low = close * (1 - rng.uniform(0, 0.002, n))
    return Candles(open_time=t0 + np.arange(n, dtype=np.int64) * M, open=close, high=high, low=low,
                   close=close, volume=np.full(n, 10.0), timeframe_min=1)


def test_equals_previous_5m_candle():
    c = _candles()
    bh, bl = ind.box_levels(c.open_time, c.high, c.low, 5, M)
    assert np.isnan(bh[:5]).all()                                   # 첫 박스엔 직전 박스가 없다
    c5 = resample(c, 5)
    for i in range(5, len(c)):
        k = i // 5                                                  # i 가 속한 5분봉 → 직전 5분봉 = k-1
        assert bh[i] == c5.high[k - 1] and bl[i] == c5.low[k - 1]


def test_causal_and_window_invariant():
    c = _candles()
    full_h, full_l = ind.box_levels(c.open_time, c.high, c.low, 5, M)
    # 미래 봉을 붙여도 과거 값은 그대로
    h, _ = ind.box_levels(c.open_time[:300], c.high[:300], c.low[:300], 5, M)
    np.testing.assert_array_equal(h, full_h[:300])
    # 창이 블록 중간(7번째 봉)에서 시작: 잘린 첫 블록(7~9)은 직전 박스로 안 쓴다 → 10~14 는 NaN, 15 부터 같다
    h, l = ind.box_levels(c.open_time[7:], c.high[7:], c.low[7:], 5, M)
    assert np.isnan(h[:8]).all()
    np.testing.assert_array_equal(h[8:], full_h[15:])
    np.testing.assert_array_equal(l[8:], full_l[15:])


def test_missing_block_gives_nan():
    c = _candles(40)
    keep = np.r_[0:10, 15:40]                                       # 10~14 블록이 통째로 빔
    h, _ = ind.box_levels(c.open_time[keep], c.high[keep], c.low[keep], 5, M)
    assert np.isnan(h[10:15]).all()                                 # 원래 15~19 — 직전 블록이 없다
    assert not np.isnan(h[15:20]).any()


def test_resampled_timeframe_aligns_to_hours():
    c15 = resample(_candles(24 * 60), 15)
    r = SeriesResolver(c15)
    bh = r.resolve({"indicator": "BOX_HIGH", "period": 4})          # 15분봉 4개 = 1시간 박스
    c1h = resample(_candles(24 * 60), 60)
    for i in range(4, len(c15)):
        assert bh[i] == c1h.high[i // 4 - 1]


def test_preset_breakout_entries():
    """GUI '직전 박스 돌파'와 같은 노드 — 진입 봉은 전부 종가가 직전 박스 고가를 처음 넘은 봉이어야 한다."""
    c = _candles(3000, seed=4)
    p = {"symbol": "BTCUSDT", "timeframe": "1m", "days": "3", "equity": "10000", "slType": "swing", "slLookback": "1",
         "slBuffer": "0", "tpType": "riskReward", "tpValue": "1.5", "leverage": "1", "sizeType": "equityPercent",
         "sizeValue": "10", "cooldownBars": "0", "avoidFundingMin": "0", "entryType": "taker", "levTiers": [],
         "entryGroups": [{"side": "long", "conditions": [{"cross": "crossOver", "left": {"source": "close"},
                                                          "right": {"indicator": "BOX_HIGH", "period": 5}}]}]}
    preset = Preset.from_dict(server._build_preset(p), validate=True)
    m = run(c, preset, BacktestConfig(initial_equity=10000.0))
    assert m.num_trades > 10
    bh, _ = ind.box_levels(c.open_time, c.high, c.low, 5, M)
    sig = {int(c.open_time[i]) for i in range(1, len(c))
           if c.close[i] > bh[i] and not (c.close[i - 1] > bh[i - 1])}
    for tr in m.trades:
        t = int(tr.entry_time) if hasattr(tr, "entry_time") else int(tr["entry_time"])
        assert t in sig or (t - M) in sig                            # 신호 봉 종가 또는 다음 봉 시가 체결


def test_swing_one_bar_is_signal_bar_low():
    """영상의 '진입 봉 저점 손절' = swing lookback 1 (스키마 최소값을 2→1 로 내렸다). 엔진 계산은 원래 1 도 됐다."""
    from engine.backtest import _exit_level
    c = _candles(50)
    for sb in (10, 20, 30):
        assert _exit_level({"type": "swing", "lookback": 1}, c.close[sb], -1, float("nan"), c, sb) == c.low[sb]
