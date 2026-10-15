"""HR — 하이리스크 하이리턴: 판정을 통과한 것(B3·M)에 레버리지를 얹는다.

요청(2026-10-14): "새로운 전략이 필요해… 하이리스크 하이리턴의 전략을 찾아봐".
왜 새 신호가 아니라 레버리지인가: 가설 약 32개 중 신호로 살아남은 건 B3(HB, 낙폭 축소)와 M(펀딩 횡단면)뿐이다.
레버리지는 엣지를 만들지 않고 키운다 — 엣지가 0 인 데 얹으면 수수료·펀딩·청산만 커진다(SC: −100%).
그러니 '하이리턴'의 근거가 있는 유일한 길은 **이미 랜덤을 넘은 것을 키우는 것**이고, 질문은 '몇 배까지 키우면
복리가 오히려 줄어드는가(변동성 손실·청산)'다. 이건 신호 탐색이 아니라 크기 결정이라 다중검정이 늘지 않는다.

A. B3(일봉 SMA200 위 AND 4h ST(14,2.5) 상승일 때만 롱) × 노출 E ∈ {1,1.5,2,3,4,5}
   - 진입 때 명목 = E × 자산, 거래 중엔 명목 고정(리밸런싱 없음 — 봇이 그렇다). 교차 증거금(자산 전체가 담보).
   - 청산: 4h 봉 **저가**로 본 자산이 유지증거금(명목×MMR 0.5%) 아래면 강제청산 → 그 거래 손실 = 담보 전부 −
     잔여 0 (자산 ×0.0, 파산). 저가는 1분봉에서 리샘플한 진짜 봉 안 극값.
   - 비용: 진입·청산 명목 × taker 5bp, 펀딩 = 현재 명목 × 실요율(롱이 양 요율 지불).
B. M(유동성 상위 30·k=7·lb=21·h=3, 편도 15bp) × E ∈ {1,1.5,2,3,4,5}
   - E=1 이 이미 롱 1 + 숏 1(총 2배)이다. 보유 주기 수익 r 에 E 를 곱해 복리(비용·펀딩도 E 배).
   - ⚠ 주기 안 경로를 안 본다(8h 격자 종가) — 한 다리 급등 청산은 과소평가된다. 최악 주기만 따로 찍는다.

선택 규칙(결과 전 고정): 구간 1·2(B3: BTC 기간 3등분 앞 둘 · M: 2021-22, 2023-24)를 이은 기간에서 **연복리가 최대인 E**를
고르고 그 **절반**(하프 켈리 관례 — 추정오차·꼬리 대비)을 채택. 격자에 없으면 아래로 내림.
통과(구간 3 = 홀드아웃, 한 번만 본다): ① 채택 E 의 구간 3 연복리 > 1배의 연복리 ② 구간 3 강제청산 0
③ (B3만) 알트 55개에서 채택 E 로 파산(청산) 비율 ≤ 1배의 파산 비율 + 10%p — 'BTC 에서만 버티는 레버리지' 차단.

    python3 -u -m research.exp_HR_leverage
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                        # noqa: E402
from engine.candles import resample                          # noqa: E402
from research import lib                                     # noqa: E402
from research.exp_HB_trend_hold import fund_per_bar, regime  # noqa: E402
from research.exp_T4_params import alt_bases                 # noqa: E402
from research.exp_T_supertrend_search import _ym, segments   # noqa: E402
from research import exp_M_funding_xs as M                   # noqa: E402

FEE = 0.0005
MMR = 0.005
LEVS = (1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
BARS_YEAR = 6 * 365


def b3_run(b4, pos, fund, E):
    """B3 를 노출 E 로. 반환 (봉별 자산(시가평가), 강제청산 횟수, 거래 수)."""
    n = len(b4.close)
    eq = np.empty(n)
    cash, liq, trades = 1.0, 0, 0
    in_pos, p0, notional, coll = False, 0.0, 0.0, 0.0
    for t in range(n):
        c = b4.close[t]
        if in_pos:
            # 봉 t 동안 보유(진입은 t-1 종가). 저가로 청산 검사.
            low_eq = coll + notional * (b4.low[t] / p0 - 1.0)
            if low_eq <= notional * (b4.low[t] / p0) * MMR:
                cash, in_pos, liq = 0.0, False, liq + 1
                eq[t:] = 0.0
                return eq, liq, trades
            coll -= notional * (c / p0) * fund[t - 1]          # 봉 t-1 종가→t 종가 사이 정산분
            cur = coll + notional * (c / p0 - 1.0)
            if not pos[t]:                                    # 종가에 청산
                cash = cur - notional * (c / p0) * FEE
                in_pos = False
                eq[t] = cash
            else:
                eq[t] = cur
        else:
            eq[t] = cash
            if pos[t] and cash > 0:
                notional, p0 = E * cash, c
                coll = cash - notional * FEE
                in_pos, trades = True, trades + 1
                eq[t] = coll
    return eq, liq, trades


def cagr_mdd(eq, bars_year=BARS_YEAR):
    eq = np.asarray(eq, float)
    yrs = (len(eq) - 1) / bars_year
    end = eq[-1] / eq[0]
    ann = (end ** (1 / yrs) - 1) * 100 if end > 0 else -100.0
    peak = np.maximum.accumulate(eq)
    mdd = float(np.max(1 - eq / np.where(peak > 0, peak, 1))) * 100
    return (end - 1) * 100, ann, mdd


def pick(rows):
    """구간1·2 연복리 최대 E 의 절반(격자에서 내림)."""
    best = max(rows, key=lambda r: r[1])[0]
    half = best / 2
    return best, max(e for e in LEVS if e <= half) if half >= LEVS[0] else LEVS[0]


def part_b3():
    segs = segments()
    st = cs.stats("BTCUSDT")
    base = lib.load("BTCUSDT", start_ms=st["min"], end_ms=st["max"], with_funding=False)[0]
    b4, b1d = resample(base, 240), resample(base, 1440)
    pos = regime(b4, b1d)["B3"]
    fund = fund_per_bar("BTCUSDT", b4)
    cuts = [int(np.searchsorted(b4.open_time, a)) for a, _ in segs] + [len(b4.close)]
    print(f"A. B3 × 레버리지 · BTCUSDT 4h · {_ym(int(b4.open_time[0]))}~{_ym(int(b4.open_time[-1]))} · "
          f"교차 증거금 · 청산은 봉 저가로")
    for i in range(3):
        print(f"   구간{i+1}: {_ym(int(b4.open_time[cuts[i]]))} ~ {_ym(int(b4.open_time[cuts[i+1]-1]))}"
              + ("  (홀드아웃)" if i == 2 else ""))
    print(f"   {'E':>4} | {'구간1·2 연':>9} {'MDD':>5} {'청산':>4} | {'구간3 연':>8} {'MDD':>5} {'청산':>4} | "
          f"{'전체 수익':>10} {'연':>7} {'MDD':>5} {'청산':>4}")

    def seg_run(lo, hi, E):
        sl = slice(lo, hi)
        sub = b4.__class__(b4.open_time[sl], b4.open[sl], b4.high[sl], b4.low[sl], b4.close[sl], b4.volume[sl], 240)
        return b3_run(sub, pos[sl], fund[sl], E)

    sel, hold, full = [], {}, {}
    for E in LEVS:
        e12, l12, _ = seg_run(cuts[0], cuts[2], E)
        e3, l3, _ = seg_run(cuts[2], cuts[3], E)
        ef, lf, tr = seg_run(0, len(b4.close), E)
        a12, a3, af = cagr_mdd(e12), cagr_mdd(e3), cagr_mdd(ef)
        sel.append((E, a12[1]))
        hold[E] = (a3[1], l3)
        full[E] = (af, lf)
        print(f"   {E:4.1f} | {a12[1]:+8.1f}% {a12[2]:4.0f}% {l12:4d} | {a3[1]:+7.1f}% {a3[2]:4.0f}% {l3:4d} | "
              f"{af[0]:+9.0f}% {af[1]:+6.1f}% {af[2]:4.0f}% {lf:4d}", flush=True)
    best, E_ = pick(sel)
    print(f"   선택: 구간1·2 연복리 최대 E={best:.1f} → 절반 채택 E={E_:.1f}")

    print(f"\n   알트 55개(2021-01~, 1h→4h, 펀딩 실데이터) — 파산(강제청산) 비율과 수익 중앙")
    alts = []
    for s, b, _ in alt_bases():
        b1h = b.__class__(b.open_time - 59 * 60_000, b.open, b.high, b.low, b.close, b.volume, 60,
                          taker_buy=b.taker_buy)
        a4, a1d = resample(b1h, 240), resample(b1h, 1440)
        alts.append((a4, regime(a4, a1d)["B3"], fund_per_bar(s, a4)))
    ruin = {}
    for E in LEVS:
        res = [b3_run(a4, p, f, E) for a4, p, f in alts]
        ruin[E] = np.mean([r[1] > 0 for r in res]) * 100
        tot = [cagr_mdd(r[0])[0] for r in res]
        print(f"   E={E:3.1f}  파산 {ruin[E]:3.0f}%  수익 중앙 {np.median(tot):+7.0f}%  "
              f"상위 25% {np.percentile(tot, 75):+7.0f}%  수익 양수 {np.mean(np.array(tot) > 0) * 100:3.0f}%", flush=True)
    ok1 = hold[E_][0] > hold[1.0][0]
    ok2 = hold[E_][1] == 0
    ok3 = ruin[E_] <= ruin[1.0] + 10
    print(f"   판정(E={E_:.1f}) ① 홀드아웃 연복리 {hold[E_][0]:+.1f}% vs 1배 {hold[1.0][0]:+.1f}% {'✅' if ok1 else '❌'} · "
          f"② 홀드아웃 청산 {hold[E_][1]} {'✅' if ok2 else '❌'} · ③ 알트 파산 {ruin[E_]:.0f}% vs 1배 {ruin[1.0]:.0f}% "
          f"{'✅' if ok3 else '❌'} → {'✅ 통과' if ok1 and ok2 and ok3 else '❌'}")


def m_returns(P, F, U, lb=21, hold=3, k=7, cost=0.0015):
    """M 의 보유 주기별 순수익(E=1)."""
    out = []
    prevL = prevS = None
    S, N = P.shape
    for t in range(lb, N - hold - 1, hold):
        sig = np.where(U[:, t], F[:, t - lb:t].mean(axis=1), np.nan)
        order = np.argsort(sig)[:int(U[:, t].sum())]
        li, si = order[:k], order[-k:]
        price = (P[li, t + hold] / P[li, t] - 1).mean() - (P[si, t + hold] / P[si, t] - 1).mean()
        fund = F[si, t + 1:t + hold + 1].sum(1).mean() - F[li, t + 1:t + hold + 1].sum(1).mean()
        turn = 1.0 if prevL is None else 1.0 - (
            len(np.intersect1d(li, prevL)) + len(np.intersect1d(si, prevS))) / (2 * k)
        out.append(price + fund - 4.0 * turn * cost)
        prevL, prevS = li, si
    return np.array(out)


def part_m():
    syms, times, P, F, V = M.load(with_volume=True)
    U = M.universe_mask(V, 30)
    per_year = 365 / 1.0                                       # h=3 × 8h = 1일
    print(f"\nB. M × 레버리지 · {len(syms)}심볼 · 상위 30·k=7·lb=21·h=3 · 편도 15bp · E=1 이 롱1+숏1")
    rs = {}
    for name, a, b in M.SEGMENTS:
        m = M._seg_mask(times, a, b)
        rs[name] = m_returns(P[:, m], F[:, m], U[:, m])
    r12 = np.concatenate([rs["2021-22"], rs["2023-24"]])
    r3 = rs["2025-26"]
    allr = np.concatenate([r12, r3])
    print(f"   주기(1일) 수익: 평균 {allr.mean()*1e4:+.1f}bp · 표준편차 {allr.std()*1e4:.0f}bp · "
          f"최악 {allr.min()*100:+.1f}% · 최악 5% 평균 {np.sort(allr)[:max(1, len(allr)//20)].mean()*100:+.1f}%")
    print(f"   {'E':>4} | {'구간1·2 연':>9} {'MDD':>5} | {'구간3 연':>8} {'MDD':>5} | {'전체 연':>7} {'MDD':>5} {'파산':>4}")

    def run(r, E):
        g = 1 + E * r
        broke = bool((g <= 0).any())
        return np.concatenate([[1.0], np.cumprod(np.maximum(g, 0))]), broke

    sel, hold = [], {}
    for E in LEVS:
        e12, _ = run(r12, E)
        e3, b3 = run(r3, E)
        ef, bf = run(allr, E)
        a12, a3, af = cagr_mdd(e12, per_year), cagr_mdd(e3, per_year), cagr_mdd(ef, per_year)
        sel.append((E, a12[1]))
        hold[E] = (a3[1], b3)
        print(f"   {E:4.1f} | {a12[1]:+8.1f}% {a12[2]:4.0f}% | {a3[1]:+7.1f}% {a3[2]:4.0f}% | "
              f"{af[1]:+6.1f}% {af[2]:4.0f}% {'예' if bf else '-':>4}")
    # 연속 E 로 성장 최적(켈리) 참고
    grid = np.arange(0.5, 20.01, 0.5)
    g12 = [np.log(np.maximum(1 + E * r12, 1e-12)).mean() for E in grid]
    print(f"   참고: 구간1·2 로그성장 최대 E ≈ {grid[int(np.argmax(g12))]:.1f} (격자 0.5~20)")
    best, E_ = pick(sel)
    ok1 = hold[E_][0] > hold[1.0][0]
    ok2 = not hold[E_][1]
    print(f"   선택: 구간1·2 연복리 최대 E={best:.1f} → 절반 채택 E={E_:.1f}")
    print(f"   판정(E={E_:.1f}) ① 홀드아웃 연복리 {hold[E_][0]:+.1f}% vs 1배 {hold[1.0][0]:+.1f}% {'✅' if ok1 else '❌'} · "
          f"② 파산 없음 {'✅' if ok2 else '❌'} → {'✅ 통과' if ok1 and ok2 else '❌'}")


if __name__ == "__main__" and "--combo" not in sys.argv:
    part_b3()
    part_m()


# ── C. 연 100% 를 넘을 수 있나 — 연도별·롤링 12개월, 그리고 B3+M 묶음 (2026-10-14 추가) ──
# 요청: "연 수익 100% 넘는 전략 찾아. 레버리지 적용이어도 괜찮아".
# 레버리지로 올릴 수 있는 연복리의 상한은 성장 최적 배수에서의 값이고 그건 샤프에 달렸다(대략 e^(SR²/2)−1).
# 연 100% = 로그성장 0.69 → 풀 켈리면 SR≈1.18, 하프 켈리면 SR≈1.36 이 필요하다. 그러니 볼 것은 샤프다.
# 묶음 규칙(결과 전 고정): 일별 수익, 비중 = 구간1·2(2021-01~2024-12) 변동성 역수, 묶음 배수 = 구간1·2 연복리 최대의 절반,
# 매일 리밸런싱(근사). 홀드아웃 2025-01~ 한 번. 통과 = 홀드아웃 연복리 ≥ 100%.

def daily_b3():
    """B3 1배 일별 수익(UTC 날짜 → 수익). HB 의 curve 와 같은 회계."""
    from research.exp_HB_trend_hold import curve
    st = cs.stats("BTCUSDT")
    base = lib.load("BTCUSDT", start_ms=st["min"], end_ms=st["max"], with_funding=False)[0]
    b4, b1d = resample(base, 240), resample(base, 1440)
    pos = regime(b4, b1d)["B3"]
    r = b4.close[1:] / b4.close[:-1] - 1.0
    pr = curve(pos, r, fund_per_bar("BTCUSDT", b4))
    day = (b4.open_time[1:] + 240 * 60_000 - 1) // 86_400_000       # 봉 t+1 이 끝나는 날
    out = {}
    for d, x in zip(day, pr):
        out[int(d)] = out.get(int(d), 1.0) * (1 + x)
    return {d: v - 1 for d, v in out.items()}


def daily_m():
    syms, times, P, F, V = M.load(with_volume=True)
    U = M.universe_mask(V, 30)
    lb, hold = 21, 3
    r = m_returns(P, F, U)
    ts = times[np.arange(lb, P.shape[1] - hold - 1, hold)][:len(r)] + hold * M.FUND_MS   # 주기 끝 시각
    out = {}
    for t, x in zip(ts, r):
        d = int((t - 1) // 86_400_000)
        out[d] = out.get(d, 1.0) * (1 + x)
    return {d: v - 1 for d, v in out.items()}


def part_c():
    import datetime as dt
    b, m = daily_b3(), daily_m()
    days = sorted(set(b) & set(m))
    days = list(range(days[0], days[-1] + 1))
    R = np.array([[b.get(d, 0.0), m.get(d, 0.0)] for d in days])
    year = np.array([dt.datetime.fromtimestamp(d * 86400, dt.timezone.utc).year for d in days])
    sel = year <= 2024
    sr = lambda x: x.mean() / x.std() * np.sqrt(365)
    print(f"\nC. 연 100% 가능성 · 일별 · {dt.date.fromtimestamp(days[0]*86400)}~{dt.date.fromtimestamp(days[-1]*86400)}")
    print(f"   연 샤프(1배)  B3 {sr(R[:,0]):.2f} · M {sr(R[:,1]):.2f} · 상관 {np.corrcoef(R[:,0], R[:,1])[0,1]:+.2f}"
          f"   (연 100% 에 필요: 풀 켈리 1.18 · 하프 켈리 1.36)")
    print(f"   구간1·2 샤프 B3 {sr(R[sel,0]):.2f} · M {sr(R[sel,1]):.2f}   홀드아웃(2025~) B3 {sr(R[~sel,0]):.2f} · M {sr(R[~sel,1]):.2f}")
    w = 1 / R[sel].std(0); w /= w.sum()
    combo = R @ w
    print(f"   묶음 비중(변동성 역수, 구간1·2): B3 {w[0]:.0%} · M {w[1]:.0%} · 묶음 샤프 전체 {sr(combo):.2f}"
          f" · 구간1·2 {sr(combo[sel]):.2f} · 홀드아웃 {sr(combo[~sel]):.2f}")
    grid = np.arange(0.5, 15.01, 0.5)
    ann = lambda x: (np.prod(np.maximum(1 + x, 0)) ** (365 / len(x)) - 1) * 100
    best = grid[int(np.argmax([ann(E * combo[sel]) for E in grid]))]
    E_c = max(g for g in grid if g <= best / 2)
    print(f"   묶음 배수: 구간1·2 최대 {best:.1f} → 절반 {E_c:.1f}")

    series = {"B3 1배": R[:, 0], "B3 2배": 2 * R[:, 0], "M 1배": R[:, 1], "M 2배": 2 * R[:, 1],
              f"묶음 {E_c:.1f}배": E_c * combo}
    yrs = sorted(set(year))
    print(f"\n   {'':10} " + " ".join(f"{y:>8}" for y in yrs) + f" {'홀드아웃':>8} {'MDD':>5} {'12개월>100%':>11}")
    for name, x in series.items():
        eq = np.cumprod(np.maximum(1 + x, 0))
        mdd = np.max(1 - eq / np.maximum.accumulate(eq)) * 100
        roll = eq[365:] / eq[:-365] - 1
        cells = [f"{(np.prod(1 + x[year == y]) - 1) * 100:+7.0f}%" for y in yrs]
        print(f"   {name:10} " + " ".join(cells) + f" {ann(x[~sel]):+7.0f}% {mdd:4.0f}% {np.mean(roll > 1) * 100:10.0f}%")
    ok = ann(E_c * combo[~sel]) >= 100
    print(f"   판정: 묶음 {E_c:.1f}배 홀드아웃 연복리 {ann(E_c * combo[~sel]):+.0f}% → {'✅ 연 100% 이상' if ok else '❌ 연 100% 미달'}")


if __name__ == "__main__" and "--combo" in sys.argv:
    part_c()
