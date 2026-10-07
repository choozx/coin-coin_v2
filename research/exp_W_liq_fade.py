"""W — 청산 연쇄 역추세 (스캘핑, 15분봉 이하). 하네스: research/evt.py (판정 규칙도 거기 고정).

가설. 레버리지 강제청산이 몰리면 시장가 물량이 한쪽으로 쏟아져 가격이 '적정'보다 더 밀린다
(오버슈트). 쏟아짐이 끝나면 되돌아온다. 지표가 아니라 **강제 주문이라는 구조적 원인**이 있다는 게
지금까지 기각된 계열(SuperTrend·이평·VWAP — 가격에서 만든 지표)과 다른 점이다.

청산 데이터는 없으니 1분봉으로 흔적을 잡는다 — 세 가지가 **같은 봉에서 동시에**:
    ① 몸통 충격  z = (close/open − 1) / 직전 하루 봉수익률 표준편차   |z| ≥ k
    ② 거래량 폭증 v / 직전 하루 평균 거래량 ≥ vm
    ③ 체결 쏠림  하락 충격이면 taker 매수비율 ≤ 0.5 − d (매도가 시장가로 쏟아짐), 상승은 반대
진입: 그 봉 종가에 반대 방향(fade). 거울로 같은 방향(follow = 모멘텀 이어가기)도 같은 격자에 넣는다
— fade 가 지고 follow 가 이기면 그것도 답이다. 청산: h 봉 뒤 종가.

정직한 사전 확률: 낮음~중간. 큰 청산 뒤 되돌림은 잘 알려진 현상이라 이미 경쟁이 붙어 있을 것이고,
15분봉 이하 왕복 10bp 를 넘을 만큼 클지가 관건이다.

    python3 -u -m research.exp_W_liq_fade
"""
from __future__ import annotations

import itertools
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from research import evt   # noqa: E402

TFS = ("1m", "3m", "5m", "15m")
KS = (3.0, 4.0, 5.0)          # 몸통 충격 z
VMS = (3.0, 5.0)              # 거래량 배수
DS = (0.0, 0.1)               # 체결 쏠림(0 = 거는 조건 없음 — 사실상 ①②만)
MODES = ("fade", "follow")
HOLDS = (1, 3, 6, 12)         # 봉 수
GRID = list(itertools.product(TFS, KS, VMS, DS, MODES, HOLDS))

_F: dict = {}


def feats(b):
    key = evt.pin(b)
    if key not in _F:
        w = evt.day_bars_of(b)
        r = b.close / b.open - 1.0
        cc = np.concatenate(([0.0], b.close[1:] / b.close[:-1] - 1.0))
        z = r / evt.past_std(cc, w)
        vr = b.volume / evt.past_mean(b.volume, w)
        with np.errstate(invalid="ignore", divide="ignore"):
            tbr = np.where(b.volume > 0, b.taker_buy / b.volume, 0.5)
        _F[key] = (z, vr, tbr)
    return _F[key]


def signal(b, combo):
    tf, k, vm, d, mode, hold = combo
    z, vr, tbr = feats(b)
    with np.errstate(invalid="ignore"):
        down = (z <= -k) & (vr >= vm) & (tbr <= 0.5 - d)
        up = (z >= k) & (vr >= vm) & (tbr >= 0.5 + d)
    i_dn, i_up = np.nonzero(down)[0], np.nonzero(up)[0]
    s = 1.0 if mode == "fade" else -1.0          # fade: 하락 충격 → 롱
    idx = np.concatenate((i_dn, i_up))
    side = np.concatenate((np.full(len(i_dn), s), np.full(len(i_up), -s)))
    return idx, side


def label(c):
    tf, k, vm, d, mode, h = c
    return f"{tf:>3} z≥{k} 거래량×{vm} 쏠림{d} {mode:6} 보유{h}"


def main() -> int:
    evt.search("W · 청산 연쇄 역추세(fade)·모멘텀(follow)", GRID, signal, label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
