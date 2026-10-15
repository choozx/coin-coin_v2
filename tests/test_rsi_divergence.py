"""RSI 다이버전스(RSI_DIV) — 신호 규칙·대칭·인과성·라이브 창 수렴·프리셋 배선. 네트워크 없음.

규칙은 research/exp_DV_rsi_divergence.signals_long 과 같다(BTC 5m 6.9년에서 신호 2,423개 일치 확인).
상태가 있는 신호라 '미래 봉을 붙여도 과거 신호가 안 바뀌는가'(인과성)와 '라이브처럼 최근 창만 넣어도
끝부분이 같은가'(창 수렴)를 따로 고정한다.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import conditions as C                 # noqa: E402
from engine import indicators as ind               # noqa: E402
from engine.candles import Candles                 # noqa: E402
from engine.preset import Preset                   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _divergence_path():
    """평탄 → 급락(저점1, RSI 바닥) → 반등 → 완만한 하락으로 더 낮은 저점2(RSI 는 덜 빠짐) → 상승."""
    seg = [np.full(60, 100.0) + np.tile([0.05, -0.05], 30)]
    seg.append(100 - np.arange(1, 13) * 0.8)
    seg.append(seg[-1][-1] + np.arange(1, 7) * 0.6)
    seg.append(seg[-1][-1] - np.arange(1, 21) * 0.21)
    seg.append(seg[-1][-1] + np.arange(1, 16) * 0.5)
    c = np.concatenate(seg)
    return c + 0.05, c - 0.05, c


def test_bullish_divergence_fires_once_after_rsi_recovers():
    h, l, c = _divergence_path()
    s = ind.rsi_divergence(h, l, c)
    hits = np.nonzero(s == 1)[0]
    assert len(hits) == 1 and not (s == -1).any()
    t = hits[0]
    r = ind.rsi(c, 14)
    assert l[60:73].min() > l[79:99].min()                 # 가격 저점은 낮아졌고
    assert r[60:73].min() < r[79:99].min()                 # RSI 저점은 높아졌다
    assert r[t] >= 30 and r[t] > r[t - 1]                  # 진입 봉: RSI 30 위 상승 마감


def test_no_signal_without_divergence():
    """첫 하락은 들쭉날쭉(RSI 20대), 두 번째는 가파름(더 낮은 저점 + 더 낮은 RSI) → 다이버전스가 아니다."""
    seg = [np.full(60, 100.0) + np.tile([0.05, -0.05], 30)]
    seg.append(100 + np.cumsum(np.tile([-0.9, 0.5], 8)))
    seg.append(seg[-1][-1] + np.arange(1, 7) * 0.6)
    seg.append(seg[-1][-1] - np.arange(1, 10) * 1.2)
    seg.append(seg[-1][-1] + np.arange(1, 16) * 0.5)
    c = np.concatenate(seg)
    r = ind.rsi(c, 14)
    assert c[60:76].min() > c[82:91].min() and r[60:76].min() > r[82:91].min()   # 둘 다 낮아짐
    assert not (ind.rsi_divergence(c + 0.05, c - 0.05, c) == 1).any()


def test_bearish_is_mirror_of_bullish():
    h, l, c = _divergence_path()
    up = ind.rsi_divergence(h, l, c)
    dn = ind.rsi_divergence(200 - l, 200 - h, 200 - c)
    assert np.array_equal(np.nonzero(up == 1)[0], np.nonzero(dn == -1)[0])


def test_causal_future_bars_do_not_change_past_signals():
    rng = np.random.default_rng(3)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, 4000)))
    h, l = c * 1.001, c * 0.999
    full = np.nan_to_num(ind.rsi_divergence(h, l, c))
    assert (full != 0).sum() > 5
    for k in (1500, 2500, 3500):
        part = np.nan_to_num(ind.rsi_divergence(h[:k], l[:k], c[:k]))
        assert np.array_equal(part, full[:k])


def test_live_window_converges_to_full_history():
    """라이브는 최근 창만 받는다. 창이 window 의 5배(300봉)면 끝부분 신호가 전체 이력과 같아야 한다."""
    rng = np.random.default_rng(7)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, 20000)))
    h, l = c * 1.001, c * 0.999
    full = np.nan_to_num(ind.rsi_divergence(h, l, c))
    W = 300
    for end in range(2000, len(c), 499):
        sw = np.nan_to_num(ind.rsi_divergence(h[end - W:end], l[end - W:end], c[end - W:end]))
        assert np.array_equal(sw[-50:], full[end - 50:end])


def test_condition_resolves_rsi_div_and_warmup_covers_window():
    h, l, c = _divergence_path()
    n = len(c)
    cd = Candles(open_time=np.arange(n, dtype=np.int64) * 300_000, open=c, high=h, low=l, close=c,
                 volume=np.ones(n), timeframe_min=5)
    node = {"left": {"indicator": "RSI_DIV", "period": 14, "params": {"level": 30, "min_gap": 5, "window": 60}},
            "cmp": ">", "right": 0}
    res = C.SeriesResolver(cd)
    fired = [i for i in range(n) if C.evaluate(node, res, i)]
    assert fired == list(np.nonzero(ind.rsi_divergence(h, l, c) == 1)[0])
    assert C.required_warmup_bars(node) >= 180                # window 60 × 3


def test_example_preset_validates_against_schema():
    p = Preset.load(os.path.join(ROOT, "presets", "examples", "rsi-divergence-5m.json"))
    assert p.timeframe == "5m"
    assert p.data["exit"]["stopLoss"]["type"] == "swing"
    assert p.data["exit"]["takeProfit"] == {"type": "riskReward", "value": 2.5}


# ── CVD 확인(cvd 1 = RSI+CVD · 2 = CVD 만) ─────────────────────────────────────
def _flow(n, buy_between, a=72, b=99):
    """거래량 1 · 저점1~저점2 사이(a~b)만 시장가 매수 비중을 바꾼다(그 밖은 중립 0.5)."""
    tb = np.full(n, 0.5)
    tb[a:b] = 0.8 if buy_between else 0.2
    return np.ones(n), tb


def test_cvd_confirm_keeps_divergence_when_buyers_absorb():
    h, l, c = _divergence_path()
    v, tb = _flow(len(c), buy_between=True)
    base = np.nonzero(ind.rsi_divergence(h, l, c) == 1)[0]
    both = np.nonzero(ind.rsi_divergence(h, l, c, volume=v, taker_buy=tb, cvd_mode=1) == 1)[0]
    only = np.nonzero(ind.rsi_divergence(h, l, c, volume=v, taker_buy=tb, cvd_mode=2) == 1)[0]
    assert len(base) == 1 and np.array_equal(both, base) and np.array_equal(only, base)


def test_cvd_confirm_rejects_divergence_when_sellers_dominate():
    """가격·RSI 다이버전스는 그대로인데 두 저점 사이에 매도가 우세하면 cvd=1·2 는 신호를 내지 않는다."""
    h, l, c = _divergence_path()
    v, tb = _flow(len(c), buy_between=False)
    assert (ind.rsi_divergence(h, l, c) == 1).any()
    assert not (ind.rsi_divergence(h, l, c, volume=v, taker_buy=tb, cvd_mode=1) == 1).any()
    assert not (ind.rsi_divergence(h, l, c, volume=v, taker_buy=tb, cvd_mode=2) == 1).any()


def test_cvd_mode_without_orderflow_data_never_fires():
    """taker_buy 가 없는 캔들(오더플로우 미수집)에서 cvd 조건은 항상 false — TAKER_DELTA 와 같은 처리."""
    h, l, c = _divergence_path()
    n = len(c)
    cd = Candles(open_time=np.arange(n, dtype=np.int64) * 300_000, open=c, high=h, low=l, close=c,
                 volume=np.ones(n), timeframe_min=5)
    node = {"left": {"indicator": "RSI_DIV", "period": 14, "params": {"level": 30, "cvd": 1}}, "cmp": ">", "right": 0}
    res = C.SeriesResolver(cd)
    assert not any(C.evaluate(node, res, i) for i in range(n))


def test_bearish_cvd_is_mirrored():
    """약세는 CVD 도 뒤집어 본다 — 가격을 뒤집고 매수·매도를 맞바꾸면 같은 봉에 숏 신호."""
    h, l, c = _divergence_path()
    v, tb = _flow(len(c), buy_between=True)
    up = ind.rsi_divergence(h, l, c, volume=v, taker_buy=tb, cvd_mode=1)
    dn = ind.rsi_divergence(200 - l, 200 - h, 200 - c, volume=v, taker_buy=v - tb, cvd_mode=1)
    assert np.array_equal(np.nonzero(up == 1)[0], np.nonzero(dn == -1)[0])


def test_cvd_example_preset_validates():
    p = Preset.load(os.path.join(ROOT, "presets", "examples", "rsi-cvd-divergence-5m.json"))
    assert p.data["entry"]["left"]["params"]["cvd"] == 1
