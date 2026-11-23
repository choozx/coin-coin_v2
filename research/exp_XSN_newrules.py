"""XSN — XS MOM 에 새 규칙 넷: 고점 근접 · 꾸준한 추세 · 분산 크기 조절 · 전략 자체 모멘텀 (사용자: "새로운 규칙을 넣어볼 수 없어?", 2026-11-23).

이미 기각(다시 하지 않음): 필터 3종 · 팩터 5개(단독) · 익절 · VT · 베타 중립 · 폭락 필터 · 보유 기간 · 버퍼 · 순위 가중 · 손절 · 기간 섞기 · 변동성 역가중 · 큰 코인만.

━━ 사전 고정 (2026-11-23 작성 · 결과 보기 전) ━━
틀      exp_XSP_other 와 같다(XSD 일 단위 회계 · 28일 · 상·하위 10% · 7일 보유 · 실제 펀딩 · 회전 0.10% · 1배 기준 · 7개 요일 각각).
고르는 규칙
  HI   고점 근접(George–Hwang): 점수 = 28일 수익률 순위 백분위 + 고점 근접(종가 / 직전 28일 최고 종가) 순위 백분위의 평균. 상·하위 10%.
  FIP  꾸준한 추세(Da–Gurun–Warachka 'frog in the pan'): 28일 수익률 상위(하위) 20% 후보 안에서
       ID = sign(28일 수익률) × (하락일 비율 − 상승일 비율) 이 작은(= 같은 방향으로 꾸준히 움직인) k 개.
크기 규칙(코인은 그대로, 그 주 노출 배수만)
  DISP 횡단면 분산: 리밸런스 날 대상 코인 28일 수익률의 표준편차가 그 몫의 지난 리밸런스들 확장 중앙값보다 크면 ×1.5, 작으면 ×0.5
       (지난 기록 8회 전엔 ×1). 모멘텀은 코인끼리 차이가 클 때 잘 된다는 가설.
  SMOM 전략 자체 모멘텀: 그 몫의 직전 28일 기본 전략 수익(배수 전)이 음수면 ×0.5, 아니면 ×1.
판정  XSP 와 같다 — ① 7요일 평균 샤프 ≥ 기본 ② 연 ≥ 기본의 90% ③ 낙폭 중앙 3%p 넘게 안 나빠짐 ④ 5/7 요일 샤프 ≥ 기본
      ⑤ 요일 분산(7몫 평균) 샤프 ≥ 기본 — 다섯 다.
참고(판정 아님) 평균 노출 배수 · 주당 회전 · 반기별 · 월요일만.

    .venv/bin/python -u research/exp_XSN_newrules.py
"""
from __future__ import annotations

import os
import sys
import warnings
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import exp_XS_cross_section as xs  # noqa: E402
from research.exp_XSD_weekday import stats  # noqa: E402
from research.exp_XSP_other import pct_rank  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning)
MODES = ("base", "HI", "FIP", "DISP", "SMOM")


def tranche(days, C, Q, Fd, age, wd, mode="base"):
    n, m = C.shape
    out = np.full(n, np.nan)
    raw = np.full(n, np.nan)                                    # 배수 전 일 수익(SMOM 용)
    i0 = max(int(np.searchsorted(days, xs.START)), 62)
    while datetime.fromtimestamp(days[i0] / 1000, timezone.utc).weekday() != wd:
        i0 += 1
    w = np.zeros(m)
    turns, scales, disps = [], [], []
    for i in range(i0, n - 7, 7):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - 28]) & (age[i] >= xs.AGE)
        live &= np.nan_to_num(np.nanmedian(Q[i - 29:i + 1], axis=0)) >= xs.MINQ
        js = np.where(live)[0]
        nw = np.zeros(m)
        scale = 1.0
        if len(js) >= 20:
            r28 = C[i, js] / C[i - 28, js] - 1
            k = max(1, int(len(js) * 0.10))
            if mode == "HI":
                hi = C[i, js] / np.nanmax(C[i - 27:i + 1, js], axis=0)
                sig = (pct_rank(r28) + pct_rank(hi)) / 2
                order = js[np.argsort(-sig)]
                L, S = order[:k], order[::-1][:k]
            elif mode == "FIP":
                order = js[np.argsort(-r28)]
                k2 = max(k, int(len(js) * 0.20))
                dr = C[i - 27:i + 1] / C[i - 28:i] - 1
                pos = np.nanmean(dr > 0, axis=0)
                neg = np.nanmean(dr < 0, axis=0)
                mom = C[i] / C[i - 28] - 1
                ID = np.sign(mom) * (neg - pos)
                Lp, Sp = order[:k2], order[::-1][:k2]
                L = Lp[np.argsort(ID[Lp], kind="stable")][:k]
                S = Sp[np.argsort(ID[Sp], kind="stable")][:k]
            else:
                order = js[np.argsort(-r28)]
                L, S = order[:k], order[::-1][:k]
            nw[L] = 0.5 / k
            nw[S] = -0.5 / k
            if mode == "DISP":
                dsp = float(np.nanstd(r28))
                if len(disps) >= 8:
                    scale = 1.5 if dsp > np.median(disps) else 0.5
                disps.append(dsp)
            elif mode == "SMOM":
                past = raw[i - 27:i + 1]
                past = past[np.isfinite(past)]
                if len(past) >= 28 and past.sum() < 0:
                    scale = 0.5
        nw *= scale
        scales.append(scale)
        turn = np.abs(nw - w).sum()
        turns.append(turn)
        held = np.where(nw != 0)[0]
        for h in range(1, 8):
            d = i + h
            pr = C[d, held] / C[d - 1, held] - 1
            pr = np.where(np.isfinite(pr), pr, 0.0)
            alive = np.isfinite(C[d, held])
            day = float((nw[held] * (pr - np.where(alive, Fd[d, held], 0.0))).sum())
            day -= turn * xs.COST if h == 1 else 0.0
            out[d] = day
            raw[d] = day / scale if scale else 0.0
        w = nw
    return out, turns, scales


def main():
    days, syms, C, Q, Fd, age = xs.load()
    R, T, X = {}, {}, {}
    for md in MODES:
        for wd in range(7):
            R[(md, wd)], T[(md, wd)], X[(md, wd)] = tranche(days, C, Q, Fd, age, wd, md)
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
          f"{'노출':>5} {'회전':>5} | {'요일분산 연·샤프·낙폭':>22} | {'월요일만':>14}")
    verdict = {}
    for md in MODES:
        A, Sh, M = agg(md)
        ok = int(sum(Sh[w] >= bS[w] for w in range(7)))
        print(f"{md:5} | {A.mean()*100:+7.1f}% {Sh.mean():5.2f} {np.median(M)*100:+7.1f}% {M.min()*100:+7.1f}% | "
              f"{ok if md != 'base' else '-':>4}/7 | {np.mean([np.mean(X[(md, w)]) for w in range(7)]):5.2f} "
              f"{np.mean([np.mean(T[(md, w)]) for w in range(7)]):5.2f} | "
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
    for md in MODES:
        r = np.mean([R[(md, w)][common] for w in range(7)], axis=0)
        by = {}
        for x, t in zip(r, d):
            dt = datetime.fromtimestamp(t / 1000, timezone.utc)
            key = f"{dt.year}H{1 if dt.month <= 6 else 2}"
            by[key] = by.get(key, 0.0) + x
        print(f"  {md:4} " + " ".join(f"{k} {v*100:+6.1f}%" for k, v in by.items()))


if __name__ == "__main__":
    main()
