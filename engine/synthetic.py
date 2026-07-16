"""합성 1분봉 생성기 (실데이터 없을 때 엔진 검증용).

기하 브라운 운동 기반 랜덤워크로 OHLCV를 만든다. seed 고정으로 재현 가능.
"""
from __future__ import annotations

from datetime import datetime, timezone
import numpy as np

from .candles import Candles, MINUTE_MS


def generate(n_minutes: int = 60 * 24 * 30, start: str = "2024-01-01",
             price0: float = 40_000.0, vol_per_min: float = 0.0008,
             drift_per_min: float = 0.0, seed: int = 42) -> Candles:
    """n_minutes개의 1분봉 생성.

    vol_per_min: 분당 로그수익률 표준편차 (0.0008 ≈ 연 60%대 변동성 근사)
    """
    rng = np.random.default_rng(seed)
    start_ms = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp() * 1000)
    open_time = start_ms + np.arange(n_minutes, dtype=np.int64) * MINUTE_MS

    # 종가 랜덤워크
    rets = rng.normal(drift_per_min, vol_per_min, n_minutes)
    close = price0 * np.exp(np.cumsum(rets))
    open_ = np.empty(n_minutes)
    open_[0] = price0
    open_[1:] = close[:-1]

    # 봉 내 고저: 각 봉에 추가 노이즈
    wick = np.abs(rng.normal(0, vol_per_min, n_minutes)) * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    volume = rng.uniform(5, 50, n_minutes)

    return Candles(open_time, open_, high, low, close, volume, timeframe_min=1)
