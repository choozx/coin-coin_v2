"""변동성 순위(ATR_RANK) — 직전 lookback 봉 NATR 대비 백분위. 네트워크 없음."""
from __future__ import annotations

import os
import sys

import numpy as np
import talib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import conditions as C                 # noqa: E402
from engine import indicators as ind               # noqa: E402


def _walk(n, seed=1):
    rng = np.random.default_rng(seed)
    vol = np.where(np.arange(n) % 500 < 100, 0.008, 0.002)          # 변동성이 주기적으로 커진다
    c = 100 * np.exp(np.cumsum(rng.normal(0, vol)))
    return c * (1 + vol), c * (1 - vol), c


def test_matches_brute_force():
    h, l, c = _walk(1500)
    lb = 200
    got = ind.atr_rank(h, l, c, 14, lb, chunk=97)                  # 청크 경계도 같이 검증
    natr = talib.NATR(h, l, c, timeperiod=14)
    for t in range(lb + 14, 1500, 37):
        past = natr[t - lb:t]
        assert np.isclose(got[t], (past < natr[t]).mean() * 100)
    assert np.isnan(got[:lb]).all()


def test_causal():
    h, l, c = _walk(3000)
    full = ind.atr_rank(h, l, c, 14, 300)
    part = ind.atr_rank(h[:2000], l[:2000], c[:2000], 14, 300)
    assert np.allclose(full[:2000], part, equal_nan=True)


def test_high_vol_bursts_rank_high():
    h, l, c = _walk(3000)
    r = ind.atr_rank(h, l, c, 14, 400)
    burst = (np.arange(3000) % 500 >= 30) & (np.arange(3000) % 500 < 100) & ~np.isnan(r)
    calm = (np.arange(3000) % 500 >= 200) & ~np.isnan(r)
    assert np.nanmedian(r[burst]) > 80 > np.nanmedian(r[calm])


def test_warmup_covers_lookback():
    node = {"left": {"indicator": "ATR_RANK", "period": 14, "params": {"lookback": 2016}}, "cmp": ">=", "right": 80}
    assert C.required_warmup_bars(node) >= 2016
