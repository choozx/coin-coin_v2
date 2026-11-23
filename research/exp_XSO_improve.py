"""XSO — XS MOM 수익 늘리기 · 손실 줄이기, 아직 안 해 본 다섯 가지 (사용자: "수익 극대화하거나 손실 더 줄일 방법", 2026-11-23).

이미 기각(다시 하지 않음): 필터 3종(펀딩·변동성 조정·최근 제외) · 팩터 5개 · 주중 익절(XSTP) · 변동성 타기팅·베타 중립·폭락 필터(XSRK) · 보유 기간.

━━ 사전 고정 (2026-11-23 작성 · 결과 보기 전) ━━
틀      exp_XSD_weekday 일 단위 회계 · XS MOM 규칙(28일 · 상·하위 10% · 7일 보유 · 실제 펀딩 · 회전 0.10%) · 1배 · 7개 요일 각각.
수익 쪽
  BUF   버퍼: 리밸런스 때 들고 있던 롱(숏)이 상위(하위) 20% 안이면 유지, 빈자리는 상위(하위) 순서대로 채워 개수 k 그대로. 회전 비용을 줄인다.
  RW    순위 가중: 상·하위 10% 안에서 순위 r(1=가장 극단)에 가중 ∝ (k − r + 1), 다리 합 0.5 그대로. 극단에 더 싣는다.
손실 쪽 (일 종가 기준 — 실매매는 매시간 볼 수 있어 더 빨리 발동)
  SS25 · SS50  숏 손절: 숏 코인 종가가 그 주 진입가 대비 +25% / +50% 이상이면 그 종가에 정리(회전 비용), 다음 리밸런스까지 그 자리는 현금.
  CS25  코인 손절(양쪽): 롱 −25% 이하 · 숏 +25% 이상이면 정리.
판정
  수익 쪽(BUF·RW): ① 7요일 평균 연 ≥ 기본 ② 7요일 평균 샤프 ≥ 기본 ③ 낙폭 중앙이 기본보다 3%p 넘게 나빠지지 않음 ④ 7개 중 5개 이상 요일에서 샤프 ≥ 기본 — 넷 다.
  손실 쪽(SS·CS): XSRK 와 같다 — ① 낙폭 중앙 개선 ② 연 ≥ 기본의 90% ③ 샤프 ≥ 기본 ④ 5/7 요일 낙폭 개선 — 넷 다.
참고(판정 아님) 주당 회전 · 연 비용 · 손절 발동 수 · 비용 0.05%(지정가 체결 가정)일 때 기본 · 월요일만.

    .venv/bin/python -u research/exp_XSO_improve.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import exp_XS_cross_section as xs  # noqa: E402
from research.exp_XSD_weekday import monthly, stats  # noqa: E402

MODES = ("base", "BUF", "RW", "SS25", "SS50", "CS25")
PROFIT, LOSS = ("BUF", "RW"), ("SS25", "SS50", "CS25")


def tranche(days, C, Q, Fd, age, wd, mode="base", cost=xs.COST):
    """반환: 일 수익 배열 · 주당 회전 목록 · 손절 발동 수."""
    n, m = C.shape
    out = np.full(n, np.nan)
    i0 = max(int(np.searchsorted(days, xs.START)), 62)            # XSRK 와 같은 시작(기본 재현 확인용)
    while datetime.fromtimestamp(days[i0] / 1000, timezone.utc).weekday() != wd:
        i0 += 1
    w = np.zeros(m)
    turns, stops = [], 0
    stop = {"SS25": (None, 0.25), "SS50": (None, 0.50), "CS25": (0.25, 0.25)}.get(mode)
    for i in range(i0, n - 7, 7):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - 28]) & (age[i] >= xs.AGE)
        live &= np.nan_to_num(np.nanmedian(Q[i - 29:i + 1], axis=0)) >= xs.MINQ
        js = np.where(live)[0]
        nw = np.zeros(m)
        if len(js) >= 20:
            sig = C[i, js] / C[i - 28, js] - 1
            k = max(1, int(len(js) * 0.10))
            order = js[np.argsort(-sig)]                        # 강한 순
            if mode == "BUF":
                k2 = max(k, int(len(js) * 0.20))
                top2, bot2 = set(order[:k2]), set(order[-k2:])
                L = [j for j in order[:k2] if w[j] > 0][:k]
                for j in order:
                    if len(L) >= k:
                        break
                    if j not in L:
                        L.append(j)
                S = [j for j in order[::-1][:k2] if w[j] < 0 and j in bot2][:k]
                for j in order[::-1]:
                    if len(S) >= k:
                        break
                    if j not in S and j not in L:
                        S.append(j)
                assert all(j in top2 or w[j] == 0 for j in L)
                nw[L] = 0.5 / k
                nw[S] = -0.5 / k
            elif mode == "RW":
                g = np.arange(k, 0, -1, dtype=float)
                g = g / g.sum() * 0.5
                nw[order[:k]] = g                               # 가장 강한 것이 가장 크게
                nw[order[::-1][:k]] = -g                        # 가장 약한 것이 가장 크게
            else:
                nw[order[:k]] = 0.5 / k
                nw[order[-k:]] = -0.5 / k
        turn = np.abs(nw - w).sum()
        turns.append(turn)
        cur = nw.copy()
        entry = C[i].copy()
        for h in range(1, 8):
            d = i + h
            held = np.where(cur != 0)[0]
            pr = C[d, held] / C[d - 1, held] - 1
            pr = np.where(np.isfinite(pr), pr, 0.0)
            alive = np.isfinite(C[d, held])
            day = float((cur[held] * (pr - np.where(alive, Fd[d, held], 0.0))).sum())
            if h == 1:
                day -= turn * cost
            if stop and h < 7:                                  # 마지막 날은 어차피 리밸런스
                lo, hi = stop
                mv = C[d, held] / entry[held] - 1
                hit = np.zeros(len(held), bool)
                hit |= (cur[held] < 0) & np.isfinite(mv) & (mv >= hi)
                if lo is not None:
                    hit |= (cur[held] > 0) & np.isfinite(mv) & (mv <= -lo)
                if hit.any():
                    hj = held[hit]
                    day -= np.abs(cur[hj]).sum() * cost         # 정리 비용
                    cur[hj] = 0.0
                    stops += int(hit.sum())
            out[d] = day
        w = cur                                                 # 정리한 자리는 다음 리밸런스에 새로 진입(회전에 반영)
    return out, turns, stops


def main():
    days, syms, C, Q, Fd, age = xs.load()
    R, T, N = {}, {}, {}
    for md in MODES:
        for wd in range(7):
            R[(md, wd)], T[(md, wd)], N[(md, wd)] = tranche(days, C, Q, Fd, age, wd, md)
    for wd in range(7):
        R[("cost05", wd)], T[("cost05", wd)], N[("cost05", wd)] = tranche(days, C, Q, Fd, age, wd, "base", cost=0.0005)
    common = np.all([np.isfinite(v) for v in R.values()], axis=0)
    d = days[common]
    print(f"공통 구간 {datetime.fromtimestamp(d[0]/1000, timezone.utc):%Y-%m-%d} ~ {datetime.fromtimestamp(d[-1]/1000, timezone.utc):%Y-%m-%d} · "
          f"{len(d)}일 · 1배\n")
    S = {k: stats(v[common]) for k, v in R.items()}
    Wm = {k: monthly(d, v[common])[0] for k, v in R.items()}

    def agg(md):
        anns = np.array([S[(md, w)]["ann"] for w in range(7)])
        shs = np.array([S[(md, w)]["sharpe"] for w in range(7)])
        mdds = np.array([S[(md, w)]["mdd"] for w in range(7)])
        return anns, shs, mdds
    bA, bS, bM = agg("base")
    print(f"{'방법':7} | {'7요일 평균 연':>11} {'샤프':>6} {'낙폭 중앙':>9} {'최악 요일 낙폭':>11} {'최악의 달(중앙)':>12} | "
          f"{'샤프≥기본':>7} {'낙폭 개선':>7} | {'주당 회전':>7} {'연 비용':>7} {'손절':>5} | {'월요일만 연·낙폭':>16}")
    verdict = {}
    for md in MODES + ("cost05",):
        A, Sh, M = agg(md)
        sh_ok = int(sum(Sh[w] >= bS[w] for w in range(7)))
        dd_ok = int(sum(M[w] > bM[w] for w in range(7)))
        tv = np.mean([np.mean(T[(md, w)]) for w in range(7)])
        c = 0.0005 if md == "cost05" else xs.COST
        print(f"{md:7} | {A.mean()*100:+10.1f}% {Sh.mean():+6.2f} {np.median(M)*100:+8.1f}% {M.min()*100:+10.1f}% "
              f"{np.median([Wm[(md, w)] for w in range(7)])*100:+11.1f}% | {sh_ok if md != 'base' else '-':>5}/7 {dd_ok if md != 'base' else '-':>5}/7 | "
              f"{tv:7.2f} {tv*c*52*100:6.1f}% {np.mean([N[(md, w)] for w in range(7)]):5.0f} | "
              f"{S[(md, 0)]['ann']*100:+6.1f}% · {S[(md, 0)]['mdd']*100:+.1f}%")
        if md in PROFIT:
            verdict[md] = [A.mean() >= bA.mean(), Sh.mean() >= bS.mean(), np.median(M) >= np.median(bM) - 0.03, sh_ok >= 5]
        elif md in LOSS:
            verdict[md] = [np.median(M) > np.median(bM), A.mean() >= 0.9 * bA.mean(), Sh.mean() >= bS.mean(), dd_ok >= 5]
    print("\n■ 판정")
    print("  수익 쪽 ① 연 ≥ 기본 ② 샤프 ≥ 기본 ③ 낙폭 중앙 3%p 넘게 안 나빠짐 ④ 5/7 요일 샤프 ≥ 기본")
    print("  손실 쪽 ① 낙폭 중앙 개선 ② 연 ≥ 기본의 90% ③ 샤프 ≥ 기본 ④ 5/7 요일 낙폭 개선")
    for md, ok in verdict.items():
        print(f"  {md:5}: {'✅ 합격' if all(ok) else '❌'} [{''.join('○' if x else '×' for x in ok)}]")
    print("  (cost05 = 기본 규칙을 회전 비용 0.05% 로 — 지정가 체결이 되면 얻는 몫, 참고)")


if __name__ == "__main__":
    main()
