"""BB — 볼린저밴드 스캘핑(5m·15m): 밴드 이탈 역추세(fade) · 돌파 추종(follow) · 스퀴즈 뒤만. 하네스: research/evt.py

요청(2026-10-14): SuperTrend+RSI 필터(SC 기각) 뒤 "그럼 볼린저밴드는?" — BB 를 진입 신호로 직접 본 적이 없다.
가까운 것: U(세션 VWAP σ 밴드 역추세) ❌ · Y1(수축 돌파 — Donchian) ❌.

신호(신호봉 종가): 종가가 BB(n, k) 상단 밖으로 **처음** 나간 봉 / 하단 밖으로 처음 나간 봉.
  fade   상단 이탈 숏 · 하단 이탈 롱 (평균 회귀)
  follow 상단 이탈 롱 · 하단 이탈 숏 (돌파)
  squeeze 게이트: 직전 봉의 밴드 폭(상단−하단)/중심 이 직전 하루 폭 분포의 하위 20% 일 때만(없음/있음).
격자: TF 5m·15m × n 20·50 × k 2·2.5·3 × fade/follow × squeeze 없음/있음 × 보유 3·6·12·24봉 = 192.
판정(evt.search 내장, 결과 전 고정): 구간1·2 양수·거래 ≥ 30 · 거래당 gross ≥ 12bp · 시간 귀무 + 방향 귀무 best-of-192 p95 초과
  → min(구간1,2) 최대 하나 → 홀드아웃 1회. gross 먼저, net 따로.

    python3 -u -m research.exp_BB_bollinger
"""
from __future__ import annotations

import itertools
import sys

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import indicators as ind   # noqa: E402
from research import evt               # noqa: E402

TFS = ("5m", "15m")
NS = (20, 50)
KS = (2.0, 2.5, 3.0)
MODES = ("fade", "follow")
SQZ = (False, True)
HOLDS = (3, 6, 12, 24)
GRID = list(itertools.product(TFS, NS, KS, MODES, SQZ, HOLDS))
MIN_BP = 12.0

_F: dict = {}


def bands(b, n, k):
    key = (evt.pin(b), n, k)
    if key not in _F:
        up, mid, lo = ind.bollinger(b.close, n, k)
        width = (up - lo) / mid
        d = evt.day_bars_of(b)
        prev = np.concatenate(([np.nan], width[:-1]))           # 직전 봉 폭
        q = np.full(len(width), np.nan)
        w0 = np.nan_to_num(width, nan=np.inf)
        if len(w0) > d + 1:
            win = swv(w0[:-1], d)                                 # 봉 t 의 창 = [t-d, t-1]
            q[d:] = np.percentile(np.where(np.isinf(win), np.nan, win), 20, axis=1)
        _F[key] = (up, lo, prev <= q)
    return _F[key]


def signal(b, combo):
    tf, n, k, mode, sqz, hold = combo
    up, lo, tight = bands(b, n, k)
    c = b.close
    pc = np.concatenate(([np.nan], c[:-1]))
    pu = np.concatenate(([np.nan], up[:-1]))
    pl = np.concatenate(([np.nan], lo[:-1]))
    with np.errstate(invalid="ignore"):
        out_up = (c > up) & ~(pc > pu)
        out_dn = (c < lo) & ~(pc < pl)
        if sqz:
            out_up &= tight
            out_dn &= tight
    iu, idn = np.nonzero(out_up)[0], np.nonzero(out_dn)[0]
    s = -1.0 if mode == "fade" else 1.0
    return (np.concatenate((iu, idn)),
            np.concatenate((np.full(len(iu), s), np.full(len(idn), -s))))


def label(c):
    tf, n, k, mode, sqz, h = c
    return f"{tf:>3} BB({n},{k}) {mode:6} {'스퀴즈' if sqz else '      '} 보유{h}"


if __name__ == "__main__":
    evt.search("BB · 볼린저밴드 이탈", GRID, signal, label, min_bp=MIN_BP)
