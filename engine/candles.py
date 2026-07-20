"""캔들 컨테이너 + 1분봉 → 상위 타임프레임 리샘플링.

candle-collector가 저장하는 형식(1분봉, ms epoch)을 기준으로 한다.
docs/data-source.md 참조.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np

# 프리셋 타임프레임 → 분(minute)
TIMEFRAME_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440,
}

MINUTE_MS = 60_000


@dataclass
class Candles:
    """OHLCV 시계열. 모든 배열은 같은 길이, open_time 오름차순.

    open_time: 각 봉의 시작 시각 (ms epoch, UTC)
    """
    open_time: np.ndarray  # int64 ms
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    timeframe_min: int
    # 테이커 매수 체결량(base). 오더플로우 델타/CVD 지표용. 구버전 캐시엔 없어 None 허용
    # (None이면 델타/CVD 조건은 NaN → 항상 false, 나머지 지표엔 영향 없음).
    taker_buy: np.ndarray = None

    def __len__(self) -> int:
        return len(self.open_time)

    @classmethod
    def from_rows(cls, rows, timeframe_min: int = 1) -> "Candles":
        """rows: iterable of (open_time_ms, open, high, low, close, volume[, taker_buy]).

        (symbol, open_time) 중복 제거 + open_time 정렬을 수행한다.
        7번째 컬럼(taker_buy)이 있으면 함께 실어 온다.
        """
        arr = np.array(sorted(set(map(tuple, rows))), dtype=float)
        if arr.size == 0:
            raise ValueError("빈 캔들 데이터")
        # 중복 open_time 제거 (정렬 후 첫 값 유지)
        ot = arr[:, 0].astype(np.int64)
        _, keep = np.unique(ot, return_index=True)
        arr = arr[np.sort(keep)]
        return cls(
            open_time=arr[:, 0].astype(np.int64),
            open=arr[:, 1], high=arr[:, 2], low=arr[:, 3],
            close=arr[:, 4], volume=arr[:, 5],
            timeframe_min=timeframe_min,
            taker_buy=arr[:, 6] if arr.shape[1] > 6 else None,
        )

    def gap_report(self) -> list:
        """연속성 검사. 기대 간격보다 벌어진 구간(결측)을 리스트로 반환."""
        step = self.timeframe_min * MINUTE_MS
        diffs = np.diff(self.open_time)
        gaps = np.where(diffs != step)[0]
        return [(int(self.open_time[i]), int(self.open_time[i + 1]), int(diffs[i] // step))
                for i in gaps]


def resample(base: Candles, target_min: int) -> Candles:
    """1분봉(또는 하위 TF) → 상위 타임프레임 집계.

    상위 봉 경계는 epoch 기준 정렬(예: 15m 봉은 00/15/30/45분 시작).
    open=구간 첫 open, high=max, low=min, close=구간 마지막 close, volume=합.
    """
    if target_min == base.timeframe_min:
        return base
    if target_min % base.timeframe_min != 0:
        raise ValueError(f"{target_min}m는 베이스 {base.timeframe_min}m의 배수가 아님")

    bucket_ms = target_min * MINUTE_MS
    bucket = (base.open_time // bucket_ms) * bucket_ms  # 각 1분봉이 속한 상위봉 시작시각

    # 상위봉 경계가 바뀌는 지점
    edges = np.concatenate(([0], np.where(np.diff(bucket) != 0)[0] + 1, [len(base)]))
    n = len(edges) - 1

    has_tb = base.taker_buy is not None
    ot = np.empty(n, dtype=np.int64)
    o = np.empty(n); h = np.empty(n); l = np.empty(n); c = np.empty(n); v = np.empty(n)
    tb = np.empty(n) if has_tb else None
    for k in range(n):
        s, e = edges[k], edges[k + 1]
        ot[k] = bucket[s]
        o[k] = base.open[s]
        h[k] = base.high[s:e].max()
        l[k] = base.low[s:e].min()
        c[k] = base.close[e - 1]
        v[k] = base.volume[s:e].sum()
        if has_tb:
            tb[k] = base.taker_buy[s:e].sum()          # 테이커 매수량도 합산
    return Candles(ot, o, h, l, c, v, target_min, taker_buy=tb)


def signal_close_index(base: Candles, target_min: int):
    """베이스(1분봉) 인덱스 t마다, 그 t가 어떤 상위봉을 '닫는' 마지막 분이면
    그 상위봉 인덱스를 주는 매핑을 만든다.

    반환: (signal_bar_of, is_close)
      signal_bar_of[t] = t가 속한 상위봉 인덱스
      is_close[t]       = t가 그 상위봉의 마지막 1분봉인가 (신호 판정 시점)
    """
    bucket_ms = target_min * MINUTE_MS
    bucket = (base.open_time // bucket_ms) * bucket_ms
    uniq, inv = np.unique(bucket, return_inverse=True)
    signal_bar_of = inv  # 0..n-1
    # 각 상위봉의 마지막 1분봉 = 다음 원소의 bucket이 다르거나 마지막
    is_close = np.zeros(len(base), dtype=bool)
    is_close[-1] = True
    is_close[:-1] = np.diff(inv) != 0
    return signal_bar_of, is_close
