"""M + 극단 제외 필터 — 연구기간에서 고르고, 이미 본 77일은 참고만.

M 의 OOS 손실은 펌프 뒤 펀딩이 극단으로 간 알트(IOST −6.93%/8h, RLC)에서 났다. 그런 종목을 빼면?
⚠ 그 77일을 이미 봤으니 거기서 필터를 고르면 커브핏이다. **값은 연구기간(2021-01 ~ 2026-09-01) 세 구간으로만**
고르고, 77일 숫자는 '참고(오염)'로만 찍는다. 진짜 검증은 다음에 새로 쌓일 데이터다.

필터(형태·후보값은 결과 보기 전에 고정, 모두 그 시각 이전 정보만):
  F 극단 펀딩 — 직전 7일 평균 펀딩 |f| > 문턱 인 종목 제외 (0.05 · 0.10 · 0.20 %/8h)
  P 급등락   — 직전 7일 가격 변동 |r| > 문턱 인 종목 제외 (30 · 50 %)
나머지는 M 고정 설정 그대로(상위 30 · k=7 · lb=21 · h=3 · 편도 15bp). 귀무도 같은 유니버스에서 뽑는다.

    python3 -u -m research.exp_M_filter
"""
from __future__ import annotations

import numpy as np

from research.exp_M_funding_xs import SEGMENTS, _seg_mask, load, null_dist, simulate, universe_mask
from research.exp_M_oos import H, K, LB, OOS_START, TOP, _ms

COST = 0.0015
FILTERS = [("없음", None, None)] + [(f"F |펀딩|>{x*100:.2f}%", "F", x) for x in (0.0005, 0.0010, 0.0020)] \
          + [(f"P |7일 가격|>{x:.0%}", "P", x) for x in (0.30, 0.50)]


def allowed(P, F, kind, x):
    S, N = P.shape
    ok = np.ones((S, N), bool)
    if kind == "F":
        cf = np.concatenate([np.zeros((S, 1)), np.cumsum(F, axis=1)], axis=1)
        avg = np.full((S, N), np.nan)
        avg[:, LB:] = (cf[:, LB:N] - cf[:, :N - LB]) / LB          # t 직전 LB 정산 평균 = 신호와 같은 정보
        ok = ~(np.abs(avg) > x)
    elif kind == "P":
        r = np.full((S, N), np.nan)
        r[:, LB:] = P[:, LB:] / P[:, :-LB] - 1                     # t 시각 가격까지(그 시각에 앎)
        ok = ~(np.abs(r) > x)
    return ok


def main():
    syms, times, P, F, V = load(with_volume=True)
    U0 = universe_mask(V, TOP)
    i0 = int(np.searchsorted(times, _ms(OOS_START)))
    print(f"M + 극단 제외 필터 · 상위 {TOP} · k={K} · 편도 {COST*1e4:.0f}bp")
    print(f"   {'필터':20} | {'연구 2021-22':>22} | {'연구 2023-24':>22} | {'연구 2025-26.08':>22} || 77일 참고(오염) 위상 0·1·2")
    for name, kind, x in FILTERS:
        U = U0 & allowed(P, F, kind, x) if kind else U0
        cells = []
        for seg, a, b in SEGMENTS:
            m = _seg_mask(times, a, b) & (times < times[i0])
            r, turn, fs, ps = simulate(P[:, m], F[:, m], LB, H, K, COST, U[:, m])
            p95 = float(np.percentile(null_dist(P[:, m], F[:, m], LB, H, K, COST, U=U[:, m], samples=500), 95))
            cells.append(f"{r:+8.1f}% (펀딩{fs:+4.0f}) {'✅' if r > p95 and r > 0 else '❌'}")
        oos = []
        for off in range(H):
            lo = i0 - LB + off
            r, *_ = simulate(P[:, lo:], F[:, lo:], LB, H, K, COST, U[:, lo:])
            oos.append(f"{r:+6.1f}%")
        excl = 1 - (U[:, :i0].sum() / max(U0[:, :i0].sum(), 1))
        print(f"   {name:20} | " + " | ".join(cells) + " || " + " ".join(oos) + f"  (제외 {excl:.1%})", flush=True)


if __name__ == "__main__":
    main()
