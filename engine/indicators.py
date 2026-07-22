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


def adx(high, low, close, period: int = 14) -> np.ndarray:
    """Average Directional Index — 추세 '강도' 0~100 (방향 무관). 높을수록(예 25+) 추세장, 낮으면 횡보.
    레짐 필터용: 추세추종은 ADX 높을 때만, 평균회귀는 낮을 때만 진입하는 게이트."""
    return talib.ADX(_d(high), _d(low), _d(close), timeperiod=period)


def plus_di(high, low, close, period: int = 14) -> np.ndarray:
    """+DI — 상승 방향성 강도. +DI > -DI 면 상승 우위."""
    return talib.PLUS_DI(_d(high), _d(low), _d(close), timeperiod=period)


def minus_di(high, low, close, period: int = 14) -> np.ndarray:
    """-DI — 하락 방향성 강도."""
    return talib.MINUS_DI(_d(high), _d(low), _d(close), timeperiod=period)


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


def supertrend(high, low, close, period: int = 10, multiplier: float = 3.0):
    """SuperTrend — ATR 기반 추세 추종 라인. 반환 (line, direction).

    - line: 추세선. 상승추세엔 가격 아래(지지), 하락추세엔 가격 위(저항).
    - direction: +1 상승추세 / -1 하락추세. -1→+1 전환이 강세 플립 신호.

    상태가 있는(재귀) 지표라 봉을 순회하며 이전 밴드/방향을 이어받는다.
    ATR 워밍업(NaN) 구간은 NaN으로 남긴다(conditions.py가 false 처리).
    """
    h, l, c = _d(high), _d(low), _d(close)
    atr_ = talib.ATR(h, l, c, timeperiod=period)
    hl2 = (h + l) / 2.0
    basic_u = hl2 + multiplier * atr_       # 기본 상단 밴드
    basic_l = hl2 - multiplier * atr_       # 기본 하단 밴드
    n = len(c)
    fu = np.full(n, np.nan)                 # 최종 상단 밴드
    fl = np.full(n, np.nan)                 # 최종 하단 밴드
    line = np.full(n, np.nan)
    dir_ = np.full(n, np.nan)
    started = False
    for i in range(n):
        if np.isnan(atr_[i]):
            continue
        if not started:                     # 첫 유효봉: 상단 밴드에서 하락추세로 시드(다음 봉부터 자기교정)
            fu[i], fl[i] = basic_u[i], basic_l[i]
            line[i], dir_[i] = fu[i], -1.0
            started = True
            continue
        # 최종 밴드: 추세가 유지되는 한 밴드가 가격 쪽으로만 좁혀지도록 잠금
        fu[i] = basic_u[i] if (basic_u[i] < fu[i-1] or c[i-1] > fu[i-1]) else fu[i-1]
        fl[i] = basic_l[i] if (basic_l[i] > fl[i-1] or c[i-1] < fl[i-1]) else fl[i-1]
        if line[i-1] == fu[i-1]:            # 직전 하락추세
            if c[i] <= fu[i]:
                line[i], dir_[i] = fu[i], -1.0
            else:                           # 상단 상향 돌파 → 상승 플립
                line[i], dir_[i] = fl[i], 1.0
        else:                               # 직전 상승추세
            if c[i] >= fl[i]:
                line[i], dir_[i] = fl[i], 1.0
            else:                           # 하단 하향 이탈 → 하락 플립
                line[i], dir_[i] = fu[i], -1.0
    return line, dir_


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


# ---- 오더플로우: 테이커 매수/매도 델타 & CVD ----
# klines의 taker_buy(테이커가 공격적으로 산 체결량)로 봉당 매수/매도 압력을 계산.
# taker_sell = volume - taker_buy → delta = taker_buy - taker_sell = 2*taker_buy - volume.
def taker_delta(volume, taker_buy) -> np.ndarray:
    """봉당 테이커 순매수(base) = 2*taker_buy - volume. +면 공격 매수 우위."""
    return 2.0 * _d(taker_buy) - _d(volume)


def taker_delta_ratio(volume, taker_buy) -> np.ndarray:
    """정규화 델타 = delta / volume, 범위 [-1, 1]. +0.2 ≈ 매수세 강함, -0.2 ≈ 매도세 강함."""
    v = _d(volume)
    d = 2.0 * _d(taker_buy) - v
    out = np.full(len(v), np.nan)
    nz = v > 0
    out[nz] = d[nz] / v[nz]
    return out


def cvd(volume, taker_buy) -> np.ndarray:
    """누적 볼륨 델타(CVD) = 델타 누적합. 결측(NaN) 봉은 0으로 보고 누적."""
    d = 2.0 * _d(taker_buy) - _d(volume)
    d = np.where(np.isnan(d), 0.0, d)
    return np.cumsum(d)


