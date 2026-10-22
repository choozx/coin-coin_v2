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
from numpy.lib.stride_tricks import sliding_window_view as swv


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


def session_vwap_z(open_time, high, low, close, volume, session_ms: int = 86_400_000,
                   warmup_ms: int = 3_600_000) -> np.ndarray:
    """세션(기본 UTC 일) VWAP 대비 이탈을 σ 단위로. z = (close − VWAP) / σ_VWAP.

    σ 는 세션 안 거래량가중 분산(Σv·tp² / Σv − VWAP²)의 제곱근 — 흔히 쓰는 VWAP 표준편차 밴드와 같다.
    세션마다 리셋하므로 **데이터 창의 시작점과 무관**하다. 누적 VWAP(vwap())는 백테스트 시작일에
    따라 값이 달라져 라이브와 어긋나므로 스캘핑 판정엔 이걸 쓴다. 분산이 0(세션 첫 봉 등)이면 NaN.

    warmup_ms: 세션 시작 후 이 시간 동안은 NaN. 봉이 몇 개 없으면 σ 가 0 에 가까워 z 가 폭발한다
    (실측: 진입 시점 z = 58,081). 그건 신호가 아니라 계산 인공물이다. 봉 수가 아니라 **시간**으로
    자르는 건 타임프레임과 무관하게 같은 구간을 비우려는 것이다.
    """
    ot = np.asarray(open_time, dtype=np.int64)
    tp = (_d(high) + _d(low) + _d(close)) / 3.0
    v = _d(volume)
    sess = ot // session_ms
    out = np.full(len(tp), np.nan)
    if not len(tp):
        return out
    starts = np.r_[0, np.flatnonzero(np.diff(sess)) + 1, len(tp)]
    for a, b in zip(starts[:-1], starts[1:]):
        cv = np.cumsum(v[a:b])
        with np.errstate(invalid="ignore", divide="ignore"):
            vw = np.cumsum(tp[a:b] * v[a:b]) / cv
            var = np.cumsum(tp[a:b] ** 2 * v[a:b]) / cv - vw ** 2
            sd = np.sqrt(np.where(var > 0, var, np.nan))
            out[a:b] = (_d(close)[a:b] - vw) / sd
    out[(ot - sess * session_ms) < warmup_ms] = np.nan
    return out


# ---- 이동평균 두 개(빠른·느린)로 읽는 방향·수렴·횡보 — 'SMA200 방향 + SMA22 타점' 기법용 ----
def sma_slope(close, period: int, lookback: int) -> np.ndarray:
    """SMA(period) 의 lookback 봉 변화율(%). +면 우상향('머리가 위'), −면 우하향."""
    m = sma(close, period)
    out = np.full(len(m), np.nan)
    if lookback < len(m):
        prev = m[:-lookback]
        with np.errstate(invalid="ignore", divide="ignore"):
            out[lookback:] = (m[lookback:] / prev - 1.0) * 100.0
    return out


def ma_gap(close, fast: int, slow: int, window: int = 1) -> np.ndarray:
    """|SMA(fast) − SMA(slow)| / 종가 (%) 의 직전 window 봉 최소값. 작을수록 두 선이 모였다(스퀴즈)."""
    c = _d(close)
    with np.errstate(invalid="ignore", divide="ignore"):
        g = np.abs(sma(c, fast) - sma(c, slow)) / c * 100.0
    if window <= 1:
        return g
    return talib.MIN(g, timeperiod=int(window))


def ma_angle(high, low, close, period: int = 75, sma_type: bool = True, lookback: int = 5,
             atr_period: int = 14, scale: float = 4.5) -> np.ndarray:
    """이평 '각도'(도, −90~90) — 차트 축척 대신 ATR 로 잰다: atan(봉당 이평 변화 ÷ ATR × scale).

    차트의 각도는 확대·축소에 따라 바뀌어 그대로는 못 옮긴다. 봉당 변화를 ATR 로 나누면 코인·봉 크기와 무관하게
    분포가 거의 같다(BTC·DOGE 1m~1h, SMA75: 상위 25% 0.08 · 상위 10% 0.12 ATR/봉). scale=4.5 면 봉당 0.22 ATR = 45°,
    75이평 상위 25% ≈ 20°, 상위 10% ≈ 28° — 영상('20~30도 이상이면 매우 강한 추세')의 감각에 맞춘 기준.
    """
    c = _d(close)
    m = sma(c, period) if sma_type else ema(c, period)
    atr = talib.ATR(_d(high), _d(low), c, timeperiod=atr_period)
    out = np.full(len(c), np.nan)
    k = max(1, int(lookback))
    if len(c) > k:
        with np.errstate(invalid="ignore", divide="ignore"):
            out[k:] = np.degrees(np.arctan((m[k:] - m[:-k]) / (k * atr[k:]) * scale))
    return out


