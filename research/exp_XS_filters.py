"""XS MOM 필터 3종 — 현행 규칙보다 '확실히' 나은가 (사용자 요청, 2026-11-19).

현행(exp_XS_cross_section MOM, engine.xs_mom 규칙)과 같은 데이터·대상·비용·리밸런스(월요일 종가)·10%·동일 가중.
바꾸는 것은 '어떤 코인을 롱/숏으로 고르나' 하나뿐. 개수 k = int(대상 수 × 10%) 는 현행과 같게 유지
(필터로 빠진 코인 자리는 순위상 다음 코인이 채운다).

━━ 사전 고정 (2026-11-19 작성 · 결과 보기 전) ━━
FUNDF  펀딩 필터(F1+F2 한 묶음). 신호일까지 7일 펀딩 합 F7(롱이 내는 쪽이 +).
       숏 후보 중 F7 ≤ −0.30% 제외(펀딩을 계속 내야 하고 스퀴즈 꼬리 — NS 결과).
       롱 후보 중 F7 ≥ +0.60% 제외(평상시 0.01%/8h = 7일 +0.21% 의 약 3배 = 과열).
VOLADJ 변동성 조정 순위. ln(C[i]/C[i−28]) ÷ (최근 28개 일간 로그수익률 표준편차 × √28). 일간 수익률 20개 미만이면 대상 제외.
SKIP3  최근 3일 제외 순위. C[i−3]/C[i−28] − 1. (체결은 현행처럼 월요일 종가)
판정(필터마다, 넷 다 — 3개 동시 검정)
  ① 개선의 t ≥ 2.4 : 주별 d = r(필터) − r(현행) 의 평균/표준오차 (같은 주끼리 짝지음)
  ② 개선이 반기 2025-H2 · 2026-H1 · 2026-H2 모두 > 0 (d 합)
  ③ 가장 큰 개선 주 1개를 빼도 d 평균 > 0
  ④ 필터 자체가 현행 합격 기준을 그대로 통과(t ≥ 2.4 · 위 3반기 > 0 · 최고 기간 빼도 > 0 · 20%로 해도 연 > 0)
합격해도 실매매 규칙은 바꾸지 않는다 — 종이 기록 병행 후보가 될 뿐.
참고(판정 아님): 셋 다 조합 · SKIP 1·5일 · 펀딩 필터 한쪽씩 · 최대낙폭 · 다리별.

    python3 -u -m research.exp_XS_filters
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from engine.xs_mom import AGE, COST, MINQ
from research.exp_XS_cross_section import HALVES, START, _ms, judge, load

D = 86_400_000
LOOK, HOLD = 28, 7
SHORT_F, LONG_F = -0.0030, 0.0060


def run(days, C, Q, Fd, age, sig_kind="MOM", fund=(False, False), skip=0, frac=0.10, cost=COST):
    n, m = C.shape
    i0 = int(np.searchsorted(days, START))
    while datetime.fromtimestamp(days[i0] / 1000, timezone.utc).weekday() != 0:
        i0 += 1
    lr = np.diff(np.log(C), axis=0)                             # lr[i-1] = ln(C[i]/C[i-1])
    w = np.zeros(m)
    out = []
    for i in range(i0, n - HOLD, 7):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - LOOK]) & (age[i] >= AGE)
        live &= np.nan_to_num(np.nanmedian(Q[i - 29:i + 1], axis=0)) >= MINQ
        if skip:
            live &= ~np.isnan(C[i - skip])
        if sig_kind == "VOLADJ":
            seg = lr[i - LOOK:i]                                 # 일간 로그수익률 28개(i−27..i)
            cnt = np.sum(~np.isnan(seg), axis=0)
            live &= cnt >= 20
        js = np.where(live)[0]
        if len(js) < 20:
            continue
        if sig_kind == "VOLADJ":
            sd = np.nanstd(seg[:, js], axis=0, ddof=1) * np.sqrt(LOOK)
            sig = np.log(C[i, js] / C[i - LOOK, js]) / np.where(sd > 0, sd, np.nan)
            sig = np.nan_to_num(sig, nan=0.0)
        else:
            sig = C[i - skip, js] / C[i - LOOK, js] - 1
        k = max(1, int(len(js) * frac))
        order = js[np.argsort(-sig)]
        f7 = Fd[i - 6:i + 1].sum(0)
        L = [j for j in order if not (fund[1] and f7[j] >= LONG_F)][:k]
        S = [j for j in order[::-1] if not (fund[0] and f7[j] <= SHORT_F)][:k]
        nw = np.zeros(m)
        nw[L] = 0.5 / k
        nw[S] = -0.5 / k
        turn = np.abs(nw - w).sum()
        r = np.zeros(m)
        for j in np.where(nw != 0)[0]:
            seg2 = C[i:i + HOLD + 1, j]
            last = np.where(~np.isnan(seg2))[0][-1]
            r[j] = seg2[last] / seg2[0] - 1 - Fd[i + 1:i + 1 + last, j].sum()
        rl = (nw[nw > 0] * r[nw > 0]).sum() * 2
        rs = (nw[nw < 0] * r[nw < 0]).sum() * 2
        out.append((int(days[i]), (nw * r).sum() - turn * cost, turn, rl, rs))
        w = nw
    return out


def compare(base, res, title):
    tb = {x[0]: x[1] for x in base}
    t = np.array([x[0] for x in res])
    d = np.array([x[1] - tb[x[0]] for x in res])
    td = d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))
    halves = []
    for i, (nm, y, mo) in enumerate(HALVES):
        a = _ms(y, mo)
        b = _ms(*HALVES[i + 1][1:]) if i + 1 < len(HALVES) else 1 << 62
        halves.append((nm, d[(t >= a) & (t < b)]))
    c1 = td >= 2.4
    c2 = all(len(h) and h.sum() > 0 for _, h in halves[3:])
    c3 = np.delete(d, d.argmax()).mean() > 0
    print(f"   개선 d: 연 {d.mean()*52*100:+.1f}%p · t {td:+.2f} · 개선 주 {np.mean(d>0):.0%} · "
          + " · ".join(f"{nm} {h.sum()*100:+.1f}" for nm, h in halves))
    print(f"   ① 개선 t≥2.4 {'✅' if c1 else '❌'} · ② 개선 25H2·26H1·26H2 {'✅' if c2 else '❌'} · ③ 최대 개선 주 빼도 {'✅' if c3 else '❌'}")
    return c1 and c2 and c3


def legs(res):
    return f"롱 다리 연 {np.mean([x[3] for x in res])*52*50:+.1f}% · 숏 다리 연 {np.mean([x[4] for x in res])*52*50:+.1f}% (총 노출 기여)"


def main():
    days, syms, C, Q, Fd, age = load()
    print(f"심볼 {len(syms)}개 · {len(days)}일")
    base = run(days, C, Q, Fd, age)
    judge(base, "현행 MOM (재현 확인 — 연 +63.1% 이어야 함)")
    print("   " + legs(base))
    tests = [("FUNDF 펀딩 필터(숏 F7≤−0.3% · 롱 F7≥+0.6% 제외)", dict(fund=(True, True))),
             ("VOLADJ 변동성 조정 순위", dict(sig_kind="VOLADJ")),
             ("SKIP3 최근 3일 제외", dict(skip=3))]
    verdict = {}
    for title, kw in tests:
        res = run(days, C, Q, Fd, age, **kw)
        q5 = run(days, C, Q, Fd, age, frac=0.20, **kw)
        ok4 = judge(res, title, extra=np.mean([x[1] for x in q5]) * 52)
        print("   " + legs(res))
        ok = compare(base, res, title) and ok4
        print(f"   ④ 필터 자체 합격 {'✅' if ok4 else '❌'} → 판정 {'✅ 합격(종이 병행 후보)' if ok else '❌ 불합격'}")
        verdict[title] = ok
    print("\n━━ 참고(판정 아님) ━━")
    for title, kw in [("숏 펀딩 필터만", dict(fund=(True, False))), ("롱 펀딩 필터만", dict(fund=(False, True))),
                      ("SKIP1", dict(skip=1)), ("SKIP5", dict(skip=5)),
                      ("셋 다 조합(VOLADJ+FUNDF, SKIP 은 VOLADJ 와 겹쳐 제외)", dict(sig_kind="VOLADJ", fund=(True, True)))]:
        res = run(days, C, Q, Fd, age, **kw)
        judge(res, title)
        compare(base, res, title)
    print("\n판정: " + " · ".join(f"{k.split()[0]} {'✅' if v else '❌'}" for k, v in verdict.items()))


if __name__ == "__main__":
    main()
