"""XSP — XS MOM 다른 방향 세 가지: 기간 섞기 · 변동성 역가중 · 큰 코인만 (사용자: "다른 쪽으로 다시 알아봐", 2026-11-23).

XSO 까지 기각(다시 하지 않음): 필터 3종 · 팩터 5개 · 익절 · VT · 베타 중립 · 폭락 필터 · 보유 기간 · 버퍼 · 순위 가중 · 손절.
이번엔 '어떤 코인을 고르나(규칙)'가 아니라 ① 신호를 한 기간에 기대는 운 ② 코인별 크기 ③ 대상 범위를 바꾼다.

━━ 사전 고정 (2026-11-23 작성 · 결과 보기 전) ━━
틀      exp_XSO_improve 와 같다(XSD 일 단위 회계 · 28일 · 상·하위 10% · 7일 보유 · 실제 펀딩 · 회전 0.10% · 1배 · 7개 요일 각각 · 시작 = XSRK 와 같음).
LB   기간 섞기: 14·28·56일 수익률 각각의 순위 백분위를 평균해 순위(56일 값이 없는 코인은 있는 기간만 평균). 개수 k·가중은 그대로.
       28일 하나에 기대는 '기간 운'을 줄인다(Monday 견고성: 기간 7~90일 전부 +).
IV   변동성 역가중: 고른 코인(그대로) 안에서 가중 ∝ 1 / 직전 30일 일 수익 표준편차, 다리 합 0.5 그대로. 가장 날뛰는 코인 몫을 줄인다.
Q20  큰 코인만: 30일 거래대금 중앙 ≥ $20M(기본 $5M). 나머지 그대로. 실체결 비용·용량이 나은 쪽.
판정  ① 7요일 평균 샤프 ≥ 기본 ② 7요일 평균 연 ≥ 기본의 90% ③ 낙폭 중앙이 기본보다 3%p 넘게 나빠지지 않음
      ④ 7개 중 5개 이상 요일에서 샤프 ≥ 기본 ⑤ 요일 분산(7몫 평균) 샤프 ≥ 기본의 요일 분산 샤프 — 다섯 다(실매매는 요일 분산으로 갈 예정이라 ⑤ 추가).
참고(판정 아님) 대상 코인 수 중앙 · 주당 회전 · 요일 분산 연·낙폭 · 월요일만.

    .venv/bin/python -u research/exp_XSP_other.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import exp_XS_cross_section as xs  # noqa: E402
from research.exp_XSD_weekday import stats  # noqa: E402

MODES = ("base", "LB", "IV", "Q20")


def pct_rank(x):
    """작을수록 0, 클수록 1. nan 은 nan."""
    out = np.full(len(x), np.nan)
    ok = np.isfinite(x)
    if ok.sum() > 1:
        r = np.argsort(np.argsort(x[ok]))
        out[ok] = r / (ok.sum() - 1)
    return out


def tranche(days, C, Q, Fd, age, wd, mode="base"):
    n, m = C.shape
    out = np.full(n, np.nan)
    i0 = max(int(np.searchsorted(days, xs.START)), 62)
    while datetime.fromtimestamp(days[i0] / 1000, timezone.utc).weekday() != wd:
        i0 += 1
    minq = 20e6 if mode == "Q20" else xs.MINQ
    w = np.zeros(m)
    turns, nuni = [], []
    for i in range(i0, n - 7, 7):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - 28]) & (age[i] >= xs.AGE)
        live &= np.nan_to_num(np.nanmedian(Q[i - 29:i + 1], axis=0)) >= minq
        js = np.where(live)[0]
        nuni.append(len(js))
        nw = np.zeros(m)
        if len(js) >= 20:
            if mode == "LB":
                with np.errstate(invalid="ignore", divide="ignore"):
                    rs = [pct_rank(C[i, js] / C[i - L, js] - 1) for L in (14, 28, 56)]
                sig = np.nanmean(np.vstack(rs), axis=0)
            else:
                sig = C[i, js] / C[i - 28, js] - 1
            k = max(1, int(len(js) * 0.10))
            order = js[np.argsort(-sig)]
            L, S = order[:k], order[::-1][:k]
            if mode == "IV":
                r = C[i - 29:i + 1] / C[i - 30:i] - 1
                for leg, sgn in ((L, 1), (S, -1)):
                    v = np.nanstd(r[:, leg], axis=0, ddof=1)
                    v = np.where(np.isfinite(v) & (v > 1e-4), v, np.nanmedian(v))
                    g = 1 / v
                    nw[leg] = sgn * 0.5 * g / g.sum()
            else:
                nw[L] = 0.5 / k
                nw[S] = -0.5 / k
        turn = np.abs(nw - w).sum()
        turns.append(turn)
        held = np.where(nw != 0)[0]
        for h in range(1, 8):
            d = i + h
            pr = C[d, held] / C[d - 1, held] - 1
            pr = np.where(np.isfinite(pr), pr, 0.0)
            alive = np.isfinite(C[d, held])
            day = float((nw[held] * (pr - np.where(alive, Fd[d, held], 0.0))).sum())
            out[d] = day - (turn * xs.COST if h == 1 else 0.0)
        w = nw
    return out, turns, nuni


def main():
    days, syms, C, Q, Fd, age = xs.load()
    R, T, U = {}, {}, {}
    for md in MODES:
        for wd in range(7):
            R[(md, wd)], T[(md, wd)], U[(md, wd)] = tranche(days, C, Q, Fd, age, wd, md)
    common = np.all([np.isfinite(v) for v in R.values()], axis=0)
    d = days[common]
    print(f"공통 구간 {datetime.fromtimestamp(d[0]/1000, timezone.utc):%Y-%m-%d} ~ "
          f"{datetime.fromtimestamp(d[-1]/1000, timezone.utc):%Y-%m-%d} · {len(d)}일 · 1배\n")
    S = {k: stats(v[common]) for k, v in R.items()}
    P = {md: stats(np.mean([R[(md, w)][common] for w in range(7)], axis=0)) for md in MODES}

    def agg(md):
        return (np.array([S[(md, w)][x] for w in range(7)]) for x in ("ann", "sharpe", "mdd"))
    bA, bS, bM = agg("base")
    print(f"{'방법':5} | {'평균 연':>8} {'샤프':>5} {'낙폭 중앙':>8} {'최악 요일':>8} | {'샤프≥기본':>6} | "
          f"{'대상 수':>6} {'회전':>5} | {'요일분산 연·샤프·낙폭':>22} | {'월요일만':>14}")
    verdict = {}
    for md in MODES:
        A, Sh, M = agg(md)
        ok = int(sum(Sh[w] >= bS[w] for w in range(7)))
        print(f"{md:5} | {A.mean()*100:+7.1f}% {Sh.mean():5.2f} {np.median(M)*100:+7.1f}% {M.min()*100:+7.1f}% | "
              f"{ok if md != 'base' else '-':>4}/7 | {np.median(U[(md, 0)]):6.0f} {np.mean([np.mean(T[(md, w)]) for w in range(7)]):5.2f} | "
              f"{P[md]['ann']*100:+6.1f}% {P[md]['sharpe']:5.2f} {P[md]['mdd']*100:+6.1f}% | "
              f"{S[(md, 0)]['ann']*100:+6.1f}% {S[(md, 0)]['mdd']*100:+.1f}%")
        if md != "base":
            verdict[md] = [Sh.mean() >= bS.mean(), A.mean() >= 0.9 * bA.mean(), np.median(M) >= np.median(bM) - 0.03,
                           ok >= 5, P[md]["sharpe"] >= P["base"]["sharpe"]]
    print("\n■ 판정 ① 샤프 ≥ 기본 ② 연 ≥ 기본의 90% ③ 낙폭 중앙 3%p 넘게 안 나빠짐 ④ 5/7 요일 샤프 ≥ 기본 ⑤ 요일 분산 샤프 ≥ 기본")
    for md, v in verdict.items():
        print(f"  {md:4}: {'✅ 합격' if all(v) else '❌'} [{''.join('○' if x else '×' for x in v)}]")
    print("\n■ 요일별 샤프(월~일)")
    for md in MODES:
        print(f"  {md:4} " + " ".join(f"{S[(md, w)]['sharpe']:5.2f}" for w in range(7)))
    print("■ 반기별 수익(요일 분산, 단순 합)")
    halves = {}
    for md in MODES:
        r = np.mean([R[(md, w)][common] for w in range(7)], axis=0)
        by = {}
        for x, t in zip(r, d):
            dt = datetime.fromtimestamp(t / 1000, timezone.utc)
            key = f"{dt.year}H{1 if dt.month <= 6 else 2}"
            by[key] = by.get(key, 0.0) + x
        halves[md] = by
        print(f"  {md:4} " + " ".join(f"{k} {v*100:+6.1f}%" for k, v in by.items()))


if __name__ == "__main__":
    main()
