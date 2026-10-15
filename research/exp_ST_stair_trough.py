"""ST — 다이버전스 저점1 의 기준을 'RSI 바닥 봉'에 고정 (계단식 하락 수정).

요청(2026-10-14): 차트에서 사용자가 본 상승 다이버전스를 규칙이 늦게 잡았다.
원인: 현행 규칙은 'min_gap(5봉) 안의 더 낮은 저가 = 같은 저점1' 이고 그때마다 5봉 시계를 다시 건다.
계단식으로 조금씩 낮아지는 하락에서는 시계가 계속 리셋돼 저점1 이 오른쪽으로 밀리고, 눈에 보이는 다이버전스
(RSI 바닥 vs 그 뒤 더 낮은 가격 저점)를 놓친다. 전체 신호의 6.9% 가 가격 저점1 이 RSI 저점1 보다 5봉 이상 늦다.

수정(stair): 저점1 = RSI 바닥 봉(tR1)에 고정. tR1 뒤 min_gap 봉 안의 더 낮은 저가는 저점1 가격(P1)만 갱신하고 시계는 그대로.
  tR1 + min_gap 이후 P1(또는 P2)보다 낮은 저가 + 저점2 조건(RSI > R1 · CVD 모드면 CVD(저점2) > CVD(가격 저점1)) → 저점2.
  저점2 조건이 안 맞는 더 낮은 저가 → 가격 저점1 을 거기로 옮긴다(RSI 바닥 기준은 유지).
  RSI 가 R1 아래로 가면 RSI 바닥을 그 봉으로 옮기고 저점2 무효(CVD 만 모드는 유지 — CV 의 C 와 같다).
  진입·창(window, RSI 바닥부터)·청산은 현행과 같다.
비교: 모드 A(RSI 만)·B(RSI+CVD)·C(CVD 만) × 현행/수정, 롱, 청산 = 프리셋(전저점 20·2.5R·48봉), gross.
판정(결과 전 고정): 수정이 현행보다 거래당 gross bp 가 다섯 시험(BTC 3 + ETH + SOL) 중 ≥ 4 에서 높으면 '수정이 낫다'.

    python3 -u -m research.exp_ST_stair_trough
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                   # noqa: E402
from engine.candles import resample                     # noqa: E402
from research import lib                                # noqa: E402
from research.exp_CV_cvd_divergence import FEE, GAP, LEVEL, NR, T, WIN, prep, signals  # noqa: E402
from research.exp_EX2_swing_rr import run               # noqa: E402
from research.exp_T_supertrend_search import _ym, segments  # noqa: E402


def signals_stair(low, r, cvd, mode):
    out = []
    t1 = None                                   # = RSI 바닥 봉(tR1)
    for t in range(1, len(low)):
        x = r[t]
        if x != x:
            continue
        if t1 is not None and t - t1 > WIN:
            t1 = None
        if t1 is None:
            if x < LEVEL:
                t1, R1, P1, tP1, P2, tP2 = t, x, low[t], t, None, -1
            continue
        if x < R1:                              # RSI 바닥 갱신
            R1, t1 = x, t
            if low[t] < P1:
                P1, tP1 = low[t], t
            if mode != "C":
                P2 = None
            continue
        if t - t1 < GAP:                        # 바닥 근처 — 가격 저점1 만 갱신, 시계 유지
            if low[t] < P1:
                P1, tP1 = low[t], t
            continue
        ref = P1 if P2 is None else P2
        if low[t] < ref:
            rsi_ok = x > R1
            cvd_ok = cvd[t] > cvd[tP1]
            ok = rsi_ok if mode == "A" else (rsi_ok and cvd_ok) if mode == "B" else cvd_ok
            if ok:
                P2, tP2 = low[t], t
            else:                               # 저점2 실패 → 가격 저점1 을 옮긴다(RSI 바닥 유지)
                P1, tP1, P2 = low[t], t, None
            continue
        if P2 is not None and t > tP2 and x >= LEVEL and x > r[t - 1]:
            out.append(t)
            t1 = None
    return np.array(out, int)


def main():
    tests = [(f"BTC 구간{i+1}", "BTCUSDT", a, z) for i, (a, z) in enumerate(segments())]
    for s in ("ETHUSDT", "SOLUSDT"):
        st = cs.stats(s)
        tests.append((s[:3], s, st["min"], st["max"]))
    print("ST · 계단식 저점 수정 · 5m 롱 · 손절 전저점(20) · 익절 2.5R · 48봉 · gross 거래당 bp (거래 수) · net = taker 왕복 10bp 1배")
    rows = []
    for name, s, a, z in tests:
        b = resample(lib.load(s, start_ms=a, end_ms=z, with_funding=False)[0], 5)
        r, cvd, atr, lw, ok = prep(b)
        L, R, Cv = b.low.tolist(), r.tolist(), cvd.tolist()
        row = {}
        for m in "ABC":
            for ver, fn in (("현행", signals), ("수정", signals_stair)):
                idx = fn(L, R, Cv, m)
                idx = idx[ok[idx]]
                pr, pt, ps, _ = run(b, idx, lw, NR, T)
                net = (np.clip(1 + pr - 2 * FEE, 0, None).prod() - 1) * 100
                row[(m, ver)] = (len(pr), pr.mean() * 1e4, net, pt * 100, ps * 100)
        rows.append(row)
        print(f"  {name} 완료", flush=True)
    for m, lab in (("A", "A RSI 만"), ("B", "B RSI + CVD"), ("C", "C CVD 만")):
        print(f"\n  {lab}")
        print(f"    {'':5} " + " ".join(f"{t[0]:>22}" for t in tests))
        for ver in ("현행", "수정"):
            print(f"    {ver:5} " + " ".join(f"{r[(m, ver)][1]:+6.1f}bp ({r[(m, ver)][0]:4d}) {r[(m, ver)][2]:+6.0f}%" for r in rows))
        k = sum(r[(m, "수정")][1] > r[(m, "현행")][1] for r in rows)
        print(f"    → 수정이 높음 {k}/5 → {'✅ 수정이 낫다' if k >= 4 else '❌'}")


if __name__ == "__main__":
    main()
