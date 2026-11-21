"""XSD — XS MOM 리밸런스 요일 분산: 자금을 나눠 서로 다른 요일에 리밸런스 (사용자, 2026-11-21).

XS2 부수 발견: 같은 데이터에서 리밸런스 요일만 바뀌어도 MOM 최대낙폭이 −21%(월) ↔ −48% 로 갈린다. 월요일은 7개 중 최고라 운이 섞였다.
자금을 K 개 몫(트랜치)으로 나눠 각 몫이 다른 요일에 리밸런스하면 '요일 운'이 평균화된다 — 실매매 기대치를 정직하게 보고 낙폭을 줄이는지 본다.

━━ 사전 고정 (2026-11-21 작성 · 결과 보기 전) ━━
틀      exp_XS_cross_section 과 같다(USDT 무기한 654 · 2024-01 ~ 2026-11-16 · 상장 ≥ 30일 · 거래대금 ≥ $5M · 28일 수익률 상·하위 10% · 롱 0.5 · 숏 0.5 ·
        7일 보유 · 실제 펀딩 · 회전당 0.10% · 폐지면 마지막 종가). 회계만 **일 단위 평가**(트랜치를 섞으려면 같은 날짜 축이 필요).
        각 트랜치는 자기 요일 종가에 리밸런스하고 7일 동안 가중치 고정(가중치 표류 무시 — XS 와 같음). 포트폴리오 = 트랜치 일 수익의 평균.
비교    S_k = 요일 k 하나(7개) · P2 = 월+목 · P7 = 7개 요일 전부(매일 1/7 씩 교체).
보고    연 수익(일 평균 × 365) · 연 샤프 · 최대낙폭 · 최악의 달 · 이익 달 비율 · 연 회전 비용.
판단    (사전) ① 단일 요일 7개의 평균을 '월요일만'의 정직한 기대치로 본다(월요일은 사후 최고).
        ② P7 의 최대낙폭이 단일 요일 낙폭의 중앙보다 작고 ③ 샤프가 단일 요일 샤프의 중앙 이상이면 → 실매매를 트랜치 분할로 바꿀 근거.
        P2 는 '운영 부담이 작은 절충안'으로 같은 기준을 본다.

    .venv/bin/python -u research/exp_XSD_weekday.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import exp_XS_cross_section as xs  # noqa: E402

D = xs.D
NAMES = ("월", "화", "수", "목", "금", "토", "일")


def tranche(days, C, Q, Fd, age, wd, cost=xs.COST):
    """요일 wd(0=월) 리밸런스 트랜치의 일 수익 배열(len(days)), 시작 전·끝 뒤는 nan."""
    n, m = C.shape
    out = np.full(n, np.nan)
    i0 = int(np.searchsorted(days, xs.START))
    w = np.zeros(m)
    i = i0
    while datetime.fromtimestamp(days[i] / 1000, timezone.utc).weekday() != wd:
        i += 1
    first = i
    for i in range(first, n - 7, 7):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - 28]) & (age[i] >= xs.AGE)
        live &= np.nan_to_num(np.nanmedian(Q[i - 29:i + 1], axis=0)) >= xs.MINQ
        js = np.where(live)[0]
        nw = np.zeros(m)
        if len(js) >= 20:
            sig = C[i, js] / C[i - 28, js] - 1
            k = max(1, int(len(js) * 0.10))
            order = np.argsort(-sig)
            nw[js[order[:k]]] = 0.5 / k
            nw[js[order[-k:]]] = -0.5 / k
        turn = np.abs(nw - w).sum()
        held = np.where(nw != 0)[0]
        for h in range(1, 8):
            d = i + h
            pr = C[d, held] / C[d - 1, held] - 1                 # 폐지 뒤엔 nan → 0(마지막 종가로 고정)
            pr = np.where(np.isfinite(pr), pr, 0.0)
            alive = np.isfinite(C[d, held])
            day = float((nw[held] * (pr - np.where(alive, Fd[d, held], 0.0))).sum())
            out[d] = day - (turn * cost if h == 1 else 0.0)
        w = nw
    return out


def stats(r):
    r = r[np.isfinite(r)]
    eq = np.cumprod(1 + r)
    mdd = (eq / np.maximum.accumulate(eq) - 1).min()
    return dict(ann=r.mean() * 365, sharpe=r.mean() / r.std(ddof=1) * np.sqrt(365), mdd=mdd, n=len(r), eq=eq)


def monthly(days, r):
    by = {}
    for d, x in zip(days, r):
        if np.isfinite(x):
            k = datetime.fromtimestamp(d / 1000, timezone.utc).strftime("%Y-%m")
            by[k] = by.get(k, 1.0) * (1 + x)
    v = np.array([x - 1 for x in by.values()])
    return v.min(), np.mean(v > 0), len(v)


def line(name, days, r):
    s = stats(r)
    worst, winm, nm = monthly(days, r)
    return s, (f"  {name:14} 연 {s['ann']*100:+6.1f}% · 샤프 {s['sharpe']:+.2f} · 최대낙폭 {s['mdd']*100:+6.1f}% · "
               f"최악의 달 {worst*100:+6.1f}% · 이익 달 {winm:.0%}({nm}개월)")


def main():
    days, syms, C, Q, Fd, age = xs.load()
    T = {wd: tranche(days, C, Q, Fd, age, wd) for wd in range(7)}
    common = np.all([np.isfinite(T[wd]) for wd in range(7)], axis=0)    # 일곱 트랜치가 모두 돌아가는 날만(공정 비교)
    d = days[common]
    print(f"공통 구간 {datetime.fromtimestamp(d[0]/1000, timezone.utc):%Y-%m-%d} ~ {datetime.fromtimestamp(d[-1]/1000, timezone.utc):%Y-%m-%d} · {len(d)}일\n")
    print("■ 단일 요일(각 요일 종가에 리밸런스, 자금 전부)")
    single = {}
    for wd in range(7):
        s, txt = line(f"{NAMES[wd]}요일만", d, T[wd][common])
        single[wd] = s
        print(txt)
    anns = np.array([single[w]["ann"] for w in range(7)]); shs = np.array([single[w]["sharpe"] for w in range(7)])
    mdds = np.array([single[w]["mdd"] for w in range(7)])
    print(f"  {'단일 요일 평균':14} 연 {anns.mean()*100:+6.1f}% · 샤프 {shs.mean():+.2f} · 최대낙폭 중앙 {np.median(mdds)*100:+.1f}% "
          f"(범위 {mdds.max()*100:+.1f} ~ {mdds.min()*100:+.1f}%)")
    print("\n■ 트랜치 분할(자금을 나눠 여러 요일에)")
    res = {}
    for name, wds in (("P2 월+목", (0, 3)), ("P3 월+수+금", (0, 2, 4)), ("P7 매일 1/7", tuple(range(7)))):
        r = np.mean([T[w][common] for w in wds], axis=0)
        s, txt = line(name, d, r)
        res[name] = s
        print(txt)
    print("\n■ 판단(사전 기준: 낙폭 < 단일 요일 중앙 · 샤프 ≥ 단일 요일 중앙)")
    for name, s in res.items():
        a, b = s["mdd"] > np.median(mdds), s["sharpe"] >= np.median(shs)
        print(f"  {name:12} 낙폭 {s['mdd']*100:+.1f}% vs 중앙 {np.median(mdds)*100:+.1f}% {'○' if a else '×'} · "
              f"샤프 {s['sharpe']:+.2f} vs 중앙 {np.median(shs):+.2f} {'○' if b else '×'} → {'분할 근거 ✅' if a and b else '근거 부족'}")
    mon = single[0]
    print(f"\n  참고: 월요일만(지금 실매매) 연 {mon['ann']*100:+.1f}% · 낙폭 {mon['mdd']*100:+.1f}% — 7개 중 순위 "
          f"수익 {int((anns > mon['ann']).sum()) + 1}위 · 낙폭 {int((mdds > mon['mdd']).sum()) + 1}위(작을수록 좋음)")
    print("  참고: 회전 비용은 트랜치 수와 무관(각 몫이 자기 회전만) — 다만 주문 건수는 K 배, 몫당 금액은 1/K.")


if __name__ == "__main__":
    main()