def ma_crosses(close, fast: int, slow: int, window: int, ma_only: bool = False) -> np.ndarray:
    """직전 window 봉 동안의 교차 횟수 — 가격×빠른선 · 빠른선×느린선 · 가격×느린선 셋을 합친다.
    ma_only=True 면 빠른선×느린선만 — '이평이 서로 엉킨다'(가격은 짧은 이평을 몇 봉마다 넘나들어 섞으면 늘 많다).

    추세 중엔 거의 안 생기는 교차가 몰리면 횡보다('4단 교차'). 지표가 NaN 인 구간의 부호 변화는 세지 않는다.
    """
    c = _d(close)
    f, s = sma(c, fast), sma(c, slow)
    n = np.zeros(len(c))
    for a, b in (((f, s),) if ma_only else ((c, f), (f, s), (c, s))):
        d = np.sign(a - b)
        flip = np.zeros(len(c))
        ok = ~np.isnan(d[1:]) & ~np.isnan(d[:-1]) & (d[1:] != 0) & (d[:-1] != 0)
        flip[1:] = ((d[1:] != d[:-1]) & ok).astype(float)
        n += flip
    out = talib.SUM(n, timeperiod=int(window))
    out[np.isnan(s)] = np.nan
    return out


def body_atr(open_, high, low, close, period: int = 14) -> np.ndarray:
    """캔들 몸통 |종가−시가| 를 ATR 배수로. '몸통이 엄청 긴 캔들' 돌파 판정용."""
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.abs(_d(close) - _d(open_)) / atr(high, low, close, period)


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




def atr_rank(high, low, close, period: int = 14, lookback: int = 2016, chunk: int = 4096) -> np.ndarray:
    """변동성 순위 — 현재 NATR(ATR/종가) 이 직전 lookback 봉의 NATR 중 몇 백분위인가(0~100). 인과적.

    봉 t 의 값 = 100 × (직전 lookback 봉 NATR 중 현재보다 작은 것의 비율). 창이 다 차기 전·ATR 워밍업은 NaN.
    코인마다 변동성 수준이 달라 절대 임계값(예: ATR 0.3%)은 옮겨 쓸 수 없다 — 자기 최근 분포 대비 순위로 본다.
    research/exp_VR_vol_filter: RSI+CVD 다이버전스가 고변동(상위 20%)에서만 거래당 bp 가 컸다.
    """
    natr = talib.NATR(_d(high), _d(low), _d(close), timeperiod=period)
    n = len(natr)
    out = np.full(n, np.nan)
    if n <= lookback:
        return out
    win = swv(natr[:-1], lookback)                  # win[j] = natr[j : j+lookback] → 봉 j+lookback 의 과거
    for a in range(0, len(win), chunk):
        w = win[a:a + chunk]
        cur = natr[a + lookback:a + lookback + len(w)]
        valid = ~np.isnan(w).any(axis=1) & ~np.isnan(cur)
        r = (w < cur[:, None]).mean(axis=1) * 100.0
        out[a + lookback:a + lookback + len(w)] = np.where(valid, r, np.nan)
    return out

