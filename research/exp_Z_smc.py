"""Z — 유튜브 SMC/ICT 계열: 유동성 스윕 · FVG 되돌림 (스캘핑, 15분봉 이하). 하네스: research/evt.py

가설. 직전 고점·저점 너머엔 손절 주문이 몰려 있고, 큰손이 그걸 '쓸고'(스윕) 되돌린다.
그리고 세 봉 사이에 생긴 가격 공백(FVG)은 나중에 메워지면서 지지·저항이 된다.
유튜브에서 가장 많이 도는 스캘핑 계열이라, 대본 없이도 표준 정의로 기계화해 둔다.

Z1 · 스윕
    기준선 = 직전 UTC 일 고저(PDH/PDL) 또는 직전 4시간 블록 고저.
    봉 고가가 기준 고점을 넘었는데 종가는 그 아래로 돌아오면 → 숏(fade = 스윕 역추세, SMC 정석).
    저점도 대칭. 기준선 하나당 첫 스윕만(같은 날 반복 진입 막기). 거울(follow)도 같이 본다.
Z2 · FVG 되돌림
    상승 FVG: low[i] > high[i−2] (공백 = high[i−2] ~ low[i]). 이후 E 봉 안에 저가가 공백 윗변
    low[i] 에 닿으면 그 봉 종가에 롱(fade = 공백이 지지한다는 SMC 정석). 하락 FVG 대칭.
    공백 크기 하한 = 직전 하루 봉 수익률 표준편차 × g. 공백 하나당 한 번.
    ※ 실전은 공백 가장자리 지정가지만 여기선 닿은 봉 종가 진입(회계 통일).

정직한 사전 확률: 낮음. 스윕은 '돌파 실패에 거는 평균회귀', FVG 는 '되돌림 매수'라 이미 기각된
평균회귀 계열(B·U)과 정보가 크게 겹친다. 기준선이 지표가 아니라 '가격 수준'이라는 점만 새롭다.

    python3 -u -m research.exp_Z_smc
"""
from __future__ import annotations

import itertools
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from research import evt   # noqa: E402

MODES = ("fade", "follow")

# ── Z1 ──
GRID1 = list(itertools.product(("5m", "15m"), ("day", "4h"), MODES, (3, 6, 12, 24)))
_F: dict = {}


def levels(b, kind):
    """봉마다 '직전 블록' 고저. 블록 = UTC 일 또는 4시간."""
    key = ("lv", id(b), kind)
    if key not in _F:
        blk_min = 1440 if kind == "day" else 240
        blk = b.open_time // (blk_min * 60_000)
        u, inv = np.unique(blk, return_inverse=True)
        hi = np.full(len(u), -np.inf)
        lo = np.full(len(u), np.inf)
        np.maximum.at(hi, inv, b.high)
        np.minimum.at(lo, inv, b.low)
        prev_ok = np.concatenate(([False], np.diff(u) == 1))      # 바로 앞 블록이 있어야
        ph = np.where(prev_ok, np.concatenate(([np.nan], hi[:-1])), np.nan)
        pl = np.where(prev_ok, np.concatenate(([np.nan], lo[:-1])), np.nan)
        _F[key] = (ph[inv], pl[inv], inv)
    return _F[key]


def first_per_block(mask, blk):
    i = np.nonzero(mask)[0]
    if not len(i):
        return i
    _, first = np.unique(blk[i], return_index=True)
    return i[first]


def signal1(b, combo):
    tf, kind, mode, _ = combo
    ph, pl, blk = levels(b, kind)
    with np.errstate(invalid="ignore"):
        sw_hi = (b.high > ph) & (b.close < ph)          # 고점 스윕 후 안으로 마감
        sw_lo = (b.low < pl) & (b.close > pl)
    i_hi, i_lo = first_per_block(sw_hi, blk), first_per_block(sw_lo, blk)
    s = 1.0 if mode == "fade" else -1.0
    return (np.concatenate((i_hi, i_lo)),
            np.concatenate((np.full(len(i_hi), -s), np.full(len(i_lo), s))))


def label1(c):
    tf, kind, mode, h = c
    return f"스윕 {tf:>3} {'전일' if kind == 'day' else '직전4h'} {mode:6} 보유{h}"


# ── Z2 ──
GRID2 = list(itertools.product(("3m", "5m", "15m"), (0.0, 1.0), MODES, (3, 6, 12)))
EXPIRY = 24


def fvg(b, g):
    key = ("fvg", id(b), g)
    if key not in _F:
        cc = np.concatenate(([0.0], b.close[1:] / b.close[:-1] - 1.0))
        sd = evt.past_std(cc, evt.day_bars_of(b)) * b.close
        n = len(b.close)
        idx, side = [], []
        h, l_ = b.high, b.low
        with np.errstate(invalid="ignore"):
            bull = np.nonzero((l_[2:] - h[:-2]) > g * sd[2:])[0] + 2
            bear = np.nonzero((l_[:-2] - h[2:]) > g * sd[2:])[0] + 2
        for i in bull:                                  # 공백 윗변 = low[i], 되돌아와 닿으면 롱
            top = l_[i]
            w = l_[i + 1:min(n, i + 1 + EXPIRY)]
            t = np.nonzero(w <= top)[0]
            if len(t):
                idx.append(i + 1 + t[0]); side.append(1.0)
        for i in bear:                                  # 공백 아랫변 = high[i], 닿으면 숏
            bot = h[i]
            w = h[i + 1:min(n, i + 1 + EXPIRY)]
            t = np.nonzero(w >= bot)[0]
            if len(t):
                idx.append(i + 1 + t[0]); side.append(-1.0)
        _F[key] = (np.asarray(idx, dtype=np.int64), np.asarray(side))
    return _F[key]


def signal2(b, combo):
    tf, g, mode, _ = combo
    idx, side = fvg(b, g)
    return idx, side * (1.0 if mode == "fade" else -1.0)


def label2(c):
    tf, g, mode, h = c
    return f"FVG {tf:>3} 크기≥{g}σ {mode:6} 보유{h}"


def main() -> int:
    evt.search("Z1 · 유동성 스윕(전일·직전 4h 고저)", GRID1, signal1, label1)
    print("\n" + "═" * 70 + "\n")
    evt.search("Z2 · FVG 되돌림", GRID2, signal2, label2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
