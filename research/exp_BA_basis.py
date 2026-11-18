"""BA — 베이시스 급등 되돌림: 선물이 현물보다 크게 비쌀 때 현물 롱 + 선물 숏, 프리미엄이 꺼지면 청산. 차입 불필요.

basis b = 선물 종가 / 현물 종가 − 1 (같은 1시간봉). 진입 b > X → 청산 b < Y 또는 최대 보유 Hmax 시간.
손익 = (현물 수익률 − 선물 수익률) + 보유 중 펀딩(양수면 숏이 받음) − 비용 0.30%(한쪽 현물 0.10% + 선물 0.05%, 왕복).

함정: 1시간 종가는 두 시장의 '마지막 체결'이라 얇은 현물은 몇십 초 늦은 값일 수 있다 → 빠르게 움직이는 봉에서
가짜 프리미엄이 생기고 다음 봉에 저절로 '되돌아간다'. 그래서 두 가지로 잰다.
  즉시(낙관)  신호 봉 종가에 진입
  지연(보수)  신호 다음 봉 종가에 진입(1시간 지나도 프리미엄이 남아 있을 때만 = 그 시점 b > X 재확인)
판정은 **지연** 기준 · 전반(2021-01~2023-12) / 후반(2024-01~) 둘 다 양수여야. 연율은 슬롯 K 개 포트폴리오(자본 = 명목×1.33).

데이터: exp_CC_carry 의 load() (M 유니버스 56심볼 선물·현물 1h · funding).

    python3 -u -m research.exp_BA_basis
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from research.exp_CC_carry import CAP, load

COST = 0.0030
SPLIT = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def trades(grid, P, S, F, X, Y, Hmax, delay):
    B = P / S - 1
    n, m = P.shape
    out = []
    for j in range(m):
        b, p, s, f = B[:, j], P[:, j], S[:, j], F[:, j]
        i = 1
        while i < n - 1:
            if not (b[i] > X):
                i += 1
                continue
            e = i + delay
            if e >= n - 1 or not (b[e] > X) or np.isnan(p[e]) or np.isnan(s[e]):
                i += 1
                continue
            k = e + 1
            fund = 0.0
            while k < n:
                if np.isnan(p[k]) or np.isnan(s[k]):
                    k -= 1
                    break
                fund += f[k]
                if b[k] < Y or k - e >= Hmax:
                    break
                k += 1
            k = min(k, n - 1)
            if k <= e:
                i = e + 1
                continue
            g = (s[k] / s[e] - 1) - (p[k] / p[e] - 1)
            out.append((grid[e], j, b[e], b[k], g, fund, g + fund - COST, k - e))
            i = k + 1
    return out


def portfolio(tr, n_hours, K=5):
    """동시 최대 K 개, 각 명목 1/K. 들어온 순서대로 자리가 있으면 잡는다. → 자본 기준 연율(전체·후반)."""
    tr = sorted(tr, key=lambda x: x[0])
    busy = []
    got = []
    for t, j, b0, b1, g, fund, net, h in tr:
        busy = [u for u in busy if u > t]
        if len(busy) < K:
            busy.append(t + h * 3_600_000)
            got.append((t, net / K))
    yrs = n_hours / (365 * 24)
    tot = sum(v for _, v in got) / yrs / CAP
    return tot, len(got)


def main():
    grid, syms, P, S, F = load()
    B = P / S - 1
    ok = ~np.isnan(B)
    q = np.nanpercentile(B[ok], [1, 5, 50, 95, 99, 99.9])
    print(f"BA 베이시스 되돌림 · {len(syms)}심볼 · 1h · basis(선물/현물−1) 분포 % : 1% {q[0]*100:+.3f} · 5% {q[1]*100:+.3f} · "
          f"중앙 {q[2]*100:+.3f} · 95% {q[3]*100:+.3f} · 99% {q[4]*100:+.3f} · 99.9% {q[5]*100:+.3f}")
    n_h = len(grid)
    h1 = int(((grid >= SPLIT)).sum())
    print("   건당 % (비용 0.30% 후) · [전반 2021~2023 | 후반 2024~] · 연율 = 슬롯 5개 포트폴리오 자본 기준")
    print(f"   {'진입':>6} {'청산':>6} {'최대':>5} {'방식':>4} | {'건수':>5} {'베이시스':>8} {'펀딩':>6} {'순':>7} {'±2se':>6} "
          f"{'이익':>4} {'보유h':>5} | {'전반 순':>8} {'후반 순':>8} | {'연율':>6}")
    for X in (0.003, 0.005, 0.01, 0.02):
        for Y in (0.001, 0.0):
            for Hmax in (24, 168):
                for delay, nm in ((0, "즉시"), (1, "지연")):
                    tr = trades(grid, P, S, F, X, Y, Hmax, delay)
                    if len(tr) < 10:
                        continue
                    a = np.array([(x[4], x[5], x[6], x[7], x[0]) for x in tr])
                    net = a[:, 2] * 100
                    se = 2 * net.std() / np.sqrt(len(net))
                    n0, n1 = net[a[:, 4] < SPLIT], net[a[:, 4] >= SPLIT]
                    ann, _ = portfolio(tr, n_h)
                    print(f"   {X*100:5.1f}% {Y*100:5.1f}% {Hmax:4d}h {nm:>4} | {len(net):5d} {a[:,0].mean()*100:+8.3f} "
                          f"{a[:,1].mean()*100:+6.3f} {net.mean():+7.3f} {se:6.3f} {np.mean(net > 0):4.0%} {a[:,3].mean():5.1f} | "
                          f"{(n0.mean() if len(n0) else np.nan):+8.3f} {(n1.mean() if len(n1) else np.nan):+8.3f} | {ann*100:+5.1f}%")
    _ = h1


if __name__ == "__main__":
    main()