# ---- RSI 다이버전스 ----
def _rsi_div_long(low, r, level, min_gap, window, strength, cvd=None, cvd_mode=0):
    """강세 다이버전스 진입 봉 → (t, 저점2 가격) 목록. 봉 종가 기준·인과적(t 이후 데이터 안 봄).

    ① RSI < level 인 봉에서 저점1 시작 — P1 = 그 뒤 최저 저가, R1 = 최저 RSI.
    ② 저점1 뒤 min_gap 봉 이상 지나 저가가 P1(또는 이전 P2)보다 낮아지고 그 봉 RSI > R1 + strength → 저점2(P2).
       더 낮은 저가에서 RSI 도 R1 이하면 다이버전스가 아니라 새 저점1. min_gap 안의 더 낮은 저가는 같은 저점.
       가격은 안 낮아졌는데 RSI 가 R1 아래로 가면 R1 갱신·P2 무효.
    ③ P2 봉 뒤, 저가 > P2 · RSI ≥ level · RSI 상승 마감인 첫 봉 = 진입 신호.
    window 봉 안에 진입 못 하면 저점1 을 버린다. research/exp_DV_rsi_divergence.signals_long 과 같은 규칙.
    반환 (t, P2, tP1, tR1, tP2) — 뒤 셋은 차트용: 가격 저점1 봉 · RSI 저점1 봉 · 저점2 봉.

    cvd_mode — ②의 '저점2' 판정을 주문 흐름으로 바꾼다(research/exp_CV_cvd_divergence 의 A·B·C):
      0 RSI 만(RSI > R1) · 1 RSI 그리고 CVD(저점2) > CVD(가격 저점1) · 2 CVD 만(RSI 조건 없음).
      CVD(저점2) > CVD(저점1) = 두 저점 사이 시장가 매수가 매도보다 많았다(가격은 내렸는데 흡수).
    """
    out = []
    t1 = None
    P1 = R1 = P2 = None
    tP1 = tR1 = tP2 = -1
    for t in range(1, len(low)):
        x = r[t]
        if x != x:
            continue
        if t1 is not None and t - t1 > window:
            t1 = None
        if t1 is None:
            if x < level:
                t1, P1, R1, P2, tP1, tR1, tP2 = t, low[t], x, None, t, t, -1
            continue
        ref = P1 if P2 is None else P2
        if low[t] < ref:
            rsi_ok = x > R1 + strength
            ok = rsi_ok if cvd_mode == 0 else (cvd[t] > cvd[tP1]) if cvd_mode == 2 else (rsi_ok and cvd[t] > cvd[tP1])
            if t - t1 >= min_gap and ok:
                P2, tP2 = low[t], t
            elif t - t1 < min_gap:
                if x < R1:
                    tR1 = t
                P1, R1, t1, tP1, P2 = low[t], min(R1, x), t, t, None
            else:
                if cvd_mode == 2 and x >= R1:                # CVD 만: RSI 바닥은 그대로 둔다
                    P1, t1, tP1, P2 = low[t], t, t, None
                else:
                    P1, R1, t1, tP1, tR1, P2 = low[t], x, t, t, t, None
            continue
        if x < R1:
            R1, t1, tR1 = x, t, t
            if cvd_mode != 2:                                 # CVD 만이면 RSI 가 더 낮아져도 저점2 유지
                P2 = None
            continue
        if P2 is not None and t > tP2 and x >= level and x > r[t - 1]:
            out.append((t, P2, tP1, tR1, tP2))
            t1 = None
    return out


def _cvd_lists(volume, taker_buy, cvd_mode):
    """CVD(누적 시장가 매수 − 매도)와 그 거울(약세용). cvd_mode 0 이면 (None, None)."""
    if not cvd_mode:
        return None, None
    cvd = np.cumsum(2.0 * _d(taker_buy) - _d(volume))
    return cvd.tolist(), (-cvd).tolist()


def rsi_divergence(high, low, close, period: int = 14, level: float = 30.0,
                   min_gap: int = 5, window: int = 60, strength: float = 0.0,
                   volume=None, taker_buy=None, cvd_mode: int = 0) -> np.ndarray:
    """RSI 다이버전스 진입 신호. 봉마다 +1(강세 — 롱) · -1(약세 — 숏) · 0, RSI 워밍업은 NaN.

    강세: RSI 가 level 아래(과매도)였고 → 가격 저점은 낮아지는데 RSI 저점은 높아졌고 →
          봉 마감 RSI 가 level 위(이미 위면 첫 상승 마감)인 봉.
    약세: 위를 위아래로 뒤집은 것(가격 고점↑·RSI 고점↓, 기준 100 − level).
    손절 기준인 저점2 는 최근 봉 최저가와 같아서 프리셋 손절은 swing(전저점)으로 표현한다.
    상태가 있는(경로 의존) 신호라 라이브 창이 짧으면 시작부가 다를 수 있다 — window 의 3배면 수렴.
    cvd_mode 1·2 는 volume·taker_buy 가 필요하다(_rsi_div_long 참고). 약세는 CVD 도 뒤집어 본다
    (CVD(고점2) < CVD(고점1) = 두 고점 사이 매도 우세).
    """
    r = rsi(close, period)
    rl = r.tolist()
    rm = [100.0 - v if v == v else v for v in rl]
    cv, cvm = _cvd_lists(volume, taker_buy, int(cvd_mode))
    out = np.where(np.isnan(r), np.nan, 0.0)
    for t, *_ in _rsi_div_long(_d(low).tolist(), rl, level, int(min_gap), int(window), strength, cv, int(cvd_mode)):
        out[t] = 1.0
    for t, *_ in _rsi_div_long((-_d(high)).tolist(), rm, level, int(min_gap), int(window), strength, cvm,
                               int(cvd_mode)):
        out[t] = -1.0 if out[t] == 0 else 0.0          # 같은 봉에 양쪽이면 없음
    return out


