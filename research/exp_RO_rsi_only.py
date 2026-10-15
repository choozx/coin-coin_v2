"""RO — RSI 하나만으로 반대매매(과매도 롱 · 과매수 숏). 하네스: research/evt.py

요청(2026-10-14): "rsi 로만 해서 반대매매로만 했을 때는 어떤 거 같아?"
앞선 B 는 BTC 15m 에 ADX 게이트·ATR 손절을 섞었고, G 는 그 최선 변형을 TF 만 바꿨다. 필터 없는 순수 RSI 역추세를
5m~4h 전체로 한 번에 본 적은 없어서 여기서 닫는다.

신호: RSI(p) 가 하단 θ 아래로 **내려가는 봉**에서 롱, 상단 100−θ 위로 올라가는 봉에서 숏(구역 진입 순간 한 번).
      진입 = 신호봉 종가, 청산 = h 봉 뒤 종가(고정 보유). 방향 = 양쪽 / 롱만 / 숏만.
격자: TF 5m·15m·1h·4h × p 7·14 × θ 20·25·30 × 방향 3 × 보유 3·6·12·24 = 288.
판정(사전 고정, evt.search 그대로): 구간1·2 양수·귀무 best-of-288 p95 초과(롱숏이면 방향 귀무도) →
      min(구간1,2) 최대 하나 → 홀드아웃 1회. gross 먼저. 경제적 문턱 거래당 12bp(R 과 같다 — taker 왕복 10bp + 여유).

정직한 사전 확률: 낮다. B(15m 0/6)·G(1h·4h 귀무 미달)와 Y·Z 의 fade 가 거래당 0.2~0.5bp 였다.

    python3 -u -m research.exp_RO_rsi_only
"""
from __future__ import annotations

import itertools
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import indicators as ind   # noqa: E402
from research import evt               # noqa: E402

TFS = ("5m", "15m", "1h", "4h")
PERIODS = (7, 14)
THETAS = (20, 25, 30)
SIDES = ("both", "long", "short")
HOLDS = (3, 6, 12, 24)
GRID = list(itertools.product(TFS, PERIODS, THETAS, SIDES, HOLDS))
MIN_BP = 12.0

_R: dict = {}


def rsi_of(b, p):
    key = (evt.pin(b), p)
    if key not in _R:
        _R[key] = ind.rsi(b.close, p)
    return _R[key]


def signal(b, combo):
    tf, p, th, side, hold = combo
    r = rsi_of(b, p)
    prev = np.concatenate(([np.nan], r[:-1]))
    with np.errstate(invalid="ignore"):
        lo = (r < th) & (prev >= th)
        hi = (r > 100 - th) & (prev <= 100 - th)
    i_lo = np.nonzero(lo)[0] if side in ("both", "long") else np.array([], int)
    i_hi = np.nonzero(hi)[0] if side in ("both", "short") else np.array([], int)
    return (np.concatenate((i_lo, i_hi)),
            np.concatenate((np.ones(len(i_lo)), -np.ones(len(i_hi)))))


def label(c):
    tf, p, th, side, h = c
    return f"{tf:>3} RSI{p:<2} {th}/{100 - th} {side:5} 보유{h}"


if __name__ == "__main__":
    evt.search("RO · RSI 단독 반대매매", GRID, signal, label, min_bp=MIN_BP)
