"""UM — XS MOM 을 업비트 원화 시장에: ① 롱만 · ② 롱 + 바이낸스 BTC 숏 (사용자, 2026-11-20).

XS MOM(바이낸스 알트 롱숏)이 업비트에서도 통하면 같은 효과를 다른 시장에서 얻는 분산이 된다. 업비트는 현물 → 숏 불가.

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
데이터  업비트 KRW 300개 1시간봉(exp_UP9_upbit_pump.collect, data/upbit9.db) 2024-07 ~ 2026-11-18 → 일봉(09:00 KST = 00:00 UTC 경계, 종가 = 08:59 KST).
        바이낸스 BTCUSDT 일봉·펀딩(xvenue.db xs_1d · fh5_funding, exp_XS2_factors 수집분) — ② 의 숏 다리.
대상    상장(첫 봉) ≥ 30일 · 30일 일 거래대금 중앙 ≥ 30억 원 · 스테이블(USDT·USDC·USDS·USD1·DAI) 제외.
신호    월요일 09:00 KST 종가 기준 28일 수익률. 상위 10% 동일 가중 롱(합 1). 7일 보유 · 매주 월요일 리밸런스.
비용    회전당 0.10%(업비트 수수료 0.05% + 호가 차 0.05%). 폐지·거래 중단이면 마지막 종가.
기준선  B = 같은 대상 전체 동일 가중 롱(같은 리밸런스·같은 비용) — '모멘텀으로 고른 것'의 값어치는 ① − B.
②       ① 롱 1 + 바이낸스 BTC 무기한 숏 1(같은 명목, 매주 재설정) · 숏 다리 = −BTCUSDT 수익 − 숏이 받는 펀딩(= +펀딩) · 비용 0.05%×회전.
        ⚠ 원/달러 환율·김프 변화는 무시(두 다리를 % 수익으로만 더함).
판정    ① (선택 효과) 주간 (① − B) t ≥ 2.2  ② 24H2 이후 반기 중 25H2·26H1·26H2 모두 (① − B) > 0  ③ 최고 주 빼도 누적 (① − B) > 0
        ④ 상위 20%(5분위)도 (− B) > 0.   ② (시장 중립 수익) 주간 수익 t ≥ 2.2 · 같은 반기 > 0 · 최고 주 빼도 > 0. (둘이라 t 문턱 2.2)
참고    BTC 보유 · B 자체 · 롱 상위 5% · 거래대금 문턱 10억/100억 · 바이낸스 XS MOM 과 주간 상관.

    .venv/bin/python -u research/exp_UM_upbit_mom.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import exp_UP9_upbit_pump as up  # noqa: E402
from research import exp_XS_cross_section as xs  # noqa: E402
from research import exp_XS2_factors as x2  # noqa: E402

D, H = 86_400_000, 3_600_000
START = int(datetime(2024, 9, 2, tzinfo=timezone.utc).timestamp() * 1000)    # 첫 봉(2024-07) + 60일 여유 뒤 첫 월요일
MINQ_KRW = 3e9
STABLE = {"USDT", "USDC", "USDS", "USD1", "DAI"}
COST, HEDGE_COST = 0.0010, 0.0005
T_MIN = 2.2
HALVES = [("2024-H2", datetime(2024, 7, 1)), ("2025-H1", datetime(2025, 1, 1)), ("2025-H2", datetime(2025, 7, 1)),
          ("2026-H1", datetime(2026, 1, 1)), ("2026-H2", datetime(2026, 7, 1))]


def load_upbit():
    c = up.conn()
    coins = [r[0] for r in c.execute("SELECT coin FROM up9_done WHERE n > 0") if r[0] not in STABLE]
    t_lo = int(datetime(2024, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
    t_hi = int(datetime(2026, 11, 18, tzinfo=timezone.utc).timestamp() * 1000)
    days = np.arange(t_lo, t_hi + D, D, dtype=np.int64)
    C = np.full((len(days), len(coins)), np.nan)
    Q = np.zeros((len(days), len(coins)))
    first = np.full(len(coins), np.inf)
    for j, cn in enumerate(coins):
        for t, cl, v in c.execute("SELECT t, c, v FROM up9_1h WHERE coin=?", (cn,)):
            t = int(t)
            first[j] = min(first[j], t)
            i = (t + H - t_lo) // D                          # 23:00 UTC 봉의 종가 = 다음 날 00:00 UTC(09:00 KST) 종가
            if 0 <= i < len(days):
                if (t + H) % D == 0:
                    C[i, j] = cl
                pass
            di = (t - t_lo) // D + 1                          # 그 시간이 끝나는 날(= days[di] 종가까지의 거래대금) — 종가 이후 거래를 섞지 않게
            if 0 <= di < len(days):
                Q[di, j] += v
    c.close()
    age = (days[:, None] - first[None, :]) / D
    return days, coins, C, Q, age


def btc_binance(days):
    c = sqlite3.connect(xs.DB)
    px = {int(t): cl for t, cl in c.execute("SELECT t, close FROM xs_1d WHERE symbol='BTCUSDT'")}
    fr = c.execute("SELECT t, rate FROM fh5_funding WHERE symbol='BTCUSDT'").fetchall()
    c.close()
    # xs_1d 의 t = 일봉 시작(00:00 UTC), close = 다음 날 00:00 UTC 직전 → 업비트 days[i](그날 00:00 UTC 종가)와 맞추려면 전날 봉
    P = np.array([px.get(int(d) - D, np.nan) for d in days])
    F = np.zeros(len(days))
    for t, r in fr:
        i = int(np.searchsorted(days, int(t), side="right")) - 1   # (days[i], days[i+1]] 구간 정산 → i+1 에 귀속
        if 0 <= i + 1 < len(days):
            F[i + 1] += float(r)
    return P, F


def run(days, C, Q, age, frac=0.10, minq=MINQ_KRW, mode="mom"):
    n, m = C.shape
    i0 = int(np.searchsorted(days, START))
    while datetime.fromtimestamp(days[i0] / 1000, timezone.utc).weekday() != 0:
        i0 += 1
    w = np.zeros(m)
    out = []
    for i in range(i0, n - 7, 7):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - 28]) & (age[i] >= 30)
        live &= np.median(Q[i - 29:i + 1], axis=0) >= minq
        js = np.where(live)[0]
        if len(js) < 20:
            continue
        if mode == "mom":
            sig = C[i, js] / C[i - 28, js] - 1
            k = max(1, int(len(js) * frac))
            L = js[np.argsort(-sig)[:k]]
        else:                                                # 기준선: 전체 동일 가중
            L, k = js, len(js)
        nw = np.zeros(m)
        nw[L] = 1.0 / k
        turn = np.abs(nw - w).sum()
        r = np.zeros(m)
        for j in L:
            seg = C[i:i + 8, j]
            last = np.where(~np.isnan(seg))[0][-1]
            r[j] = seg[last] / seg[0] - 1
        out.append((int(days[i]), float((nw * r).sum() - turn * COST), int(len(js)), k))
        w = nw
    return out


def tstat(v):
    v = np.asarray(v)
    return v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 2 else float("nan")


def half_sums(t, v):
    out = []
    for i, (nm, a) in enumerate(HALVES):
        a = int(a.replace(tzinfo=timezone.utc).timestamp() * 1000)
        b = int(HALVES[i + 1][1].replace(tzinfo=timezone.utc).timestamp() * 1000) if i + 1 < len(HALVES) else 1 << 62
        out.append((nm, float(v[(t >= a) & (t < b)].sum())))
    return out


def summary(name, t, v):
    eq = np.cumprod(1 + v)
    mdd = (eq / np.maximum.accumulate(eq) - 1).min()
    print(f"  {name:30} 연 {v.mean()*52*100:+7.1f}% · t {tstat(v):+.2f} · 누적 {(eq[-1]-1)*100:+7.1f}% · 최대낙폭 {mdd*100:+6.1f}% · "
          f"이익 주 {np.mean(v > 0):.0%} · 반기 " + " · ".join(f"{nm[2:]} {s*100:+.0f}" for nm, s in half_sums(t, v)))


def main():
    days, coins, C, Q, age = load_upbit()
    P, F = btc_binance(days)
    mom = run(days, C, Q, age)
    base = run(days, C, Q, age, mode="all")
    t = np.array([x[0] for x in mom])
    a = np.array([x[1] for x in mom]); b = np.array([x[1] for x in base])
    print(f"업비트 코인 {len(coins)} · 주 {len(t)} ({datetime.fromtimestamp(t[0]/1000, timezone.utc):%Y-%m-%d} ~) · "
          f"대상 중앙 {int(np.median([x[2] for x in mom]))}개 · 롱 {int(np.median([x[3] for x in mom]))}개 · 비용 {COST*100:.2f}%/회전\n")
    # BTC 숏 다리(주간): −(BTC 7일 수익) + 7일 펀딩 합(숏은 양의 펀딩을 받는다) − 재설정 비용
    hed = []
    for d in t:
        i = int(np.searchsorted(days, d))
        hed.append(-(P[i + 7] / P[i] - 1) + F[i + 1:i + 8].sum() - 2 * HEDGE_COST if np.isfinite(P[i]) and np.isfinite(P[i + 7]) else np.nan)
    hed = np.array(hed)
    upbtc = np.array([C[int(np.searchsorted(days, d)) + 7, coins.index("BTC")] / C[int(np.searchsorted(days, d)), coins.index("BTC")] - 1 for d in t])
    summary("① 모멘텀 상위 10% 롱만", t, a)
    summary("B 전체 동일 가중 롱(기준선)", t, b)
    summary("업비트 BTC 보유", t, upbtc)
    ex = a - b
    summary("① − B (선택 효과)", t, ex)
    ok = ~np.isnan(hed)
    two = a[ok] + hed[ok]
    summary("② ① 롱 + 바이낸스 BTC 숏", t[ok], two)
    q5 = np.array([x[1] for x in run(days, C, Q, age, frac=0.20)]) - b
    hs = dict(half_sums(t, ex))
    j1 = [tstat(ex) >= T_MIN, all(hs[h] > 0 for h in ("2025-H2", "2026-H1", "2026-H2")),
          np.prod(1 + np.delete(ex, ex.argmax())) - 1 > 0, q5.mean() > 0]
    hs2 = dict(half_sums(t[ok], two))
    j2 = [tstat(two) >= T_MIN, all(hs2[h] > 0 for h in ("2025-H2", "2026-H1", "2026-H2")), np.prod(1 + np.delete(two, two.argmax())) - 1 > 0]
    f = lambda xs_: "".join("○" if x else "×" for x in xs_)
    print(f"\n■ 판정 ① 선택 효과(① − B): {'✅ 합격' if all(j1) else '❌'} [{f(j1)}] · t {tstat(ex):+.2f} · 5분위 − B 연 {q5.mean()*52*100:+.1f}%")
    print(f"■ 판정 ② 롱 + BTC 숏: {'✅ 합격' if all(j2) else '❌'} [{f(j2)}] · t {tstat(two):+.2f}")
    print("\n■ 참고(판정 아님)")
    for lab, kw in (("거래대금 ≥ 10억", dict(minq=1e9)), ("거래대금 ≥ 100억", dict(minq=1e10)), ("상위 5%", dict(frac=0.05)), ("상위 20%", dict(frac=0.20))):
        r = run(days, C, Q, age, **kw)
        rb = {x[0]: x[1] for x in run(days, C, Q, age, mode="all", **({"minq": kw["minq"]} if "minq" in kw else {}))}
        v = np.array([x[1] - rb.get(x[0], np.nan) for x in r])
        v = v[np.isfinite(v)]
        print(f"  {lab:12} − B: 연 {v.mean()*52*100:+.1f}% · t {tstat(v):+.2f} · 주 {len(v)}")
    try:
        bd, bs, bC, bQ, bF, bage = xs.load()
        bm = {d: r for d, r, _ in x2.run(bd, bs, bC, bQ, bF, bage, "MOM")}
        both = [(x, bm[d]) for d, x in zip(t, ex) if d in bm]
        if len(both) > 10:
            u, v = np.array(both).T
            print(f"  바이낸스 XS MOM 과 주간 상관: 업비트 선택 효과(① − B) {np.corrcoef(u, v)[0, 1]:+.2f} · 겹치는 주 {len(both)}")
    except Exception as e:      # noqa: BLE001
        print(f"  바이낸스 XS MOM 상관 생략: {e}")


if __name__ == "__main__":
    main()