def rsi_divergence_segments(high, low, close, period: int = 14, level: float = 30.0,
                            min_gap: int = 5, window: int = 60, strength: float = 0.0,
                            volume=None, taker_buy=None, cvd_mode: int = 0):
    """차트용 — 신호마다 다이버전스를 이루는 두 저점(약세는 고점). rsi_divergence 와 같은 규칙.

    반환 [{side, t, price: ((t1, p1), (t2, p2)), rsi: ((t1, r1), (t2, r2))}, ...] (t 는 봉 인덱스).
    """
    r = rsi(close, period)
    rl = r.tolist()
    rm = [100.0 - v if v == v else v for v in rl]
    lo, hi = _d(low), _d(high)
    cv, cvm = _cvd_lists(volume, taker_buy, int(cvd_mode))
    out = []
    for t, P2, tP1, tR1, tP2 in _rsi_div_long(lo.tolist(), rl, level, int(min_gap), int(window), strength,
                                              cv, int(cvd_mode)):
        out.append({"side": 1, "t": t, "price": ((tP1, float(lo[tP1])), (tP2, float(P2))),
                    "rsi": ((tR1, rl[tR1]), (tP2, rl[tP2]))})
    for t, P2, tP1, tR1, tP2 in _rsi_div_long((-hi).tolist(), rm, level, int(min_gap), int(window), strength,
                                              cvm, int(cvd_mode)):
        out.append({"side": -1, "t": t, "price": ((tP1, float(hi[tP1])), (tP2, -float(P2))),
                    "rsi": ((tR1, rl[tR1]), (tP2, rl[tP2]))})
    return sorted(out, key=lambda d: d["t"])

# ---- 차트 패턴 (핀바 · 삼각형 수렴 돌파) — 영상 매매법 필터용 ----
def _hold(sig: np.ndarray, within: int) -> np.ndarray:
    """신호(+1/-1)를 발생 봉 포함 within 봉 동안 유지 — '최근 N봉 안에 나왔으면' 필터용. 더 최근 신호가 이긴다."""
    sig = np.nan_to_num(np.asarray(sig, dtype=float))
    if within <= 1:
        return sig
    out = np.zeros(len(sig))
    last, age = 0.0, within
    for i, v in enumerate(sig.tolist()):
        if v != 0:
            last, age = v, 0
        else:
            age += 1
        out[i] = last if age < within else 0.0
    return out


def pin_bar(open_, high, low, close, wick: float = 66.0, body: float = 30.0, min_atr: float = 0.0,
            within: int = 1) -> np.ndarray:
    """핀바 — 한쪽 꼬리가 봉 길이(고가-저가)의 wick% 이상이고 몸통이 body% 이하. +1 아래꼬리(강세) · -1 위꼬리(약세).

    min_atr>0 이면 봉 길이 ≥ ATR(14)×min_atr 인 것만(작은 봉의 꼬리는 잡음). within=N 이면 최근 N봉 안 핀바도 참.
    """
    o, h, l, c = _d(open_), _d(high), _d(low), _d(close)
    rng = h - l
    top, bot = np.maximum(o, c), np.minimum(o, c)
    with np.errstate(invalid="ignore", divide="ignore"):
        ok = (rng > 0) & (np.abs(c - o) <= body / 100.0 * rng)
        bull = ok & ((bot - l) >= wick / 100.0 * rng)
        bear = ok & ((h - top) >= wick / 100.0 * rng)
        if min_atr > 0:
            big = rng >= talib.ATR(h, l, c, timeperiod=14) * min_atr
            bull &= big
            bear &= big
    return _hold(bull.astype(float) - bear.astype(float), int(within))


