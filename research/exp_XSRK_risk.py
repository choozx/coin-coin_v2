"""XSRK — XS MOM 낙폭 줄이기 세 가지: 변동성 타기팅 · 베타 중립 · 폭락 반등 필터 (사용자: "연수익 유지하면서 낙폭 줄이기", 2026-11-23).

━━ 사전 고정 (2026-11-23 작성 · 결과 보기 전) ━━
틀      exp_XSD_weekday 일 단위 회계 · XS MOM 규칙(28일 · 상·하위 10% · 7일 보유 · 펀딩 · 회전 0.10%) · 1배 기준 · 7개 요일 각각.
VT  변동성 타기팅: 매 리밸런스에 노출 배수 = 목표 변동성 / 최근 60일 전략 일 수익 변동성(기본 전략 기준, 그날까지만).
        목표 = 그날까지의 '최근 60일 변동성'들의 확장 중앙값(미래 정보 없음 · 평균 노출이 1 근처가 되게). 120일 전엔 1. 배수는 0.5 ~ 2.0 으로 자름.
BN  베타 중립: 코인별 β = 직전 60일 일 수익을 BTC 에 회귀. 롱 다리 0.5 는 그대로, 숏 다리 = 0.5 × β롱평균 / β숏평균(0.25 ~ 1.0 으로 자름),
        그 뒤 총 노출(롱 + 숏)이 1 이 되게 두 다리를 같은 비율로 맞춤.
CF  폭락 반등 필터: 리밸런스 날 BTC 90일 수익 < 0 이고 BTC 30일 일 수익 변동성이 그날까지의 확장 중앙값보다 크면(약세 + 고변동) 그 주 노출 0.5.
판정(방법별) ① 낙폭 개선: 7요일 최대낙폭 중앙이 기본보다 작다 ② 수익 유지: 7요일 평균 연 수익 ≥ 기본 평균의 90%
        ③ 샤프: 7요일 평균 샤프 ≥ 기본 평균 ④ 요일 견고: 7개 중 5개 이상에서 최대낙폭이 기본보다 작다 — 넷 다.
참고    평균 노출 배수(VT·CF) · 최악의 달 · 월요일만 · VT+BN · VT+CF 조합(판정 아님).

    .venv/bin/python -u research/exp_XSRK_risk.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import exp_XS_cross_section as xs  # noqa: E402
from research.exp_XSD_weekday import monthly, stats  # noqa: E402

D = xs.D


def betas(C, i, js, bj, look=60):
    r = C[i - look:i + 1] / C[i - look - 1:i] - 1
    b = r[:, bj]
    x = r[:, js]
    ok = np.isfinite(x) & np.isfinite(b)[:, None]
    bb = np.where(ok, b[:, None], np.nan)
    xx = np.where(ok, x, np.nan)
    cov = np.nanmean((xx - np.nanmean(xx, 0)) * (bb - np.nanmean(bb, 0)), 0)
    return cov / np.nanvar(bb, 0)


def tranche(days, C, Q, Fd, age, wd, bj, mode="base", base_daily=None, btc_state=None, cost=xs.COST):
    """반환: 일 수익 배열 · 주별 노출 배수 목록."""
    n, m = C.shape
    out = np.full(n, np.nan)
    i = max(int(np.searchsorted(days, xs.START)), 62)
    while datetime.fromtimestamp(days[i] / 1000, timezone.utc).weekday() != wd:
        i += 1
    w = np.zeros(m)
    scales = []
    vols_hist = []
    for i in range(i, n - 7, 7):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - 28]) & (age[i] >= xs.AGE)
        live &= np.nan_to_num(np.nanmedian(Q[i - 29:i + 1], axis=0)) >= xs.MINQ
        js = np.where(live)[0]
        nw = np.zeros(m)
        if len(js) >= 20:
            sig = C[i, js] / C[i - 28, js] - 1
            k = max(1, int(len(js) * 0.10))
            order = np.argsort(-sig)
            L, S = js[order[:k]], js[order[-k:]]
            wl, ws = 0.5, 0.5
            if mode in ("BN", "VT+BN"):
                bL, bS = betas(C, i, L, bj), betas(C, i, S, bj)
                mL, mS = np.nanmean(bL), np.nanmean(bS)
                if np.isfinite(mL) and np.isfinite(mS) and mS > 0:
                    ws = float(np.clip(0.5 * mL / mS, 0.25, 1.0))
                g = wl + ws
                wl, ws = wl / g, ws / g
            nw[L] = wl / k
            nw[S] = -ws / k
        scale = 1.0
        if mode in ("VT", "VT+BN", "VT+CF") and base_daily is not None:
            past = base_daily[max(0, i - 59):i + 1]
            past = past[np.isfinite(past)]
            if len(past) >= 40:
                v = past.std(ddof=1)
                vols_hist.append(v)
                if len(vols_hist) >= 17 and v > 0:            # 약 120일(주 17개) 쌓인 뒤부터
                    scale = float(np.clip(np.median(vols_hist) / v, 0.5, 2.0))
        if mode in ("CF", "VT+CF") and btc_state is not None and btc_state[i]:
            scale *= 0.5
        nw = nw * scale
        scales.append(scale)
        turn = np.abs(nw - w).sum()
        held = np.where(nw != 0)[0]
        for h in range(1, 8):
            d = i + h
            pr = C[d, held] / C[d - 1, held] - 1
            pr = np.where(np.isfinite(pr), pr, 0.0)
            alive = np.isfinite(C[d, held])
            out[d] = float((nw[held] * (pr - np.where(alive, Fd[d, held], 0.0))).sum()) - (turn * cost if h == 1 else 0.0)
        w = nw
    return out, scales


def btc_bear_highvol(days, C, bj):
    n = len(days)
    st = np.zeros(n, bool)
    vh = []
    for i in range(91, n):
        r90 = C[i, bj] / C[i - 90, bj] - 1
        rr = C[i - 29:i + 1, bj] / C[i - 30:i, bj] - 1
        v = np.nanstd(rr)
        vh.append(v)
        st[i] = (r90 < 0) and (v > np.median(vh))
    return st


def main():
    days, syms, C, Q, Fd, age = xs.load()
    bj = syms.index("BTCUSDT")
    bstate = btc_bear_highvol(days, C, bj)
    modes = ("base", "VT", "BN", "CF", "VT+BN", "VT+CF")
    R, SC = {}, {}
    for wd in range(7):
        R[("base", wd)], SC[("base", wd)] = tranche(days, C, Q, Fd, age, wd, bj, "base")
    for md in modes[1:]:
        for wd in range(7):
            R[(md, wd)], SC[(md, wd)] = tranche(days, C, Q, Fd, age, wd, bj, md, base_daily=R[("base", wd)], btc_state=bstate)
    common = np.all([np.isfinite(R[(md, wd)]) for md in modes for wd in range(7)], axis=0)
    d = days[common]
    print(f"공통 구간 {datetime.fromtimestamp(d[0]/1000, timezone.utc):%Y-%m-%d} ~ {datetime.fromtimestamp(d[-1]/1000, timezone.utc):%Y-%m-%d} · "
          f"{len(d)}일 · 1배 기준 · BTC 약세+고변동 주 비율 {bstate[common].mean():.0%}\n")
    S = {k: stats(v[common]) for k, v in R.items()}
    W = {k: monthly(d, v[common])[0] for k, v in R.items()}
    base = dict(ann=np.mean([S[("base", w)]["ann"] for w in range(7)]), sh=np.mean([S[("base", w)]["sharpe"] for w in range(7)]),
                mdd=np.median([S[("base", w)]["mdd"] for w in range(7)]))
    print(f"{'방법':7} | {'7요일 평균 연':>11} {'샤프':>6} {'낙폭 중앙':>9} {'최악 요일 낙폭':>11} {'최악의 달(중앙)':>12} | {'낙폭 개선 요일':>9} | "
          f"{'평균 노출':>7} | {'월요일만 연·낙폭':>16}")
    verdict = {}
    for md in modes:
        anns = np.array([S[(md, w)]["ann"] for w in range(7)]); shs = np.array([S[(md, w)]["sharpe"] for w in range(7)])
        mdds = np.array([S[(md, w)]["mdd"] for w in range(7)])
        better = int(sum(S[(md, w)]["mdd"] > S[("base", w)]["mdd"] for w in range(7)))
        expo = np.mean([np.mean(SC[(md, w)]) for w in range(7)])
        print(f"{md:7} | {anns.mean()*100:+10.1f}% {shs.mean():+6.2f} {np.median(mdds)*100:+8.1f}% {mdds.min()*100:+10.1f}% "
              f"{np.median([W[(md, w)] for w in range(7)])*100:+11.1f}% | {better if md != 'base' else '-':>6}/7  | {expo:6.2f} | "
              f"{S[(md, 0)]['ann']*100:+6.1f}% · {S[(md, 0)]['mdd']*100:+.1f}%")
        if md in ("VT", "BN", "CF"):
            verdict[md] = [np.median(mdds) > base["mdd"], anns.mean() >= 0.9 * base["ann"], shs.mean() >= base["sh"], better >= 5]
    print("\n■ 판정(① 낙폭 중앙 개선 · ② 연 ≥ 기본의 90% · ③ 샤프 ≥ 기본 · ④ 5/7 요일 낙폭 개선)")
    for md, ok in verdict.items():
        print(f"  {md:3}: {'✅ 합격' if all(ok) else '❌'} [{''.join('○' if x else '×' for x in ok)}]")
    print("  (VT+BN · VT+CF 는 참고 — 판정 아님)")


if __name__ == "__main__":
    main()
