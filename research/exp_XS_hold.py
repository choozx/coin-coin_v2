"""XS MOM 보유 기간 1~30일 비교 — 요일 운을 뺀 겹침(트랜치) 방식 (사용자 요청, 2026-11-19).

현행: 월요일 종가 신호 → 7일 보유 → 다음 월요일 재편(연 +63.1%, 단 월요일이 7개 요일 중 최고).
H일 보유를 공정하게 비교하려면 '어느 날 시작했나'의 운을 빼야 한다 → 매일 종가에 새 묶음(트랜치)을 1/H 자본으로 만들고 H일 들고 판다.
그러면 늘 H개 묶음이 겹쳐 있고 시작 요일 운이 평균된다(Jegadeesh-Titman 방식).

━━ 사전 고정 (2026-11-19 작성 · 결과 보기 전) ━━
신호·대상·개수·비용   현행과 같음(28일 수익률 상·하위 10%, 동일 가중, 총 노출 1, 회전당 0.10%, 펀딩 실제).
트랜치               i일 종가에 만든 묶음은 i+1 ~ i+H 일 손익(가격 + 펀딩), 중간 재조정 없음(현행과 같은 매수 후 보유).
                     비용 = 그날 새로 만드는 묶음과 같은 날 끝나는 묶음의 가중치 차이 × 0.10% ÷ H (상쇄 안 함 → 보수적).
비교 구간            모든 H 가 같은 날짜(2024-01-31 ~ 2026-11-16 일간 손익).
선택 규칙            H 별 연 수익을 이웃 ±3일로 평활한 값이 가장 큰 H. 단 반기 25H2·26H1·26H2 가 모두 > 0 이어야 후보.
                     (30개 중 최댓값 고르기라 다중 비교 — 선택된 H 도 '현행보다 확실히 낫다'는 뜻이 아님. 곡선 모양을 본다.)
참고                 t 는 7일 묶음(겹치지 않는 주) 합으로 계산. 현행(월요일 고정 7일)도 같이 표시.

    python3 -u -m research.exp_XS_hold
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from engine.xs_mom import AGE, COST, MINQ
from research.exp_XS_cross_section import HALVES, START, _ms, load

D = 86_400_000
LOOK, HMAX = 28, 30


def selections(days, C, Q, age, i0):
    """각 날 i 의 (롱 인덱스, 숏 인덱스, k)."""
    sel = {}
    for i in range(i0, len(days) - 1):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - LOOK]) & (age[i] >= AGE)
        live &= np.nan_to_num(np.nanmedian(Q[i - 29:i + 1], axis=0)) >= MINQ
        js = np.where(live)[0]
        if len(js) < 20:
            continue
        sig = C[i, js] / C[i - LOOK, js] - 1
        k = max(1, int(len(js) * 0.10))
        o = js[np.argsort(-sig)]
        sel[i] = (o[:k], o[-k:], k)
    return sel


def main():
    days, syms, C, Q, Fd, age = load()
    n, m = C.shape
    Cf = C.copy()                                                   # 폐지 뒤엔 마지막 종가로 고정
    for i in range(1, n):
        nan = np.isnan(Cf[i])
        Cf[i, nan] = Cf[i - 1, nan]
    dC = np.zeros_like(Cf)
    dC[1:] = np.nan_to_num(Cf[1:] - Cf[:-1])
    alive = ~np.isnan(C)
    Fa = np.where(alive, Fd, 0.0)
    i0 = int(np.searchsorted(days, START))
    sel = selections(days, C, Q, age, i0)
    print(f"심볼 {len(syms)}개 · 신호일 {len(sel)}개", flush=True)

    W = {}
    for i, (L, S, k) in sel.items():
        w = np.zeros(m)
        w[L], w[S] = 0.5 / k, -0.5 / k
        W[i] = w
    d0, d1 = i0 + HMAX, n - 1                                        # 모든 H 가 같은 날짜
    dd = days[d0:d1 + 1]
    out = {}
    for H in range(1, HMAX + 1):
        r = np.zeros(n)
        for i, w in W.items():
            J = np.nonzero(w)[0]
            e = min(i + H, n - 1)
            if e <= i:
                continue
            inc = dC[i + 1:e + 1][:, J] / C[i, J] - Fa[i + 1:e + 1][:, J]   # 롱 기준 일간 손익
            r[i + 1:e + 1] += inc @ w[J] / H
            prev = W.get(i - H)
            turn = np.abs(w - (prev if prev is not None else 0)).sum()
            r[i + 1] -= turn * COST / H
        rr = r[d0:d1 + 1]
        wk = rr[: len(rr) // 7 * 7].reshape(-1, 7).sum(1)
        t = wk.mean() / (wk.std(ddof=1) / np.sqrt(len(wk)))
        eq = np.cumprod(1 + rr)
        mdd = (eq / np.maximum.accumulate(eq) - 1).min()
        hv = []
        for j, (nm, y, mo) in enumerate(HALVES):
            a = _ms(y, mo)
            b = _ms(*HALVES[j + 1][1:]) if j + 1 < len(HALVES) else 1 << 62
            hv.append(rr[(dd >= a) & (dd < b)].sum())
        out[H] = dict(ann=rr.mean() * 365, t=t, mdd=mdd, halves=hv, turn_day=None)
    anns = np.array([out[H]["ann"] for H in range(1, HMAX + 1)])
    smooth = np.array([anns[max(0, h - 3):h + 4].mean() for h in range(HMAX)])
    print(f"\n일간 손익 {datetime.fromtimestamp(dd[0]/1000, timezone.utc):%Y-%m-%d} ~ "
          f"{datetime.fromtimestamp(dd[-1]/1000, timezone.utc):%Y-%m-%d} · 매일 트랜치(요일 운 평균)")
    print(" H  | 연 수익 | 평활(±3) |   t   | 최대낙폭 | 반기 24H1 · 24H2 · 25H1 · 25H2 · 26H1 · 26H2")
    best, bestv = None, -9
    for H in range(1, HMAX + 1):
        o = out[H]
        ok = all(x > 0 for x in o["halves"][3:])
        if ok and smooth[H - 1] > bestv:
            best, bestv = H, smooth[H - 1]
        print(f" {H:2d} | {o['ann']*100:+6.1f}% | {smooth[H-1]*100:+6.1f}% | {o['t']:+5.2f} | {o['mdd']*100:+6.1f}% | "
              + " · ".join(f"{x*100:+5.1f}" for x in o["halves"]) + ("" if ok else "  (최근 반기 음수)"))
    print(f"\n선택 규칙(평활 최대 · 최근 3반기 모두 +) → H = {best}일 · 평활 연 {bestv*100:+.1f}% "
          f"(H 그 자체 {out[best]['ann']*100:+.1f}%, t {out[best]['t']:+.2f})")
    print(f"참고: 현행 7일(매일 트랜치) {out[7]['ann']*100:+.1f}% · t {out[7]['t']:+.2f} — 월요일 고정 7일은 +63.1%")


if __name__ == "__main__":
    main()