def _pivots(x: np.ndarray, k: int, high: bool) -> np.ndarray:
    """좌우 k 봉 안에서 최고(최저)인 봉 인덱스 — 봉 j 는 j+k 에 가서야 확정된다(호출부가 지연을 지킨다)."""
    n = len(x)
    if n < 2 * k + 1:
        return np.array([], dtype=int)
    w = swv(x, 2 * k + 1)
    ext = w.max(axis=1) if high else w.min(axis=1)
    mid = x[k:n - k]
    # 같은 값이 창 안에 여럿이면 가장 왼쪽 하나만 — 평평한 고점이 피벗 여러 개로 쪼개지지 않게
    first = (np.argmax(w, axis=1) if high else np.argmin(w, axis=1)) == k
    return np.nonzero((mid == ext) & first)[0] + k


def big_candle(open_, high, low, close, period: int = 14, mult: float = 2.0, within: int = 10) -> np.ndarray:
    """최근 within 봉(현재 포함) 안의 장대봉. +1 장대양봉 · −1 장대음봉 · 0. 장대 = 몸통 ≥ ATR(period) × mult. 더 최근 것이 이긴다.

    '장대양봉 뒤 눌림목' 처럼 큰 봉이 나온 **뒤** 몇 봉 동안의 조건을 걸 때 쓴다(지금 봉이 장대봉일 필요 없음).
    """
    o, c = _d(open_), _d(close)
    with np.errstate(invalid="ignore"):
        big = np.abs(c - o) >= atr(high, low, close, period) * mult
    return _hold((big & (c > o)).astype(float) - (big & (c < o)).astype(float), int(within))


def _triangle_events(high, low, close, pivot: int = 5, lookback: int = 100, tol: float = 10.0):
    """삼각형 수렴 돌파 이벤트 [(t, side, (i1,h1,i2,h2), (j1,l1,j2,l2), U(t), L(t))]. 인과적.

    봉 t 에서 이미 확정된(j+pivot ≤ t) 마지막 고점 둘·저점 둘로:
      고점2 ≤ 고점1 + tol%×폭 (낮아지거나 평평) · 저점2 ≥ 저점1 − tol%×폭 (높아지거나 평평) · 폭이 줄었다(고점2−저점2 < 폭)
      — 폭 = 고점1 − 저점1. '평평'을 가격 %로 재면 BTC 0.1%=60달러라 오르는 채널까지 삼각형이 됐다 → 패턴 폭 대비로.
      · 네 피벗이 다 최근 lookback 봉 안 · 아직 두 선이 만나기 전(U(t) > L(t)).
    두 고점을 이은 위 추세선 U, 두 저점을 이은 아래 추세선 L 을 t 까지 연장해
    종가가 U 를 처음 위로 넘으면 +1, L 을 처음 아래로 깨면 −1. 한 패턴(피벗 넷)에 신호는 한 번.
    """
    h, l, c = _d(high), _d(low), _d(close)
    n = len(c)
    ph, pl = _pivots(h, pivot, True).tolist(), _pivots(l, pivot, False).tolist()
    hi, lo = [], []                                   # 확정된 피벗(시간순)
    a = b = 0
    used = set()
    out = []
    for t in range(1, n):
        while a < len(ph) and ph[a] + pivot <= t:
            hi.append(ph[a]); a += 1
        while b < len(pl) and pl[b] + pivot <= t:
            lo.append(pl[b]); b += 1
        if len(hi) < 2 or len(lo) < 2:
            continue
        i1, i2, j1, j2 = hi[-2], hi[-1], lo[-2], lo[-1]
        if min(i1, j1) < t - lookback:
            continue
        h1, h2, l1, l2 = h[i1], h[i2], l[j1], l[j2]
        w0 = h1 - l1
        if not (w0 > 0 and h2 <= h1 + tol / 100.0 * w0 and l2 >= l1 - tol / 100.0 * w0 and (h2 - l2) < w0):
            continue
        key = (i1, i2, j1, j2)
        if key in used:
            continue
        su, sl = (h2 - h1) / (i2 - i1), (l2 - l1) / (j2 - j1)
        U, L = h2 + su * (t - i2), l2 + sl * (t - j2)
        if U <= L:
            continue                                  # 꼭짓점 지남 — 패턴 끝
        Up, Lp = U - su, L - sl                        # 직전 봉의 선 값
        if c[t] > U and c[t - 1] <= Up:
            out.append((t, 1, (i1, h1, i2, h2), (j1, l1, j2, l2), U, L)); used.add(key)
        elif c[t] < L and c[t - 1] >= Lp:
            out.append((t, -1, (i1, h1, i2, h2), (j1, l1, j2, l2), U, L)); used.add(key)
    return out


