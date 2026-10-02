"""session_vwap_z — 세션(UTC 일) VWAP σ 이탈.

누적 VWAP 는 데이터 창의 시작점에 따라 값이 바뀌어 백테스트와 라이브(슬라이딩 창)가 어긋난다.
세션 리셋 버전은 창을 어디서 자르든 같은 날의 값이 같아야 한다 — 그게 이 지표를 쓰는 이유다.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.indicators import session_vwap_z     # noqa: E402

DAY = 86_400_000
M = 60_000


def _series(n=3 * 1440, seed=0):
    rng = np.random.default_rng(seed)
    ot = np.arange(n, dtype=np.int64) * M
    c = 100 + np.cumsum(rng.normal(0, 0.1, n))
    h, l = c + rng.random(n) * 0.2, c - rng.random(n) * 0.2
    v = rng.random(n) * 10 + 1
    return ot, h, l, c, v


def test_window_start_does_not_change_values():
    ot, h, l, c, v = _series()
    full = session_vwap_z(ot, h, l, c, v)
    cut = 1440 + 700                                   # 둘째 날 한가운데부터 자른 창
    part = session_vwap_z(ot[cut:], h[cut:], l[cut:], c[cut:], v[cut:])
    day3 = slice(2 * 1440 - cut, None)                 # 셋째 날은 두 창 모두 온전하다
    np.testing.assert_allclose(part[day3], full[2 * 1440:], rtol=1e-9, equal_nan=True)


def test_resets_each_session_and_sign():
    ot = np.arange(4, dtype=np.int64) * M + np.array([0, 0, DAY, DAY])
    c = np.array([100.0, 102.0, 50.0, 48.0])
    z = session_vwap_z(ot, c, c, c, np.ones(4), warmup_ms=0)
    assert np.isnan(z[0]) and np.isnan(z[2])           # 세션 첫 봉은 분산 0 → NaN
    assert z[1] > 0 and z[3] < 0                       # 새 세션은 전날과 무관하게 다시 잰다
    np.testing.assert_allclose([z[1], z[3]], [1.0, -1.0])


def test_warmup_blanks_session_start():
    """세션 첫 1시간은 σ 가 0 근처라 z 가 폭발한다 → 값을 내지 않는다."""
    ot, h, l, c, v = _series()
    z = session_vwap_z(ot, h, l, c, v)
    minute_of_day = (ot % DAY) // M
    assert np.isnan(z[minute_of_day < 60]).all()
    assert np.isfinite(z[minute_of_day >= 60]).mean() > 0.99
    assert np.nanmax(np.abs(z)) < 20                   # 폭발 값이 남지 않는다
