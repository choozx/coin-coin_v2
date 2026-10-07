"""X — 시간대 효과 (스캘핑, 15분봉). 하네스: research/evt.py (판정 규칙도 거기 고정).

가설. BTC 에는 하루 중 특정 시각에 방향 편향이 있다 — 일봉 마감(UTC 0시) 전후 리밸런싱,
미국장 개장(13:30 UTC)·마감, 아시아 오전. 지표 없이 **시계만** 쓴다. 가격에서 만든 지표를 하나도
안 쓰는 첫 가설이라, 지금까지의 기각(지표 신호)과 겹치지 않는다.

규칙: 매일 같은 시각 HH:MM(UTC) 에 진입(그 시각에 끝나는 15분봉 종가) → h 봉 보유 → 청산.
격자 = 96 시각 × 롱/숏 × 보유(15분·30분·1시간) = 576. 하루 한 번이라 구간당 거래 ~850.
576 개 중 고르는 것이라 best-of-576 귀무로 다중검정을 같이 본다(evt.search).

정직한 사전 확률: 신호는 있을 수 있음, 비용 통과는 낮음. 알려진 시간대 효과는 거래당 몇 bp 라
왕복 10bp 를 넘기 어렵다. gross 통과·net 실패가 가장 그럴듯한 결과다.

    python3 -u -m research.exp_X_time_of_day
"""
from __future__ import annotations

import itertools
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from research import evt   # noqa: E402

TF = "15m"
SLOTS = range(96)             # 진입 시각 = 슬롯 × 15분 (UTC)
SIDES = ("long", "short")
HOLDS = (1, 2, 4)
GRID = [(TF, s, side, h) for s, side, h in itertools.product(SLOTS, SIDES, HOLDS)]

_SLOT: dict = {}


def end_slot(b):
    """봉 i 가 '끝나는' 시각의 슬롯(= 진입 시각). 캐시."""
    if evt.pin(b) not in _SLOT:
        _SLOT[evt.pin(b)] = ((b.open_time // 60_000 + b.timeframe_min) % 1440) // 15
    return _SLOT[evt.pin(b)]


def signal(b, combo):
    _, slot, side, _ = combo
    idx = np.nonzero(end_slot(b) == slot)[0]
    return idx, np.full(len(idx), 1.0 if side == "long" else -1.0)


def label(c):
    _, s, side, h = c
    return f"{s // 4:02d}:{s % 4 * 15:02d} UTC {side:5} 보유{h * 15}분"


def main() -> int:
    evt.search("X · 시간대 효과(시각만)", GRID, signal, label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
