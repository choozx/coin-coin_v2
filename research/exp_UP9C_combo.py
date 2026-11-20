"""UP9C — 후보 선택(UP9 모델 상위) + 9시 '가짜 하락' 패턴, 10분 이내 보유 (사용자 요청 "조합 확인", 2026-11-20).

UP9: 모델 상위 5 펌핑률 7.9%(평소 7배). UP9D: 펌핑 전 73% 가 08:59 종가 아래로 내려감(중앙 3틱, 내려간 경우 7틱 · −0.95%),
깊은 하락(−1~2%)은 펌핑 날에 2배 흔함. → 둘을 겹치면 '터질 코인'을 더 좁힐 수 있나. 보유는 사용자 기준 최대 10분.

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
후보    UP9 로지스틱(13개 특징, 스테이블 제외) 매일 점수 상위 N. 창 두 개:
          선택 창 = 학습 2025H1 → 2025-07~12 에서 조합을 고른다 · 확인 창 = 학습 2025 → 2026-01~11-18 에서 고른 것 하나만 판정.
경로    업비트 1분봉 08:58~09:59(k = −1 은 08:59). 체결 없는 분은 직전 종가로 채움(고·저 = 종가).
기준가  08:59 종가. 하락 감지 창 = 09:00~09:02(k 0~2). 하락 = 그 분 저가 ≤ 기준가 × (1 − D).
진입    E1 '하락 매수': 하락선 지정가 매수(그 분 시가가 이미 아래면 시가). 진입 분 = 하락 분.
        E2 '복귀 매수': 하락 분 '다음 분'부터 09:09 안에 고가가 기준가에 닿는 첫 분, 기준가 역지정 매수(시가가 위면 시가).
청산    T '10분': 진입 분 + 10분 종가.   P '익절 +5%': 진입 다음 분부터 10분 안 고가 ≥ 진입 × 1.05 면 그 값(시가가 위면 시가), 아니면 10분 종가.
        같은 분 안 순서는 모르므로 진입 분엔 익절 안 봄(보수적).
격자    N ∈ {5, 10} × D ∈ {0.5%, 1%, 2%} × 진입 {E1, E2} × 청산 {T, P} = 24 · 비용 왕복 0.25%.
선택    선택 창에서 거래 30건 이상인 조합 중 거래당 순수익 최고.
판정(확인 창, 고른 조합) ① 날짜 묶음 t ≥ 2.0  ② 2026 전반(1~5월)·후반(6~11월) > 0  ③ 상위 5% 거래 빼도 > 0  ④ 최다 코인 빼도 > 0
        ⑤ 같은 N 후보를 08:59 종가 매수 → 09:10 종가 매도(하락 조건 없음) 보다 거래당 높다.
참고    두 창의 24개 전부 · 적중(그날 펌핑) 비율 · 하락 조건이 걸린 비율.

    python3 -u -m research.exp_UP9C_combo --collect     # 후보 상위 10 의 1분봉(UP9D fetch)
    python3 -u -m research.exp_UP9C_combo
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from itertools import product

import numpy as np

from engine.xs_mom import STABLE
from research import exp_UP9_upbit_pump as up
from research.exp_UP9D_dip import conn as dconn, fetch
from research.exp_UP9R_return import TEST1, prep, tstat

FIT1 = int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
NS, DS, ENS, EXS = (5, 10), (0.005, 0.01, 0.02), ("E1", "E2"), ("T", "P")
COST, TP = 0.0025, 0.05


def candidates():
    rows = up.build()
    coin = np.array([r[0] for r in rows]); t = np.array([r[1] for r in rows]); y = np.array([r[2] for r in rows])
    X = np.array([r[4] for r in rows], dtype=float)
    keep = (t < TEST1) & ~np.isin(coin, list(STABLE | {"USDT"}))
    coin, t, y, X = coin[keep], t[keep], y[keep], X[keep]
    out = {}
    for nm, fit, te in (("선택 2025H2", t < FIT1, (t >= FIT1) & (t < up.TEST0)), ("확인 2026", t < up.TEST0, t >= up.TEST0)):
        Z = prep(X, up.FEAT, fit)
        w = up.logit(Z[fit], y[fit].astype(float))
        s = np.hstack([Z, np.ones((len(Z), 1))]) @ w
        days = {}
        for d in sorted(set(t[te])):
            idx = np.where(te & (t == d))[0]
            if len(idx) < 20:
                continue
            tp = idx[np.argsort(-s[idx])[:max(NS)]]
            days[int(d)] = [(coin[k], bool(y[k])) for k in tp]
        out[nm] = days
    return out


def collect():
    c = dconn()
    done = {(a, b) for a, b in c.execute("SELECT coin, d FROM up9d_done")}
    jobs = sorted({(cn, d) for days in candidates().values() for d, lst in days.items() for cn, _ in lst} - done)
    print(f"후보 1분봉 받을 것 {len(jobs)}", flush=True)
    for n, (cn, d) in enumerate(jobs, 1):
        rows = fetch(cn, d)
        time.sleep(0.11)
        c.executemany("INSERT OR REPLACE INTO up9d_1m VALUES (?,?,?,?,?,?,?,?)", [(cn, d, 2, *r) for r in rows])
        c.execute("INSERT OR REPLACE INTO up9d_done VALUES (?,?,?,?)", (cn, d, 2, len(rows)))
        if n % 200 == 0:
            c.commit()
            print(f"  {n}/{len(jobs)}", flush=True)
    c.commit()
    c.close()


def path(c, cn, d):
    a = c.execute("SELECT k, o, h, l, c FROM up9d_1m WHERE coin=? AND d=? ORDER BY k", (cn, d)).fetchall()
    m = {r[0]: r[1:] for r in a}
    if -1 not in m:
        return None
    O, Hh, L, C = (np.full(61, np.nan) for _ in range(4))      # 인덱스 = k + 1 (k −1 … 59)
    last = m[-1][3]
    for k in range(-1, 60):
        if k in m:
            O[k + 1], Hh[k + 1], L[k + 1], C[k + 1] = m[k]
            last = m[k][3]
        else:
            O[k + 1] = Hh[k + 1] = L[k + 1] = C[k + 1] = last
    return O, Hh, L, C


def sim(p, D, en, ex):
    """반환 순수익(없으면 None). 분 인덱스 i = k + 1."""
    O, Hh, L, C = p
    ref = C[0]
    lvl = ref * (1 - D)
    kd = next((i for i in range(1, 4) if L[i] <= lvl), None)      # k 0~2
    if kd is None:
        return None
    if en == "E1":
        ie, pe = kd, min(O[kd], lvl)
    else:
        ie = next((i for i in range(kd + 1, 11) if Hh[i] >= ref), None)   # 다음 분 ~ k 9
        if ie is None:
            return None
        pe = max(O[ie], ref)
    end = ie + 10
    if end > 60:
        return None
    px = C[end]
    if ex == "P":
        tgt = pe * (1 + TP)
        for i in range(ie + 1, end + 1):
            if Hh[i] >= tgt:
                px = max(O[i], tgt) if O[i] >= tgt else tgt
                break
    return px / pe - 1 - COST


def base(p):
    O, Hh, L, C = p
    return C[10] / C[0] - 1 - COST                                  # 08:59 → 09:09 종가(= 09:10)


def evaluate(days, c):
    paths = {}
    for d, lst in days.items():
        for cn, y in lst:
            p = path(c, cn, d)
            if p is not None:
                paths[(cn, d)] = (p, y)
    res = {}
    for N, D, en, ex in product(NS, DS, ENS, EXS):
        tr = []
        for d, lst in days.items():
            for cn, y in lst[:N]:
                if (cn, d) in paths:
                    r = sim(paths[(cn, d)][0], D, en, ex)
                    if r is not None:
                        tr.append((d, cn, y, r))
        res[(N, D, en, ex)] = tr
    bl = {N: [(d, cn, y, base(paths[(cn, d)][0])) for d, lst in days.items() for cn, y in lst[:N] if (cn, d) in paths] for N in NS}
    return res, bl, len(paths)


def summ(tr):
    if not tr:
        return dict(n=0)
    r = np.array([x[3] for x in tr]); d = np.array([x[0] for x in tr])
    u, inv = np.unique(d, return_inverse=True)
    dm = np.bincount(inv, r) / np.bincount(inv)
    return dict(n=len(r), mean=r.mean(), med=np.median(r), win=np.mean(r > 0), t=tstat(dm), hit=np.mean([x[2] for x in tr]), r=r, d=d,
                c=np.array([x[1] for x in tr]))


def lab(k):
    N, D, en, ex = k
    return f"N{N:2d} · −{D*100:.1f}% · {'하락 매수' if en=='E1' else '복귀 매수'} · {'10분' if ex=='T' else '익절+5%'}"


def main():
    if "--collect" in sys.argv:
        return collect()
    cands = candidates()
    c = dconn()
    out = {nm: evaluate(days, c) for nm, days in cands.items()}
    c.close()
    for nm, (res, bl, npth) in out.items():
        print(f"\n■ {nm} — 후보 경로 {npth}개")
        for N in NS:
            s = summ(bl[N])
            print(f"  기준(N{N} 08:59 매수 → 09:10): {s['n']}건 · {s['mean']*100:+.2f}%/건 · 중앙 {s['med']*100:+.2f} · 적중 {s['hit']:.1%}")
        print("  조합 | 건수 | 거래당 | 중앙 | 이익 | 날짜 t | 적중(펌핑)")
        for k, tr in res.items():
            s = summ(tr)
            if s["n"]:
                print(f"  {lab(k)} | {s['n']:4d} | {s['mean']*100:+6.2f}% | {s['med']*100:+6.2f} | {s['win']:.0%} | {s['t']:+5.2f} | {s['hit']:.1%}")
    res_s, _, _ = out["선택 2025H2"]
    ok_k = [k for k, tr in res_s.items() if len(tr) >= 30]
    best = max(ok_k, key=lambda k: summ(res_s[k])["mean"])
    print(f"\n→ 선택 창 최고: {lab(best)} ({summ(res_s[best])['mean']*100:+.2f}%/건)")
    res_c, bl_c, _ = out["확인 2026"]
    s = summ(res_c[best]); b = summ(bl_c[best[0]])
    r, d, cc = s["r"], s["d"], s["c"]
    h1, h2 = r[d < up.MID], r[d >= up.MID]
    cut = np.sort(r)[: len(r) - max(1, len(r) // 20)]
    topc = max(set(cc), key=lambda x: np.sum(cc == x))
    ok = [s["t"] >= 2.0, len(h1) and h1.mean() > 0 and len(h2) and h2.mean() > 0, cut.mean() > 0, r[cc != topc].mean() > 0, s["mean"] > b["mean"]]
    print(f"■ 확인 2026: {s['n']}건 · {s['mean']*100:+.2f}%/건 · 중앙 {s['med']*100:+.2f} · 이익 {s['win']:.0%} · 날짜 t {s['t']:+.2f} · 적중 {s['hit']:.1%}")
    print(f"   전/후반 {h1.mean()*100:+.2f}/{h2.mean()*100:+.2f} · 상위 5% 빼면 {cut.mean()*100:+.2f} · 최다 코인 {topc} 빼면 {r[cc!=topc].mean()*100:+.2f} · "
          f"기준(08:59→09:10) {b['mean']*100:+.2f}")
    labs = ["① t≥2", "② 전·후반", "③ 상위5% 제외", "④ 최다 코인 제외", "⑤ 기준보다"]
    print("   " + " · ".join(f"{a} {'✅' if o else '❌'}" for a, o in zip(labs, ok)) + f" → {'✅ 합격' if all(ok) else '❌ 불합격'}")


if __name__ == "__main__":
    main()
