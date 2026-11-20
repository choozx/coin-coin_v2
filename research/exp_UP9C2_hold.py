"""UP9C2 — UP9C '복귀 매수'의 보유를 늘리고 익절을 키운다 (사용자 선택, 2026-11-20).

UP9C: −2% 가짜 하락 뒤 기준가 복귀는 적중 18~27% 인데, 10분 보유로는 그날 펌핑의 일부만 먹었다(펌핑 23% 가 09:10 이후 시작).
→ 사용자 기준(보유 ≤ 10분)을 넘어, 복귀 매수 뒤 보유 시간·익절 폭을 넓혀 본다.

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
후보·경로·기준가·하락 감지·복귀 매수(E2)·비용 0.25%·창 두 개 = UP9C 와 같음(같은 데이터, 같은 함수).
청산    보유 M ∈ {10, 20, 30, 50분}(1분봉이 09:59 까지라 50분이 끝) 뒤 종가 · 그 전에 고가 ≥ 진입 × (1 + TP) 면 그 값(시가가 위면 시가), 진입 분엔 안 봄.
        TP ∈ {+5%, +10%, +15%, 없음}.
격자    N ∈ {5, 10} × D ∈ {0.5%, 1%, 2%} × M 4 × TP 4 = 96.
선택    선택 창(2025H2)에서 거래 30건 이상 중 거래당 순수익 최고 1개.
판정(확인 2026, 그 1개) UP9C 와 같은 ①~⑤(⑤ 기준 = 같은 N 08:59 매수 → 같은 M 분 뒤 종가, 하락 조건 없음).
⚠ 같은 2025~2026 데이터를 UP9·UP9X·UP9R·UP9F·UP9A·UP9C 에 이어 또 본다 — 통과해도 전진 기록 전엔 믿지 않는다.

    python3 -u -m research.exp_UP9C2_hold
"""
from __future__ import annotations

from itertools import product

import numpy as np

from research import exp_UP9_upbit_pump as up
from research.exp_UP9C_combo import COST, candidates, path, summ
from research.exp_UP9D_dip import conn as dconn

NS, DS, MS, TPS = (5, 10), (0.005, 0.01, 0.02), (10, 20, 30, 50), (0.05, 0.10, 0.15, None)


def sim(p, D, M, tp):
    O, Hh, L, C = p
    ref = C[0]
    kd = next((i for i in range(1, 4) if L[i] <= ref * (1 - D)), None)
    if kd is None:
        return None
    ie = next((i for i in range(kd + 1, 11) if Hh[i] >= ref), None)
    if ie is None:
        return None
    pe = max(O[ie], ref)
    end = min(ie + M, 60)
    px = C[end]
    if tp is not None:
        tgt = pe * (1 + tp)
        for i in range(ie + 1, end + 1):
            if Hh[i] >= tgt:
                px = O[i] if O[i] >= tgt else tgt
                break
    return px / pe - 1 - COST


def base(p, M):
    O, Hh, L, C = p
    return C[min(M, 60)] / C[0] - 1 - COST


def run(days, c):
    paths = {}
    for d, lst in days.items():
        for cn, y in lst:
            p = path(c, cn, d)
            if p is not None:
                paths[(cn, d)] = p
    res = {}
    for N, D, M, tp in product(NS, DS, MS, TPS):
        tr = []
        for d, lst in days.items():
            for cn, y in lst[:N]:
                if (cn, d) in paths and (r := sim(paths[(cn, d)], D, M, tp)) is not None:
                    tr.append((d, cn, y, r))
        res[(N, D, M, tp)] = tr
    bl = {(N, M): [(d, cn, y, base(paths[(cn, d)], M)) for d, lst in days.items() for cn, y in lst[:N] if (cn, d) in paths]
          for N in NS for M in MS}
    return res, bl


def lab(k):
    N, D, M, tp = k
    return f"N{N:2d} · −{D*100:.1f}% · {M:2d}분 · {'익절 없음' if tp is None else '익절 +%d%%' % round(tp*100)}"


def main():
    cands = candidates()
    c = dconn()
    out = {nm: run(days, c) for nm, days in cands.items()}
    c.close()
    rs, _ = out["선택 2025H2"]
    rc, blc = out["확인 2026"]
    print(" 조합 | 2025H2 건수 · 거래당 · 적중 | 2026 건수 · 거래당 · 날짜 t")
    for k in sorted(rs, key=lambda k: -(summ(rs[k]).get("mean", -9))):
        a, b = summ(rs[k]), summ(rc[k])
        if a["n"] and b["n"]:
            print(f"  {lab(k)} | {a['n']:4d} · {a['mean']*100:+6.2f}% · {a['hit']:.0%} | {b['n']:4d} · {b['mean']*100:+6.2f}% · {b['t']:+5.2f}")
    ok_k = [k for k, tr in rs.items() if len(tr) >= 30]
    best = max(ok_k, key=lambda k: summ(rs[k])["mean"])
    print(f"\n→ 선택 창 최고: {lab(best)} ({summ(rs[best])['mean']*100:+.2f}%/건)")
    s = summ(rc[best]); b = summ(blc[(best[0], best[2])])
    r, d, cc = s["r"], s["d"], s["c"]
    h1, h2 = r[d < up.MID], r[d >= up.MID]
    cut = np.sort(r)[: len(r) - max(1, len(r) // 20)]
    topc = max(set(cc), key=lambda x: np.sum(cc == x))
    ok = [s["t"] >= 2.0, len(h1) and h1.mean() > 0 and len(h2) and h2.mean() > 0, cut.mean() > 0, r[cc != topc].mean() > 0, s["mean"] > b["mean"]]
    print(f"■ 확인 2026: {s['n']}건 · {s['mean']*100:+.2f}%/건 · 중앙 {s['med']*100:+.2f} · 이익 {s['win']:.0%} · 날짜 t {s['t']:+.2f} · 적중 {s['hit']:.1%} · "
          f"최악 {r.min()*100:+.1f} · 최고 {r.max()*100:+.1f}")
    print(f"   전/후반 {h1.mean()*100:+.2f}/{h2.mean()*100:+.2f} · 상위 5% 빼면 {cut.mean()*100:+.2f} · 최다 코인 {topc} 빼면 {r[cc!=topc].mean()*100:+.2f} · "
          f"기준(08:59 → {best[2]}분) {b['mean']*100:+.2f}")
    labs = ["① t≥2", "② 전·후반", "③ 상위5% 제외", "④ 최다 코인 제외", "⑤ 기준보다"]
    print("   " + " · ".join(f"{a} {'✅' if o else '❌'}" for a, o in zip(labs, ok)) + f" → {'✅ 합격' if all(ok) else '❌ 불합격'}")
    pos = sum(1 for k in rc if summ(rc[k]).get("n") and summ(rc[k])["mean"] > 0)
    print(f"   참고: 96개 중 2026 거래당 플러스 {pos}개")


if __name__ == "__main__":
    main()
