"""UP9C3 — UP9C 복귀 매수 + 손절·익절(손익비), 보유 ≤ 10분 (사용자 요청, 2026-11-20).

UP9C2 는 보유·익절만 넓혔고 손절은 없었다. UP9X 의 손절 악화는 '08:59 미리 매수'(하락 전 진입)에서였다 —
복귀 매수는 가짜 하락을 지난 뒤 들어가므로 같은 흔들림에 털릴 가능성이 다를 수 있다.

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
후보·경로·기준가·하락 감지·복귀 매수(E2)·비용 0.25%·창 두 개 = UP9C 와 같음.
청산    진입 다음 분부터 최대 10분. 매 분: 저가 ≤ 진입 × (1 − SL) 면 손절(시가가 이미 아래면 시가), 아니면 고가 ≥ 진입 × (1 + TP) 면 익절(시가가 위면 시가).
        같은 분에 둘 다 닿으면 손절 먼저(보수적). 끝까지 안 닿으면 10분 뒤 종가.
격자    SL ∈ {1%, 2%, 3%} × TP ∈ {3%, 5%, 10%} × D ∈ {1%, 2%} × N ∈ {5, 10} = 36.
선택    선택 창(2025H2) 거래 30건 이상 중 거래당 순수익 최고 1개.
판정    확인 2026, UP9C 와 같은 ①~⑤(⑤ 기준 = 같은 N 08:59 → 09:10).
참고    손절 없는 같은 조합(UP9C2 의 10분 · 같은 TP)과 비교 · 손절·익절·시간 청산 비율.

    python3 -u -m research.exp_UP9C3_sltp
"""
from __future__ import annotations

from itertools import product

import numpy as np

from research import exp_UP9_upbit_pump as up
from research.exp_UP9C_combo import COST, base, candidates, path, summ
from research.exp_UP9D_dip import conn as dconn

SLS, TPS, DS, NS = (0.01, 0.02, 0.03), (0.03, 0.05, 0.10), (0.01, 0.02), (5, 10)
HOLD = 10


def sim(p, D, sl, tp):
    """반환 (순수익, 청산 종류) 또는 None."""
    O, Hh, L, C = p
    ref = C[0]
    kd = next((i for i in range(1, 4) if L[i] <= ref * (1 - D)), None)
    if kd is None:
        return None
    ie = next((i for i in range(kd + 1, 11) if Hh[i] >= ref), None)
    if ie is None:
        return None
    pe = max(O[ie], ref)
    end = min(ie + HOLD, 60)
    stop, tgt = (pe * (1 - sl) if sl else None), pe * (1 + tp) if tp else None
    for i in range(ie + 1, end + 1):
        if stop is not None and L[i] <= stop:
            return (min(O[i], stop) / pe - 1 - COST, "손절")
        if tgt is not None and Hh[i] >= tgt:
            return (max(O[i], tgt) / pe - 1 - COST, "익절")
    return (C[end] / pe - 1 - COST, "시간")


def run(days, c):
    paths = {}
    for d, lst in days.items():
        for cn, y in lst:
            p = path(c, cn, d)
            if p is not None:
                paths[(cn, d)] = p
    res, kinds = {}, {}
    for N, D, sl, tp in list(product(NS, DS, SLS, TPS)) + [(N, D, None, tp) for N, D, tp in product(NS, DS, TPS)]:
        tr, kd = [], []
        for d, lst in days.items():
            for cn, y in lst[:N]:
                if (cn, d) in paths and (x := sim(paths[(cn, d)], D, sl, tp)) is not None:
                    tr.append((d, cn, y, x[0]))
                    kd.append(x[1])
        res[(N, D, sl, tp)], kinds[(N, D, sl, tp)] = tr, kd
    bl = {N: [(d, cn, y, base(paths[(cn, d)])) for d, lst in days.items() for cn, y in lst[:N] if (cn, d) in paths] for N in NS}
    return res, kinds, bl


def lab(k):
    N, D, sl, tp = k
    return f"N{N:2d} · −{D*100:.0f}% · 손절 {'없음' if sl is None else '-%d%%' % round(sl*100)} · 익절 +{tp*100:.0f}%"


def main():
    cands = candidates()
    c = dconn()
    out = {nm: run(days, c) for nm, days in cands.items()}
    c.close()
    rs, ks, _ = out["선택 2025H2"]
    rc, kc, blc = out["확인 2026"]
    print(" 조합 | 2025H2 건수 · 거래당 · 이익 | 2026 건수 · 거래당 · 이익 · 날짜 t | 2026 손절/익절/시간")
    grid = [k for k in rs if k[2] is not None]
    for k in sorted(grid, key=lambda k: -(summ(rs[k]).get("mean", -9))):
        a, b = summ(rs[k]), summ(rc[k])
        if a["n"] and b["n"]:
            kk = kc[k]
            fr = " / ".join(f"{np.mean([x == z for x in kk]):.0%}" for z in ("손절", "익절", "시간"))
            print(f"  {lab(k)} | {a['n']:4d} · {a['mean']*100:+6.2f}% · {a['win']:.0%} | {b['n']:4d} · {b['mean']*100:+6.2f}% · {b['win']:.0%} · {b['t']:+5.2f} | {fr}")
    print("\n 참고: 손절 없음(같은 10분·같은 익절)")
    for k in sorted([k for k in rs if k[2] is None]):
        a, b = summ(rs[k]), summ(rc[k])
        if a["n"] and b["n"]:
            print(f"  {lab(k)} | 2025H2 {a['mean']*100:+.2f}% · 2026 {b['mean']*100:+.2f}% (t {b['t']:+.2f})")
    ok_k = [k for k in grid if len(rs[k]) >= 30]
    best = max(ok_k, key=lambda k: summ(rs[k])["mean"])
    print(f"\n→ 선택 창 최고: {lab(best)} ({summ(rs[best])['mean']*100:+.2f}%/건)")
    s = summ(rc[best]); b = summ(blc[best[0]])
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
    pos = sum(1 for k in grid if summ(rc[k]).get("n") and summ(rc[k])["mean"] > 0)
    print(f"   참고: 36개 중 2026 거래당 플러스 {pos}개")


if __name__ == "__main__":
    main()
