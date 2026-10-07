"""Y — 변동성 수축 후 돌파 · 세션 개장 범위 돌파(ORB) (스캘핑, 15분봉 이하). 하네스: research/evt.py

가설. 변동성은 뭉친다 — 좁게 눌린 뒤 터진 방향으로 한동안 간다. SuperTrend 가 휩쏘에 죽은 건
'아무 때나' 뒤집힘을 잡았기 때문이고, 수축 다음의 돌파만 골라 들어가면 다를 수 있다.
그리고 세션 개장(UTC 0시·런던·뉴욕) 직후 범위를 깨는 방향은 그날 주문 흐름의 방향이다.

Y1 · 수축 돌파 (Donchian + 수축 게이트)
    폭 = 직전 L 봉 (최고가 − 최저가) / 종가.  수축 = 폭 / 직전 하루 평균 폭 ≤ c  (c=∞ 면 게이트 없음)
    수축 상태에서 종가가 직전 L 봉 최고가를 넘으면 롱, 최저가를 깨면 숏(follow).
    거울로 fade(돌파 실패에 거는 쪽)도 같은 격자에 넣는다.
Y2 · ORB
    세션 시작 S 부터 R 분의 고저 = 개장 범위. 그 뒤 4시간 안에 5분봉 종가가 처음 범위를 벗어나면
    그 방향(follow) 또는 반대(fade). 하루 세션당 한 번.
    S ∈ 00:00(UTC 일봉) · 07:00(런던) · 13:30·14:30(뉴욕 — 서머타임 따라 둘 다)

정직한 사전 확률: 낮음. 돌파 진입도 추세추종 계열이라 SuperTrend 의 가짜 신호 문제를 물려받을 수
있다. 진입 조건이 다르니 확인은 한다.

    python3 -u -m research.exp_Y_breakout
"""
from __future__ import annotations

import itertools
import sys

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from research import evt   # noqa: E402

# ── Y1 ──
TFS = ("3m", "5m", "15m")
LS = (12, 24, 48)
CS = (0.5, 0.7, float("inf"))
MODES = ("follow", "fade")
HOLDS = (3, 6, 12, 24)
GRID1 = list(itertools.product(TFS, LS, CS, MODES, HOLDS))

_F: dict = {}


def donchian(b, L):
    key = (evt.pin(b), L)
    if key not in _F:
        n = len(b.close)
        hh = np.full(n, np.nan)
        ll = np.full(n, np.nan)
        hh[L:] = swv(b.high, L).max(axis=1)[:-1]      # 직전 L 봉(현재 봉 제외)
        ll[L:] = swv(b.low, L).min(axis=1)[:-1]
        width = (hh - ll) / b.close
        rel = width / evt.past_mean(np.nan_to_num(width, nan=0.0), evt.day_bars_of(b))
        rel[: L + evt.day_bars_of(b)] = np.nan
        _F[key] = (hh, ll, rel)
    return _F[key]


def signal1(b, combo):
    tf, L, c, mode, hold = combo
    hh, ll, rel = donchian(b, L)
    with np.errstate(invalid="ignore"):
        gate = rel <= c
        up = gate & (b.close > hh)
        dn = gate & (b.close < ll)
    i_up, i_dn = np.nonzero(up)[0], np.nonzero(dn)[0]
    s = 1.0 if mode == "follow" else -1.0
    return (np.concatenate((i_up, i_dn)),
            np.concatenate((np.full(len(i_up), s), np.full(len(i_dn), -s))))


def label1(c):
    tf, L, cc, mode, h = c
    return f"{tf:>3} L{L} 수축≤{'∞' if cc == float('inf') else cc} {mode:6} 보유{h}"


# ── Y2 ──
SESS = (0, 7 * 60, 13 * 60 + 30, 14 * 60 + 30)       # UTC 분
RS = (15, 30, 60)
HOLDS2 = (3, 6, 12, 24)                               # 5m 봉 = 15분 · 30분 · 1시간 · 2시간
GRID2 = list(itertools.product(("5m",), SESS, RS, MODES, HOLDS2))
WINDOW = 48                                           # 개장 범위 뒤 4시간(5m × 48) 안에서만


def signal2(b, combo):
    _, S, R, mode, _ = combo
    key = ("orb", evt.pin(b), S, R)
    if key not in _F:
        mins = b.open_time // 60_000
        tod = mins % 1440
        idx, side = [], []
        starts = np.nonzero(tod == S)[0]
        nr = R // b.timeframe_min
        for st in starts:
            e = st + nr
            if e + WINDOW >= len(b.close):
                continue
            if b.open_time[e - 1] - b.open_time[st] != (nr - 1) * b.timeframe_min * 60_000:
                continue                                    # 구멍 있는 날은 건너뛴다
            hi, lo = b.high[st:e].max(), b.low[st:e].min()
            w = b.close[e:e + WINDOW]
            brk = np.nonzero((w > hi) | (w < lo))[0]
            if len(brk):
                j = e + brk[0]
                idx.append(j)
                side.append(1.0 if b.close[j] > hi else -1.0)
        _F[key] = (np.asarray(idx, dtype=np.int64), np.asarray(side))
    idx, side = _F[key]
    return idx, side * (1.0 if mode == "follow" else -1.0)


def label2(c):
    _, S, R, mode, h = c
    return f"ORB {S // 60:02d}:{S % 60:02d} 범위{R}분 {mode:6} 보유{h * 5}분"


def main() -> int:
    evt.search("Y1 · 변동성 수축 후 돌파", GRID1, signal1, label1)
    print("\n" + "═" * 70 + "\n")
    evt.search("Y2 · 세션 개장 범위 돌파(ORB)", GRID2, signal2, label2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