def triangle_breakout(high, low, close, pivot: int = 5, lookback: int = 100, tol: float = 10.0,
                      within: int = 1) -> np.ndarray:
    """삼각형 수렴 돌파 신호 +1(위 돌파)/−1(아래 이탈)/0. 정의는 _triangle_events. within=N 이면 최근 N봉 안 돌파도 참."""
    sig = np.zeros(len(close))
    for t, side, *_ in _triangle_events(high, low, close, pivot, lookback, tol):
        sig[t] = side
    return _hold(sig, int(within))


# ---- 직전 박스 고·저 (영상 '5봉 박스 돌파') ----
def box_levels(open_time, high, low, n: int, tf_ms: int):
    """시간 경계로 n 봉씩 끊은 블록(예: 1분봉 n=5 → 5분 단위)에서, 각 봉이 속한 블록의 **직전 블록** 고가·저가.

    블록 안에서는 값이 일정하고 새 블록 첫 봉에서 바뀐다 — 직전 블록은 이미 닫혔으니 인과적.
    값이 캔들 시각으로만 정해져 데이터 창이 어디서 시작하든 같다: 창이 블록 중간에서 시작하면 그 첫 블록은
    잘린 것이라 '직전 블록'으로 쓰지 않는다(NaN). 블록 하나가 통째로 비면(데이터 구멍) 그다음도 NaN.
    """
    t = np.asarray(open_time, dtype=np.int64)
    h, lo = _d(high), _d(low)
    nb = len(t)
    out_h, out_l = np.full(nb, np.nan), np.full(nb, np.nan)
    if nb == 0 or n < 1 or tf_ms <= 0:
        return out_h, out_l
    size = int(n) * int(tf_ms)
    blk = t // size
    starts = np.flatnonzero(np.r_[True, blk[1:] != blk[:-1]])
    lens = np.diff(np.r_[starts, nb])
    bh, bl = np.maximum.reduceat(h, starts), np.minimum.reduceat(lo, starts)
    ids = blk[starts]
    ok = np.r_[False, ids[1:] == ids[:-1] + 1]               # 바로 앞 블록이 있어야
    if len(ok) > 1 and t[0] % size != 0:
        ok[1] = False                                         # 첫 블록이 잘렸다
    prev_h = np.where(ok, np.r_[np.nan, bh[:-1]], np.nan)
    prev_l = np.where(ok, np.r_[np.nan, bl[:-1]], np.nan)
    return np.repeat(prev_h, lens), np.repeat(prev_l, lens)


# ---- 윌리엄스 프랙탈 · 눌림 → 프랙탈 → 재돌파 (영상 '마하세븐' 이평선 눌림목) ----
def fractal_points(high, low, n: int = 2):
    """윌리엄스 프랙탈 (저점 중심 인덱스들, 고점 중심 인덱스들). 좌우 n 봉보다 낮은 저점 / 높은 고점.
    중심 c 는 c+n 봉이 닫혀야 확정된다 — 신호로 쓸 때는 반드시 c+n 에서."""
    return _pivots(_d(low), n, False), _pivots(_d(high), n, True)


def fractal(high, low, n: int = 2) -> np.ndarray:
    """프랙탈이 '확정되는 봉'에 +1(저점 프랙탈, 빨강) · −1(고점 프랙탈, 파랑). 같은 봉에 둘 다면 0."""
    lows, highs = fractal_points(high, low, n)
    out = np.zeros(len(high))
    out[lows + n] += 1
    out[highs + n] -= 1
    return out


