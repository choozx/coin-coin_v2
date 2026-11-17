"""XV — 같은 코인, 다른 계약/거래소 사이 펀딩 차익.

가설. M 이 무너진 건 펀딩이 아니라 **가격 다리**(다른 코인끼리라 헤지 불완전)였다. 같은 코인을
펀딩 높은 곳에서 숏 · 낮은 곳에서 롱 하면 가격 손익은 거의 상쇄되고 **펀딩 차이 − 비용**만 남는다.
변동성이 작아 짧은 기간으로도 판정이 된다.

짝:
  바이낸스 내부 — USDT 마진 · USDC 마진(2024-01~) · 코인 마진(인버스)
  거래소 간     — 바이낸스 USDT · 바이빗 USDT (2021~) · OKX(최근 ~3개월만)

규칙(룩어헤드 없음, 사전에 고정):
  하루 단위. 펀딩을 정산 시각 −1분 기준 UTC 일자로 묶는다 → d 일 00:00 정산은 d−1 일 칸.
  d 일 00:00 정산 직후 포지션을 정하고 d 일 08·16시, d+1 일 00시 정산을 받는다.
  신호 m = 직전 L 일 평균 스프레드(A − B). |m| × 365 > 문턱이면 sign(m) 방향(+ = A 숏·B 롱), 아니면 쉰다.
  비용: 다리 하나 편도 c. 진입·청산 2c, 방향 전환 4c.
  L ∈ {7, 30, 90}, 문턱 ∈ {0, 5%, 10%}/년 · c ∈ {2bp(메이커), 5bp(테이커)}.

판정(사전에 정함):
  ✅ 기본 설정(L=30·문턱 5%·c=5bp)에서 코인 평균 순수익이 무위험(연 ~4%)을 넘고, 연도별로 음수인 해가 없다.
  수익률은 **다리 하나의 명목 대비**. 자본 대비로는 × 레버리지 ÷ 2(두 다리 증거금).

한계(정직하게): 가격 다리의 베이시스(두 계약 가격 차) 변동은 안 넣었다 — 진입·청산 때 몇 bp 수준이고
같은 코인이라 평균 0 에 가깝지만 0 은 아니다. 코인 마진(인버스)은 손익이 코인이라 달러 헤지가 비선형이다.

    python3 -u -m research.exp_XV_venue_funding
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations

import numpy as np

from research.xv_data import DB

DAY = 86_400_000
PAIRS = (("bn_usdt", "bn_usdc"), ("bn_usdt", "bn_coin"), ("bn_usdc", "bn_coin"),
         ("bn_usdt", "bybit"), ("bn_usdt", "okx"), ("bybit", "okx"))
NAME = {"bn_usdt": "바낸 USDT", "bn_usdc": "바낸 USDC", "bn_coin": "바낸 코인M", "bybit": "바이빗", "okx": "OKX"}
LS, THRS, COSTS = (7, 30, 90), (0.0, 0.05, 0.10), (0.0002, 0.0005)
DEFAULT = (30, 0.05, 0.0005)
RF = 4.0


def load():
    """{(venue, coin): {day: 펀딩 합}} — 앞뒤 하루는 칸이 덜 찼을 수 있어 뺀다."""
    c = sqlite3.connect(DB)
    d = defaultdict(lambda: defaultdict(float))
    for v, coin, t, r in c.execute("SELECT venue, coin, t, rate FROM funding"):
        d[(v, coin)][(int(t) - 60_000) // DAY] += float(r)
    c.close()
    out = {}
    for k, m in d.items():
        days = sorted(m)
        out[k] = {x: m[x] for x in days[1:-1]}
    return out


def spread(F, a, b, coin):
    A, B = F.get((a, coin)), F.get((b, coin))
    if not A or not B:
        return None, None
    days = np.array(sorted(set(A) & set(B)), dtype=np.int64)
    if len(days) < 60:
        return None, None
    # 날짜가 연속인 구간만(빈 날이 있으면 '직전 L 일' 이 틀어진다) — 가장 긴 연속 구간
    br = np.where(np.diff(days) != 1)[0]
    segs = np.split(np.arange(len(days)), br + 1)
    seg = max(segs, key=len)
    days = days[seg]
    return days, np.array([A[x] - B[x] for x in days])


def run(s, L, thr, c):
    """하루 손익 배열(비용 포함)과 거래 횟수."""
    n = len(s)
    pnl = np.zeros(n)
    pos, trades = 0, 0
    cs = np.r_[0.0, np.cumsum(s)]
    for d in range(L, n):
        m = (cs[d] - cs[d - L]) / L
        want = int(np.sign(m)) if abs(m) * 365 > thr else 0
        if want != pos:
            legs = 2 * (abs(want - pos))                  # 0↔±1 = 2 다리, ±1↔∓1 = 4 다리
            pnl[d] -= legs * c
            trades += 1
            pos = want
        pnl[d] += pos * s[d]
    return pnl[L:], trades


def year_of(day):
    return datetime.fromtimestamp(int(day) * DAY / 1000, timezone.utc).year


def main():
    F = load()
    coins = sorted({k[1] for k in F})
    print(f"XV · 같은 코인 다른 계약/거래소 펀딩 차익 · 코인 {len(coins)}개 · 수익률 = 다리 하나 명목 대비 연%")
    L0, T0, C0 = DEFAULT
    for a, b in PAIRS:
        rows = []
        for coin in coins:
            days, s = spread(F, a, b, coin)
            if days is None:
                continue
            rows.append((coin, days, s))
        if not rows:
            continue
        d0 = min(r[1][0] for r in rows); d1 = max(r[1][-1] for r in rows)
        f = lambda x: datetime.fromtimestamp(int(x) * DAY / 1000, timezone.utc).strftime("%Y-%m-%d")
        print(f"\n■ {NAME[a]} − {NAME[b]}  ({f(d0)} ~ {f(d1)}, 코인 {len(rows)}개)")
        print(f"   {'코인':5} {'일수':>5} {'평균차(연)':>10} {'A>B 비율':>8} {'상한(연)':>8} | "
              f"기본 L={L0}·문턱{T0:.0%}·{C0*1e4:.0f}bp → 순수익(연) · 거래/년")
        per = []
        for coin, days, s in rows:
            pnl, tr = run(s, L0, T0, C0)
            yrs = len(pnl) / 365
            per.append((coin, days[L0:], pnl))
            print(f"   {coin:5} {len(s):5d} {s.mean()*365*100:+9.2f}% {np.mean(s > 0):8.0%} "
                  f"{np.abs(s).mean()*365*100:7.2f}% | {pnl.mean()*365*100:+7.2f}% · {tr/yrs:5.1f}")
        # 코인 평균 — 그리드
        print("   코인 평균 순수익(연%) — 행 L, 열 문턱 · [2bp | 5bp]")
        for L in LS:
            cells = []
            for thr in THRS:
                v = []
                for c in COSTS:
                    v.append(np.mean([run(s, L, thr, c)[0].mean() * 365 * 100 for _, _, s in rows if len(s) > L + 30]))
                cells.append(f"{v[0]:+6.2f} | {v[1]:+6.2f}")
            print(f"     L={L:2d}: " + "   ".join(f"[{t:.0%}] {x}" for t, x in zip(THRS, cells)))
        # 기본 설정 연도별(코인 평균)
        by = defaultdict(list)
        for coin, days, pnl in per:
            yy = np.array([year_of(x) for x in days])
            for y in np.unique(yy):
                by[y].append(pnl[yy == y].mean() * 365 * 100)
        print("   기본 설정 연도별(코인 평균, 연%): " + " · ".join(f"{y} {np.mean(v):+.2f}" for y, v in sorted(by.items())))
        avg = np.mean([p.mean() * 365 * 100 for _, _, p in per])
        worst = min(np.mean(v) for v in by.values())
        ok = avg > RF and worst > 0
        print(f"   판정: 코인 평균 {avg:+.2f}%/년 (무위험 {RF:.0f}%) · 최악 연도 {worst:+.2f}% → {'✅' if ok else '❌'}")


if __name__ == "__main__":
    main()