def hawkeye(high, low, close, volume, length: int = 200, divisor: float = 3.6) -> np.ndarray:
    """HawkEye Volume Indicator (LazyBear) — VSA식 봉 분류.

    각 봉을 강세(초록, 매수세 유입)/약세(빨강, 매도세)/중립(회색·기본)으로 나눠
    +1(강세) / -1(약세) / 0(중립) 신호로 반환한다. OHLCV만 사용.
    원본 색 우선순위(gray > green > red > blue)를 그대로 따름.
    """
    h, l, c, v = _d(high), _d(low), _d(close), _d(volume)
    rng = h - l
    range_avg = talib.SMA(rng, length)
    volume_avg = talib.SMA(v, length)

    high1 = np.roll(h, 1); low1 = np.roll(l, 1); vol1 = np.roll(v, 1)
    high1[0] = low1[0] = vol1[0] = np.nan          # 첫 봉엔 이전 봉 없음
    mid1 = (high1 + low1) / 2.0
    u1 = mid1 + (high1 - low1) / divisor
    d1 = mid1 - (high1 - low1) / divisor

    # 빨강(매도세)
    r = ((rng > range_avg) & (c < d1) & (v > volume_avg)) | (c < mid1)
    # 초록(매수세)
    g = ((c > mid1)
         | ((rng > range_avg) & (c > u1) & (v > volume_avg))
         | ((h > high1) & (rng < range_avg / 1.5) & (v < volume_avg))
         | ((l < low1) & (rng < range_avg / 1.5) & (v > volume_avg)))
    # 회색(중립/노디맨드)
    gr = (((rng > range_avg) & (c > d1) & (c < u1) & (v > volume_avg)
           & (v < volume_avg * 1.5) & (v > vol1))
          | ((rng < range_avg / 1.5) & (v < volume_avg / 1.5))
          | ((c > d1) & (c < u1)))

    # 우선순위: 회색(0) → 초록(+1) → 빨강(-1) → 파랑(0)
    out = np.select([gr, g, r], [0.0, 1.0, -1.0], default=0.0)
    warmup = np.isnan(range_avg) | np.isnan(volume_avg) | np.isnan(mid1)
    return np.where(warmup, np.nan, out)


def qqe(source, rsi_length: int = 6, smoothing: int = 5, factor: float = 3.0):
    """QQE 코어 (Quantitative Qualitative Estimation). 반환 (qqe_line, smoothed_rsi).

    - smoothed_rsi = EMA(RSI). qqe_line = ATR(RSI)식 트레일링 밴드(longBand/shortBand)를
      추세방향으로 선택한 값. 둘 다 0~100 스케일(중심 50).
    - 상태가 있는(재귀) 지표라 밴드·추세를 봉마다 이어받는다. 원본 Pine 로직 그대로.
    """
    src = _d(source)
    n = len(src)
    rsi = talib.RSI(src, rsi_length)
    rsi_ma = talib.EMA(rsi, smoothing)                 # smoothedRsi
    wilders = rsi_length * 2 - 1
    atr_rsi = np.full(n, np.nan)
    atr_rsi[1:] = np.abs(rsi_ma[:-1] - rsi_ma[1:])     # |smoothedRsi[1] - smoothedRsi|
    dar = talib.EMA(atr_rsi, wilders) * factor         # dynamicAtrRsi

    longband = np.zeros(n)
    shortband = np.zeros(n)
    trend = np.ones(n)
    line = np.full(n, np.nan)

    valid = ~np.isnan(rsi_ma) & ~np.isnan(dar)
    if not valid.any():
        return line, rsi_ma
    start = int(np.argmax(valid))                      # 첫 유효봉 (이후 연속 유효)
    for i in range(start, n):
        ns = rsi_ma[i] + dar[i]
        nl = rsi_ma[i] - dar[i]
        if i == start:
            longband[i], shortband[i], trend[i], line[i] = nl, ns, 1, nl
            continue
        pl, ps, prm = longband[i - 1], shortband[i - 1], rsi_ma[i - 1]
        longband[i] = max(pl, nl) if (prm > pl and rsi_ma[i] > pl) else nl
        shortband[i] = min(ps, ns) if (prm < ps and rsi_ma[i] < ps) else ns
        # ta.cross(smoothedRsi, shortBand[1]) / ta.cross(longBand[1], smoothedRsi)
        sb1, sb2 = shortband[i - 1], (shortband[i - 2] if i - 2 >= start else shortband[i - 1])
        lb1, lb2 = longband[i - 1], (longband[i - 2] if i - 2 >= start else longband[i - 1])
        cross_short = ((rsi_ma[i] > sb1 and rsi_ma[i - 1] <= sb2) or
                       (rsi_ma[i] < sb1 and rsi_ma[i - 1] >= sb2))
        cross_long = ((lb1 > rsi_ma[i] and lb2 <= rsi_ma[i - 1]) or
                      (lb1 < rsi_ma[i] and lb2 >= rsi_ma[i - 1]))
        trend[i] = 1 if cross_short else (-1 if cross_long else trend[i - 1])
        line[i] = longband[i] if trend[i] == 1 else shortband[i]
    line[:start] = np.nan
    return line, rsi_ma


def qqe_mod(source, rsi_length: int = 6, smoothing: int = 5, factor_primary: float = 3.0,
            factor_secondary: float = 1.61, threshold: float = 3.0,
            bb_length: int = 50, bb_mult: float = 0.35) -> np.ndarray:
    """QQE MOD (Mihkel00) 종합 신호 → +1 매수 / -1 매도 / 0 없음.

    매수 = 보조 smoothedRSI-50 > threshold  그리고  주 smoothedRSI-50 > 볼린저 상단.
    매도 = 보조 smoothedRSI-50 < -threshold 그리고  주 smoothedRSI-50 < 볼린저 하단.
    (볼린저는 주 QQE 트렌드라인-50 기준 SMA±mult·STDEV, length=bb_length.)
    """
    line_p, rsi_p = qqe(source, rsi_length, smoothing, factor_primary)
    _, rsi_s = qqe(source, rsi_length, smoothing, factor_secondary)
    basis = talib.SMA(line_p - 50.0, bb_length)
    dev = bb_mult * talib.STDDEV(line_p - 50.0, bb_length, nbdev=1)
    upper, lower = basis + dev, basis - dev
    rp, rs = rsi_p - 50.0, rsi_s - 50.0
    up = (rs > threshold) & (rp > upper)
    down = (rs < -threshold) & (rp < lower)
    out = np.where(up, 1.0, np.where(down, -1.0, 0.0))
    warmup = np.isnan(rp) | np.isnan(rs) | np.isnan(upper)
    return np.where(warmup, np.nan, out)


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
