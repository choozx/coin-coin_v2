"""CV — CVD 다이버전스로 RSI 다이버전스 진입을 확인하면 거래당 bp 가 오르나 (5m, 롱).

요청(2026-10-14): DV 프리셋 뒤 "더 확실한 역추세 신호" → 추천 2번 CVD 다이버전스.
CVD = 누적(시장가 매수 − 시장가 매도) = cumsum(2 × taker_buy − volume). 가격이 아니라 **주문 흐름**이라 RSI 와 겹치지 않는 정보다.
CVD 다이버전스(강세) = 가격은 저점1 → 저점2 로 낮아졌는데 CVD(저점2) > CVD(저점1)
  = 두 저점 사이에 가격은 내렸어도 **시장가 매수가 매도보다 많았다**(흡수).

진입 규칙 셋(나머지는 DV 프리셋과 같다 — RSI(14) 30 아래 → 저점2 → 봉 마감 RSI 30 위 상승 마감):
  A  RSI 다이버전스만              (현행 프리셋 = 기준선)
  B  RSI 다이버전스 AND CVD 다이버전스 (확인용으로 겹침)
  C  CVD 다이버전스만(저점2 의 RSI 조건 대신 CVD)
청산(프리셋과 같다): 손절 = 최근 20봉 전저점 · 익절 = 손절폭 × 2.5 · 48봉 시간 청산 · 같은 봉이면 손절 먼저 · gross.
데이터: BTC 세 구간 + 안 본 ETH·SOL 5m 전 기간(2021-01~). 비교 기준: 같은 청산을 댄 고변동 랜덤 진입(EX 와 같다).
판정(결과 전 고정): B 또는 C 가 A 보다 거래당 gross bp 가 **다섯 시험(BTC 3 + ETH + SOL) 모두에서** 높고,
  그 값이 경제 문턱 12bp(taker 왕복 10bp + 여유) 이상이면 '쓸 만한 확인 신호'.

    python3 -u -m research.exp_CV_cvd_divergence
    python3 -u -m research.exp_CV_cvd_divergence --both   # 숏(약세 다이버전스) 추가 — 2026-10-14 사용자 "양방향이면?"

--both: 숏은 롱을 위아래로 뒤집은 것 — 가격 고점↑·RSI 고점↓(B·C 는 CVD(고점2) < CVD(고점1) = 두 고점 사이 매도 우세),
  봉 마감 RSI 70 아래 하락 마감에 숏, 손절 = 최근 20봉 전고점, 익절 = 손절폭 × 2.5, 48봉. 같은 판정 규칙을 숏·양방향(롱+숏 합산)에 적용.
"""
from __future__ import annotations

import sys

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                   # noqa: E402
from engine import indicators as ind                    # noqa: E402
from engine.candles import resample                     # noqa: E402
from research import lib                                # noqa: E402
from research.exp_EX2_swing_rr import run               # noqa: E402
from research.exp_T_supertrend_search import _ym, segments  # noqa: E402

LEVEL, GAP, WIN, NR, T, LB = 30.0, 5, 60, 2.5, 48, 20
FEE = 0.0005
MIN_BP = 12.0


def signals(low, r, cvd, mode):
    """DV 규칙(engine.indicators._rsi_div_long) + 저점2 판정만 mode 에 따라.

    mode 'A' 저점2 RSI > R1 · 'B' 그리고 CVD(저점2) > CVD(가격 저점1) · 'C' CVD 만(RSI 조건 없음).
    """
    out = []
    t1 = None
    P1 = R1 = P2 = None
    tP1 = tP2 = -1
    for t in range(1, len(low)):
        x = r[t]
        if x != x:
            continue
        if t1 is not None and t - t1 > WIN:
            t1 = None
        if t1 is None:
            if x < LEVEL:
                t1, P1, R1, P2, tP1, tP2 = t, low[t], x, None, t, -1
            continue
        ref = P1 if P2 is None else P2
        if low[t] < ref:
            rsi_ok = x > R1
            cvd_ok = cvd[t] > cvd[tP1]
            ok = rsi_ok if mode == "A" else (rsi_ok and cvd_ok) if mode == "B" else cvd_ok
            if t - t1 >= GAP and ok:
                P2, tP2 = low[t], t
            elif t - t1 < GAP:
                P1, R1, t1, tP1, P2 = low[t], min(R1, x), t, t, None
            else:
                P1, R1, t1, tP1, P2 = low[t], min(R1, x) if mode == "C" else x, t, t, None
            continue
        if x < R1:
            R1, t1 = x, t
            if mode != "C":
                P2 = None
            continue
        if P2 is not None and t > tP2 and x >= LEVEL and x > r[t - 1]:
            out.append(t)
            t1 = None
    return np.array(out, int)


