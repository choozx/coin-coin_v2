"""R — 약한 신호 조합 (스캘핑, 3·5분봉). 하네스: research/evt.py (판정 규칙도 거기 고정).

배경. W·X·Y·Z 에서 gross 로 보인 효과는 전부 거래당 0.2~8bp — 혼자선 taker 왕복 10bp 를 못 넘는다.
같은 계열 지표를 겹치는 건 같은 정보 반복이라 소용없지만(K 의 조합 프리셋이 그 사례), **출처가 다른**
약한 신호가 겹치면 더해질 수 있다. 사용자 요청 "스캘핑 지표 몇 개 추려 조합·최적화" (2026-10-06).

재료(전부 앞 실험에서 근거가 있는 것만):
  트리거(하나) — 초단기 되돌림 계열
    don  직전 12봉 고저 돌파를 거꾸로(Y1 최선)
    fvg  FVG 되돌림 닿음에 공백 방향(Z2 최선)
    shk  몸통 충격 |z|≥3 + 거래량 ×3 을 거꾸로(W)
  필터(켜고 끄기)
    시간  off · 창(19:30~22:30 UTC 만) · 창+롱만   (X: 20~22 UTC 롱 +2~5bp)
    추세  off · 1시간봉 EMA50 방향과 같은 쪽만     (상위 시간봉 방향 — 새 재료)
    거래량 off · 트리거봉 거래량 ≥ 직전 하루 평균 ×2
  보유  2·4·8 봉

격자 3×2(TF)×3×2×2×3 = 216 — 결과 보기 전에 고정.

판정(사전 고정): evt.search 규칙 + **거래당 gross ≥ 12bp (두 구간 모두, 홀드아웃도)**.
12bp = taker 왕복 10bp + 여유 2bp. 이게 없으면 Y1·Z2 처럼 '유의하지만 돈은 안 되는' 게 통과한다.

정직한 사전 확률: 낮음. 그래도 통과하면 처음으로 '비용을 넘는 스캘핑 신호'다.

    python3 -u -m research.exp_R_combo
"""
from __future__ import annotations

import itertools
import sys

import numpy as np
import talib

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine.candles import resample                   # noqa: E402
from research import evt                              # noqa: E402
from research import exp_W_liq_fade as W              # noqa: E402
from research import exp_Y_breakout as Y              # noqa: E402
from research import exp_Z_smc as Z                   # noqa: E402

MIN_BP = 12.0
TRIGS = ("don", "fvg", "shk")
TFS = ("3m", "5m")
TIMES = ("off", "win", "win_long")
TRENDS = ("off", "with")
VOLS = (False, True)
HOLDS = (2, 4, 8)
GRID = [(tf, tr, tm, td, vo, h)
        for tr, tf, tm, td, vo, h in itertools.product(TRIGS, TFS, TIMES, TRENDS, VOLS, HOLDS)]

WIN = (19 * 60 + 30, 22 * 60 + 30)    # UTC 분
_C: dict = {}


def trigger(b, tf, tr, hold):
    if tr == "don":
        return Y.signal1(b, (tf, 12, float("inf"), "fade", hold))
    if tr == "fvg":
        return Z.signal2(b, (tf, 0.0, "fade", hold))
    return W.signal(b, (tf, 3.0, 3.0, 0.0, "fade", hold))


def ctx(b):
    """봉마다: 진입 시각(UTC 분) · 1h 추세(+1/−1, 직전 '완성된' 1시간봉 기준) · 거래량 배수."""
    if evt.pin(b) not in _C:
        tod = ((b.open_time // 60_000) + b.timeframe_min) % 1440
        h1 = resample(b, 60)
        ema = talib.EMA(h1.close, 50)
        up = np.where(np.isnan(ema), 0.0, np.sign(h1.close - ema))
        # 봉 i 의 진입 시각(봉 끝) 기준으로 이미 닫힌 1시간봉 중 마지막
        end = b.open_time + b.timeframe_min * 60_000
        k = np.searchsorted(h1.open_time + 3_600_000, end, side="right") - 1
        trend = np.where(k >= 0, up[np.clip(k, 0, None)], 0.0)
        vr = b.volume / evt.past_mean(b.volume, evt.day_bars_of(b))
        _C[evt.pin(b)] = (tod, trend, vr)
    return _C[evt.pin(b)]


def signal(b, combo):
    tf, tr, tm, td, vo, hold = combo
    idx, side = trigger(b, tf, tr, hold)
    idx = np.asarray(idx, dtype=np.int64)
    side = np.asarray(side, dtype=np.float64)
    tod, trend, vr = ctx(b)
    keep = np.ones(len(idx), dtype=bool)
    if tm != "off":
        t = tod[idx]
        keep &= (t >= WIN[0]) & (t < WIN[1])
        if tm == "win_long":
            keep &= side > 0
    if td == "with":
        keep &= trend[idx] == side
    if vo:
        with np.errstate(invalid="ignore"):
            keep &= vr[idx] >= 2.0
    return idx[keep], side[keep]


def label(c):
    tf, tr, tm, td, vo, h = c
    return f"{tr} {tf:>3} 시간:{tm:8} 추세:{td:4} 거래량:{'×2' if vo else '-':2} 보유{h}"


def main() -> int:
    evt.search("R · 약한 신호 조합(트리거 × 시간·추세·거래량 필터)", GRID, signal, label, min_bp=MIN_BP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
