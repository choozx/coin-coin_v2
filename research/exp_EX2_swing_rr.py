"""EX2 — 사용자 청산: 손절 = 전저점(최근 N봉 최저가), 익절 = 진입가 + n × (진입가 − 손절가). EX 와 같은 비교 틀.

요청(2026-10-14): "손절은 전저점, 익절은 진입-손절 거리의 n배". 엔진 프리셋으로 이미 표현된다
(stopLoss {type: swing, lookback: N} + takeProfit {type: riskReward, value: n}). 엔진과 같은 정의:
전저점 = 신호봉 포함 최근 N봉 저가 최저, 버퍼 0.
비교: EX 와 같은 진입 넷(R5 과매도 · DV 다이버전스 · 랜덤 · 고변동 랜덤), BTC 5m 전 기간 롱, gross.
격자: N 5·10·20·50 × n 0.5·1·1.5·2·3·5 × 시간 48·288봉(4시간·1일) = 48. 같은 봉이면 손절 먼저, 갭 무시(양쪽 같다).

    python3 -u -m research.exp_EX2_swing_rr
"""
from __future__ import annotations

import sys

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                    # noqa: E402
from engine import indicators as ind                     # noqa: E402
from engine.candles import resample                      # noqa: E402
from research import lib                                 # noqa: E402
from research.exp_DV_rsi_divergence import signals_long  # noqa: E402

NS = (5, 10, 20, 50)
RS = (0.5, 1, 1.5, 2, 3, 5)
TS = (48, 288)
N_RAND = 50_000


def run(b, idx, lowN, n_r, T):
    off = idx[:, None] + np.arange(1, T + 1)[None, :]
    e = b.close[idx]
    stop = lowN[idx]
    risk = e - stop
    ok = risk > 0
    e, stop, risk, off = e[ok], stop[ok], risk[ok], off[ok]
    tp = e + n_r * risk
    hs = b.low[off] <= stop[:, None]
    ht = b.high[off] >= tp[:, None]
    big = T + 1
    ts = np.where(hs.any(1), hs.argmax(1), big)
    tt = np.where(ht.any(1), ht.argmax(1), big)
    r = b.close[off[:, -1]] / e - 1
    m_s = (ts <= tt) & (ts < big)
    m_t = tt < ts
    r[m_s] = stop[m_s] / e[m_s] - 1
    r[m_t] = tp[m_t] / e[m_t] - 1
    return r, m_t.mean(), m_s.mean(), np.median(risk / e) * 1e4


def main():
    st = cs.stats("BTCUSDT")
    b = resample(lib.load("BTCUSDT", start_ms=st["min"], end_ms=st["max"], with_funding=False)[0], 5)
    n = len(b.close)
    rsi = ind.rsi(b.close, 14)
    atr = ind.atr(b.high, b.low, b.close, 14) / b.close
    ok = np.isfinite(atr) & (np.arange(n) < n - max(TS) - 1) & (np.arange(n) > 200)
    prev = np.concatenate(([np.nan], rsi[:-1]))
    with np.errstate(invalid="ignore"):
        r5 = np.nonzero((rsi < 30) & (prev >= 30) & ok)[0]
    dv = np.array([t for t, _ in signals_long(b.low.tolist(), rsi.tolist(), 30)], int)
    dv = dv[ok[dv]]
    rng = np.random.default_rng(0)
    pool = np.nonzero(ok)[0]
    rnd = rng.choice(pool, N_RAND, replace=False)
    hv = pool[atr[pool] >= np.percentile(atr[pool], 80)]
    rhv = rng.choice(hv, N_RAND, replace=False)
    sets = {"R5": r5, "DV": dv, "랜덤": rnd, "고변동랜덤": rhv}
    lows = {}
    for N in NS:
        lw = np.full(n, np.nan)
        lw[N - 1:] = swv(b.low, N).min(axis=1)            # 신호봉 포함 최근 N봉
        lows[N] = lw
    print("EX2 · 손절 전저점(최근 N봉 최저) · 익절 n × 손절폭 · BTCUSDT 5m 롱 · gross · 거래당 bp")
    print("      R5 = RSI 과매도 진입 · DV = 다이버전스 진입 · 랜덤 · 고변동랜덤(ATR 상위 20% 봉)")
    for T in TS:
        print(f"\n  시간 청산 {T}봉({T * 5 // 60}시간)")
        print(f"  {'N':>3} {'n':>4} | {'R5':>6} {'DV':>6} {'랜덤':>6} {'고변동':>6} | {'DV 익절%':>7} {'DV 손절%':>7} {'DV 손절폭':>8}")
        best = {}
        for N in NS:
            for r_ in RS:
                row = {}
                for k, v in sets.items():
                    r, pt, ps, rk = run(b, v, lows[N], r_, T)
                    row[k] = (r.mean() * 1e4, pt, ps, rk)
                for k in sets:
                    if k not in best or row[k][0] > best[k][0]:
                        best[k] = (row[k][0], N, r_)
                d = row["DV"]
                print(f"  {N:3d} {r_:4g} | {row['R5'][0]:+6.1f} {row['DV'][0]:+6.1f} {row['랜덤'][0]:+6.1f} {row['고변동랜덤'][0]:+6.1f} | "
                      f"{d[1] * 100:6.0f}% {d[2] * 100:6.0f}% {d[3]:6.0f}bp")
        print("  최고: " + " · ".join(f"{k} {v[0]:+.1f}bp (N={v[1]}, n={v[2]:g})" for k, v in best.items()))


if __name__ == "__main__":
    main()