def prep(b):
    n = len(b.close)
    r = ind.rsi(b.close, 14)
    cvd = np.cumsum(2 * b.taker_buy - b.volume)
    atr = ind.atr(b.high, b.low, b.close, 14) / b.close
    lw = np.full(n, np.nan)
    lw[LB - 1:] = swv(b.low, LB).min(axis=1)
    ok = np.isfinite(atr) & (np.arange(n) < n - T - 1) & (np.arange(n) > 200)
    return r, cvd, atr, lw, ok


def evaluate(b):
    r, cvd, atr, lw, ok = prep(b)
    L, R, Cv = b.low.tolist(), r.tolist(), cvd.tolist()
    res = {}
    for m in "ABC":
        idx = signals(L, R, Cv, m)
        idx = idx[ok[idx]]
        pr, pt, ps, _ = run(b, idx, lw, NR, T)
        net = (np.clip(1 + pr - 2 * FEE, 0, None).prod() - 1) * 100
        res[m] = (len(pr), pr.mean() * 1e4, pt * 100, ps * 100, net)
    pool = np.nonzero(ok)[0]
    hv = pool[atr[pool] >= np.percentile(atr[pool], 80)]
    rnd = np.random.default_rng(0).choice(hv, min(30000, len(hv)), replace=False)
    pr, *_ = run(b, rnd, lw, NR, T)
    res["고변동 랜덤"] = (len(pr), pr.mean() * 1e4, None, None, None)
    return res


def main():
    tests = []
    st = cs.stats("BTCUSDT")
    for i, (a, z) in enumerate(segments()):
        tests.append((f"BTC 구간{i+1} {_ym(a)}~{_ym(z)}", "BTCUSDT", a, z))
    for s in ("ETHUSDT", "SOLUSDT"):
        st = cs.stats(s)
        tests.append((f"{s[:3]} {_ym(st['min'])}~{_ym(st['max'])}", s, st["min"], st["max"]))
    print("CV · CVD 다이버전스 확인 · 5m 롱 · 손절 전저점(20) · 익절 2.5R · 48봉 · gross 거래당 bp")
    print("  A = RSI 다이버전스(현행) · B = RSI + CVD 다이버전스 · C = CVD 다이버전스만")
    rows = []
    for name, s, a, z in tests:
        b = resample(lib.load(s, start_ms=a, end_ms=z, with_funding=False)[0], 5)
        res = evaluate(b)
        rows.append(res)
        print(f"\n  {name}")
        for m in ("A", "B", "C"):
            n, bp, pt, ps, net = res[m]
            print(f"    {m}: 거래 {n:5d} · 거래당 {bp:+6.1f}bp · 익절 {pt:3.0f}% 손절 {ps:3.0f}% · net(taker 10bp, 1배) {net:+7.1f}%")
        print(f"    고변동 랜덤(같은 청산): 거래당 {res['고변동 랜덤'][1]:+6.1f}bp", flush=True)
    print("\n  판정(B·C 가 A 보다 다섯 시험 모두 높고 ≥ 12bp?)")
    for m in ("B", "C"):
        better = [r[m][1] > r["A"][1] for r in rows]
        enough = [r[m][1] >= MIN_BP for r in rows]
        print(f"    {m}: A 보다 높음 {sum(better)}/5 · 12bp 이상 {sum(enough)}/5 → "
              f"{'✅ 쓸 만한 확인 신호' if all(better) and all(enough) else '❌'}")