def pullback_fractal(high, low, close, ma: np.ndarray, n: int = 2, window: int = 60) -> np.ndarray:
    """눌림 → 프랙탈 → 재돌파. +1 롱 · −1 숏 · 0. 인과적.

    롱: 종가가 이평 아래로 내려간 뒤(눌림 시작) → 그 눌림 안에서 저점 프랙탈이 확정되고 → 종가가 이평을 상향 돌파한 봉.
    숏: 거울(이평 위로 반등 → 고점 프랙탈 → 하향 돌파). 프랙탈 확정 전에 이평을 다시 넘으면 그 눌림은 버린다.
    눌림 시작부터 window 봉이 지나도 돌파가 없으면 버린다.
    """
    c, m = _d(close), _d(ma)
    nb = len(c)
    lows, highs = fractal_points(high, low, n)
    lo_conf = np.full(nb, -1)
    hi_conf = np.full(nb, -1)
    ok = lows + n < nb
    lo_conf[(lows + n)[ok]] = lows[ok]               # 확정 봉 → 프랙탈 중심 인덱스
    ok = highs + n < nb
    hi_conf[(highs + n)[ok]] = highs[ok]
    out = np.zeros(nb)
    cl, ml, lc, hc = c.tolist(), m.tolist(), lo_conf.tolist(), hi_conf.tolist()
    d_lo = d_hi = None                                # 눌림(반등) 시작 봉
    r_lo = r_hi = False                               # 그 안에서 프랙탈 확정됐나
    for t in range(1, nb):
        x, mv, xp, mp = cl[t], ml[t], cl[t - 1], ml[t - 1]
        if mv != mv or mp != mp:
            continue
        # 롱 쪽
        if d_lo is not None and t - d_lo > window:
            d_lo, r_lo = None, False
        if x < mv and d_lo is None:
            d_lo = t
        if d_lo is not None and lc[t] >= d_lo:
            r_lo = True
        if x > mv and xp <= mp:
            if d_lo is not None and r_lo:
                out[t] = 1
            d_lo, r_lo = None, False
        # 숏 쪽
        if d_hi is not None and t - d_hi > window:
            d_hi, r_hi = None, False
        if x > mv and d_hi is None:
            d_hi = t
        if d_hi is not None and hc[t] >= d_hi:
            r_hi = True
        if x < mv and xp >= mp:
            if d_hi is not None and r_hi:
                out[t] = -1
            d_hi, r_hi = None, False
    return out


# ---- 직전 고·저 · 피벗 가격 · 매물대 · 볼린저 상태 — 영상 분석 '🛠 구현 가능' 규칙들 ----
def prev_extreme(x, n: int = 20, high: bool = True) -> np.ndarray:
    """직전 n 봉(현재 봉 제외)의 최고값(high) / 최저값. n=1 이면 x[t-1] 그대로. 앞 n 봉·창에 NaN 이 끼면 NaN.

    '지표의 직전 고점 수평선 돌파'(RSI > 직전 N봉 RSI 최고)와 '고가 > 직전 봉 고가' 를 같은 함수로 쓴다.
    """
    x = _d(x)
    out = np.full(len(x), np.nan)
    if n < 1 or len(x) <= n:
        return out
    w = swv(x[:-1], n)                              # w[j] = x[j : j+n] → 봉 j+n 의 직전 n 봉
    out[n:] = w.max(axis=1) if high else w.min(axis=1)
    return out


def pivot_level(high, low, n: int = 2, nth: int = 0, top: bool = False) -> np.ndarray:
    """봉 t 에서 이미 확정된(중심+n ≤ t) 저점(top=False)·고점 프랙탈 중 최근 nth 번째(0=마지막, 1=그 전)의 가격. 없으면 NaN.

    쌍바닥 '두 번째 저점이 전저점을 깼다' = pivot_level(nth=0) < pivot_level(nth=1). 다음 피벗이 확정될 때까지 값이 유지된다.
    """
    lows, highs = fractal_points(high, low, n)
    idx = highs if top else lows
    src = _d(high) if top else _d(low)
    out = np.full(len(src), np.nan)
    k = np.searchsorted(idx + n, np.arange(len(src)), side="right") - 1 - int(nth)
    ok = k >= 0
    out[ok] = src[idx[k[ok]]]
    return out


