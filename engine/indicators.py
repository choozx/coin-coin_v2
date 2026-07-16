"""기술적 지표 — TA-Lib(검증된 C 구현) 위임 + numpy 보조.

설계:
- 함수 시그니처는 그대로 유지 → conditions.py / backtest.py 변경 없음.
- 계산은 TA-Lib(업계 표준, 20년+ 검증)에 위임 → 직접 구현의 조용한 버그 제거.
  (예전 numpy 스토캐스틱은 %K가 전부 NaN이었음 — cumsum 평활이 워밍업 NaN에 오염.)
- TA-Lib에 없는 것만 numpy로 유지: VWAP, RVOL(상대 거래량).
- TA-Lib은 float64 입력을 요구하고 워밍업 구간을 NaN으로 채움(우리 관례와 동일 →
  conditions.py가 NaN을 false로 처리).

주의: EMA·RSI 등 재귀형 지표는 TA-Lib의 'unstable period' 특성상 초반 값이 먹인
데이터 길이에 따라 미세하게 달라질 수 있음(버그 아님, 시딩 관례). 슬라이스(IS/OOS)
백테스트에서 경계 구간이 아주 조금 다를 수 있으나 이는 직접 구현도 마찬가지.
"""
from __future__ import annotations

import numpy as np
import talib


def _d(x) -> np.ndarray:
    """TA-Lib 입력용 float64 연속 배열."""
    return np.ascontiguousarray(x, dtype=np.float64)


def sma(x: np.ndarray, period: int) -> np.ndarray:
    return talib.SMA(_d(x), timeperiod=period)


def ema(x: np.ndarray, period: int) -> np.ndarray:
    return talib.EMA(_d(x), timeperiod=period)


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    return talib.RSI(_d(close), timeperiod=period)


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    """반환: (macd_line, signal_line, hist)"""
    macd_line, sig, hist = talib.MACD(_d(close), fastperiod=fast, slowperiod=slow, signalperiod=signal)
    return macd_line, sig, hist


def bollinger(close: np.ndarray, period: int = 20, stddev: float = 2.0):
    """반환: (upper, mid, lower)"""
    upper, mid, lower = talib.BBANDS(_d(close), timeperiod=period,
                                     nbdevup=stddev, nbdevdn=stddev, matype=0)
    return upper, mid, lower


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    return talib.ATR(_d(high), _d(low), _d(close), timeperiod=period)


def stochastic(high, low, close, period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    """반환: (%K, %D). slowk = SMA(rawK, smooth_k), slowd = SMA(slowk, smooth_d)."""
    k, d = talib.STOCH(_d(high), _d(low), _d(close),
                       fastk_period=period, slowk_period=smooth_k, slowk_matype=0,
                       slowd_period=smooth_d, slowd_matype=0)
    return k, d


def stoch_rsi(close, rsi_period: int = 14, stoch_period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    """Stochastic RSI — RSI 값에 스토캐스틱 적용. 반환 (%K, %D), 0~100.

    TA-Lib STOCHRSI 정의를 따름: %K=raw fast %K(stoch_period 룩백), %D=SMA(%K, smooth_d).
    (smooth_k는 TA-Lib STOCHRSI에서 %K를 추가 평활하지 않으므로 미사용 — 표준 정의.)
    """
    k, d = talib.STOCHRSI(_d(close), timeperiod=rsi_period,
                          fastk_period=stoch_period, fastd_period=smooth_d, fastd_matype=0)
    return k, d


def cci(high, low, close, period: int = 20) -> np.ndarray:
    """Commodity Channel Index. 0 중심: +100 초과=과매수, -100 미만=과매도."""
    return talib.CCI(_d(high), _d(low), _d(close), timeperiod=period)


def mfi(high, low, close, volume, period: int = 14) -> np.ndarray:
    """Money Flow Index (거래량 가중 RSI), 0~100."""
    return talib.MFI(_d(high), _d(low), _d(close), _d(volume), timeperiod=period)


# ---- TA-Lib에 없는 지표 (numpy 직접) ----
def rvol(volume, period: int = 20) -> np.ndarray:
    """상대 거래량 = 현재 거래량 ÷ 최근 period 평균. 1.5 = 평균의 1.5배(거래량 실림)."""
    v = _d(volume)
    ma = talib.SMA(v, timeperiod=period)
    out = np.full(len(v), np.nan)
    valid = ~np.isnan(ma) & (ma > 0)
    out[valid] = v[valid] / ma[valid]
    return out


def vwap(high, low, close, volume) -> np.ndarray:
    """누적 VWAP (세션 리셋 없음 — 백테스트 전체 기준)."""
    tp = (_d(high) + _d(low) + _d(close)) / 3.0
    vol = _d(volume)
    cum_pv = np.cumsum(tp * vol)
    cum_v = np.cumsum(vol)
    return np.where(cum_v > 0, cum_pv / cum_v, np.nan)


# ---- 캔들스틱 반전 패턴 (TA-Lib CDL*) ----
# 각 CDL 함수는 봉마다 +100/+200(강세) · -100/-200(약세) · 0(없음) 반환.
# 종합 반전 = 아래 세트 중 하나라도 뜨면 신호(강세 +100 / 약세 -100).
_CDL_BULL = ["ENGULFING", "HAMMER", "INVERTEDHAMMER", "MORNINGSTAR", "PIERCING",
             "3WHITESOLDIERS", "MORNINGDOJISTAR"]
_CDL_BEAR = ["ENGULFING", "HANGINGMAN", "SHOOTINGSTAR", "EVENINGSTAR", "DARKCLOUDCOVER",
             "3BLACKCROWS", "EVENINGDOJISTAR"]


def candle(name: str, open_, high, low, close) -> np.ndarray:
    """단일 TA-Lib 캔들패턴. name='ENGULFING' → talib.CDLENGULFING. 반환 float(±100/±200/0)."""
    fn = getattr(talib, "CDL" + name)
    return fn(_d(open_), _d(high), _d(low), _d(close)).astype(float)


def reversal(open_, high, low, close, bull: bool = True) -> np.ndarray:
    """종합 반전 신호 — 주요 반전 패턴 세트 중 하나라도 뜨면 +100(강세)/-100(약세), 아니면 0."""
    o, h, l, c = _d(open_), _d(high), _d(low), _d(close)
    out = np.zeros(len(c))
    for nm in (_CDL_BULL if bull else _CDL_BEAR):
        s = getattr(talib, "CDL" + nm)(o, h, l, c)
        out = np.where(s > 0, 100.0, out) if bull else np.where(s < 0, -100.0, out)
    return out