def run_short(b, idx, highN, n_r, T_):
    """숏 — EX2.run 의 거울. 손절 = 전고점, 익절 = 진입 − n × 손절폭."""
    off = idx[:, None] + np.arange(1, T_ + 1)[None, :]
    e = b.close[idx]
    stop = highN[idx]
    risk = stop - e
    ok = risk > 0
    e, stop, risk, off = e[ok], stop[ok], risk[ok], off[ok]
    tp = e - n_r * risk
    hs = b.high[off] >= stop[:, None]
    ht = b.low[off] <= tp[:, None]
    big = T_ + 1
    ts = np.where(hs.any(1), hs.argmax(1), big)
    tt = np.where(ht.any(1), ht.argmax(1), big)
    px = b.close[off[:, -1]].copy()
    m_s = (ts <= tt) & (ts < big)
    m_t = tt < ts
    px[m_s] = stop[m_s]
    px[m_t] = tp[m_t]
    return -(px / e - 1), m_t.mean(), m_s.mean()


def evaluate_both(b):
    r, cvd, atr, lw, ok = prep(b)
    n = len(b.close)
    hw = np.full(n, np.nan)
    hw[LB - 1:] = swv(b.high, LB).max(axis=1)
    L, R, Cv = b.low.tolist(), r.tolist(), cvd.tolist()
    nH, Rm, nCv = (-b.high).tolist(), [100 - x if x == x else x for x in R], (-cvd).tolist()
    res = {}
    for m in "ABC":
        il = signals(L, R, Cv, m); il = il[ok[il]]
        ish = signals(nH, Rm, nCv, m); ish = ish[ok[ish]]
        pl, *_ = run(b, il, lw, NR, T)
        ps_, pt, pss = run_short(b, ish, hw, NR, T)
        both = np.concatenate([pl, ps_])
        res[m] = {"롱": (len(pl), pl.mean() * 1e4), "숏": (len(ps_), ps_.mean() * 1e4),
                  "양방향": (len(both), both.mean() * 1e4)}
    return res


def main_both():
    tests = []
    for i, (a, z) in enumerate(segments()):
        tests.append((f"BTC 구간{i+1}", "BTCUSDT", a, z))
    for s in ("ETHUSDT", "SOLUSDT"):
        st = cs.stats(s)
        tests.append((s[:3], s, st["min"], st["max"]))
    print("CV --both · 롱·숏·양방향 · 5m · 손절 전저/전고점(20) · 익절 2.5R · 48봉 · gross 거래당 bp (거래 수)")
    rows = []
    for name, s, a, z in tests:
        b = resample(lib.load(s, start_ms=a, end_ms=z, with_funding=False)[0], 5)
        rows.append(evaluate_both(b))
        print(f"  {name} 완료", flush=True)
    for m, lab in (("A", "A RSI 다이버전스"), ("B", "B RSI + CVD"), ("C", "C CVD 만")):
        print(f"\n  {lab}")
        print(f"    {'':6} " + " ".join(f"{t[0]:>16}" for t in tests))
        for side in ("롱", "숏", "양방향"):
            print(f"    {side:6} " + " ".join(f"{r[m][side][1]:+8.1f} ({r[m][side][0]:5d})" for r in rows))
    print("\n  판정(숏·양방향이 다섯 시험 모두 ≥ 12bp?)")
    for m in "BC":
        for side in ("숏", "양방향"):
            k = sum(r[m][side][1] >= MIN_BP for r in rows)
            print(f"    {m} {side}: 12bp 이상 {k}/5 → {'✅' if k == 5 else '❌'}")


if __name__ == "__main__":
    main_both() if "--both" in sys.argv else main()