def vp_share(high, low, close, volume, window: int = 200, band: float = 1.0, above: bool = True,
             chunk: int = 4096) -> np.ndarray:
    """매물대 두께(VPVR 근사) — 최근 window 봉(현재 포함) 거래량 중 지금 종가 위 band% 구간(above) /
    아래 band% 구간에 쌓인 비중(%). 봉 거래량은 그 봉의 저가~고가에 고르게 퍼졌다고 본다. 인과적.

    클수록 '두꺼운 매물대'(한 번에 뚫기 어렵다), 작을수록 '얇은 매물대'. 창이 다 차기 전은 NaN.
    """
    h, l, c, v = _d(high), _d(low), _d(close), _d(volume)
    nb = len(c)
    out = np.full(nb, np.nan)
    if window < 1 or nb < window:
        return out
    wh, wl, wv = swv(h, window), swv(l, window), swv(v, window)    # 행 r = 봉 r+window-1 까지의 창
    b = band / 100.0
    for a in range(0, len(wh), chunk):
        H, L, V = wh[a:a + chunk], wl[a:a + chunk], wv[a:a + chunk]
        cur = c[a + window - 1:a + window - 1 + len(H)][:, None]
        lo, hi = (cur, cur * (1 + b)) if above else (cur * (1 - b), cur)
        width = H - L
        ov = np.clip(np.minimum(H, hi) - np.maximum(L, lo), 0.0, None)
        with np.errstate(invalid="ignore", divide="ignore"):
            frac = np.where(width > 0, ov / width, ((L >= lo) & (L <= hi)).astype(float))
            tot = V.sum(axis=1)
            out[a + window - 1:a + window - 1 + len(H)] = np.where(tot > 0, 100.0 * (V * frac).sum(axis=1) / tot, np.nan)
    return out


def bb_touches(high, low, close, period: int = 20, stddev: float = 2.0, window: int = 30, upper: bool = True) -> np.ndarray:
    """최근 window 봉(현재 포함) 중 고가가 볼린저 상단 이상(upper) / 저가가 하단 이하였던 봉 수. 밴드 워밍업은 NaN."""
    up, _, lo = bollinger(close, period, stddev)
    with np.errstate(invalid="ignore"):
        hit = (_d(high) >= up) if upper else (_d(low) <= lo)
    cs = np.concatenate([[0], np.cumsum(hit)])
    idx = np.arange(len(hit))
    out = (cs[idx + 1] - cs[np.maximum(0, idx + 1 - window)]).astype(float)
    out[np.isnan(up)] = np.nan
    return out


def bb_pullback(high, low, close, period: int = 20, stddev: float = 2.0, n: int = 2, window: int = 60) -> np.ndarray:
    """볼린저 눌림 상태. +1 롱 대기 · −1 숏 대기 · 0. 인과적.

    롱: 저가가 하단밴드 터치 → 고가가 중간선 도달(반등) → 그 뒤 고점 프랙탈 확정(다시 하락) 이면 +1 이 켜지고,
        종가가 그 프랙탈 고점을 넘는 봉까지 유지된다(그 봉도 +1 — 돌파 진입 조건과 AND 로 쓴다).
        하단을 다시 터치하면 거기서 처음부터, 터치부터 window 봉이 지나면 버린다.
    숏: 거울(상단 터치 → 중간선까지 하락 → 저점 프랙탈 확정 → 종가가 그 저점 아래로 갈 때까지 −1).
    """
    up, mid, lo = bollinger(close, period, stddev)
    nb = len(close)
    lows, highs = fractal_points(high, low, n)
    lo_conf = np.full(nb, -1)
    hi_conf = np.full(nb, -1)
    ok = lows + n < nb
    lo_conf[(lows + n)[ok]] = lows[ok]               # 확정 봉 → 프랙탈 중심 인덱스
    ok = highs + n < nb
    hi_conf[(highs + n)[ok]] = highs[ok]
    hl, ll, cl = _d(high).tolist(), _d(low).tolist(), _d(close).tolist()
    ul, ml, bl, lc, hc = up.tolist(), mid.tolist(), lo.tolist(), lo_conf.tolist(), hi_conf.tolist()
    out = np.zeros(nb)

    def side(t, touch, reach, conf, beyond, sign, st):
        # st = [터치 봉, 중간선 도달 봉, 무장 레벨]
        if touch:
            st[:] = [t, None, None]
            return
        if st[0] is None:
            return
        if t - st[0] > window:
            st[:] = [None, None, None]
            return
        if st[1] is None and reach:
            st[1] = t
        if st[1] is not None and st[2] is None and conf >= st[1]:
            st[2] = (hl if sign > 0 else ll)[conf]
        if st[2] is not None:
            out[t] += sign
            if beyond(st[2]):
                st[:] = [None, None, None]

    s_long, s_short = [None, None, None], [None, None, None]
    for t in range(nb):
        if ml[t] != ml[t]:
            continue
        side(t, ll[t] <= bl[t], hl[t] >= ml[t], hc[t], lambda lv: cl[t] > lv, 1, s_long)
        side(t, hl[t] >= ul[t], ll[t] <= ml[t], lc[t], lambda lv: cl[t] < lv, -1, s_short)
    return out


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
