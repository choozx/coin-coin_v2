"""UP9F — 업비트 9시 펌핑 후보를 고르는 '필터' 찾기 (사용자 요청 "펌핑 코인을 고르는 좋은 필터", 2026-11-20).

UP9 진단: 상위 5 적중 7.9% · 펌핑한 날 +9.8% · 안 터진 날 −1.05% → 손익분기 적중 9.6%.
좋은 필터 = 적중을 올리거나(①) 안 터진 날 손실을 줄인다(②). 둘 다 '08:59 매수 → 10:00 매도' 손익으로 합쳐진다.

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
다중 비교를 피하려고 기간을 셋으로 나눈다(2026 은 UP9·UP9X·진단·UP9R 에서 이미 봤다 — 고르는 데는 쓰지 않는다).
  학습  2025-01 ~ 2025-06  UP9 로지스틱(특징 13개, 같은 절차)과 아래 EV 모델을 여기서만 맞춘다.
  선택  2025-07 ~ 2025-12  필터 12개를 모두 적용해 '상위 5 순손익/일'이 가장 높은 필터 하나를 고른다.
  확인  2026-01 ~ 2026-11-18  고른 필터 하나만 판정.
공통    대상에서 스테이블코인 제외(UP9R 실수 수정: USDT + engine.xs_mom.STABLE — USDC·USD1 등) · 08시 봉 종가 매수 → 09시 봉 종가 매도 ·
        비용 왕복 0.25% · 매일 후보 풀(필터 통과) 안에서 로지스틱 점수 상위 5. 풀이 5개 미만이면 있는 만큼, 0개면 그날 쉼(0%).
필터(12, 문턱은 학습 기간 분포로 정함)
  F0  없음(기준 = UP9 로지스틱 상위 5)
  F1  유동성 하위 20% 제외          F2  유동성 상위 20% 제외
  F3  08시 거래대금 ≥ 평소 2배       F4  08시 수익 ≥ 0         F5  08시 수익 < 0
  F6  최근 7일 안에 펌핑(어느 시각이든)  F7  30일 고점 대비 −20% 이하   F8  30일 수익 ≤ 0
  F9  업비트 BTC 직전 8시간 ≥ 0     F10 바이낸스 선물 미상장
  F11 EV 모델(필터가 아니라 순위): EV = p·G + (1−p)·L(x). p = 로지스틱 확률, G = 학습 기간 펌핑한 날 평균 r,
      L(x) = 학습 기간 '안 터진 날' r 의 릿지 회귀(특징 18개, λ=1). EV 상위 5.
판정(확인 기간, 고른 필터 하나) ① 날짜 평균 t ≥ 2.0  ② 2026 전반(1~5월)·후반(6~11월) > 0  ③ 최다 코인 빼도 · 최고 날 빼도 > 0
        ④ 상위 5% 날 빼도 > 0  ⑤ F0(기준)보다 높다(날짜별 차이 t ≥ 2).
참고(판정 아님) 필터별 적중률·안 터진 날 평균을 세 기간 모두 · 확인 기간에 나머지 필터 성적(고르지 않은 것도, 비교용).

    .venv/bin/python -u -m research.exp_UP9F_filter
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from engine.xs_mom import STABLE
from research import exp_UP9_upbit_pump as up
from research.exp_UP9R_return import NEW, TEST1, extra, prep, ridge, tstat

FIT1 = int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
COST, N = 0.0025, 5
NAMES = ["F0 없음(기준)", "F1 유동성 하위20% 제외", "F2 유동성 상위20% 제외", "F3 08시 거래 ≥2배", "F4 08시 수익 ≥0",
         "F5 08시 수익 <0", "F6 최근 7일 펌핑", "F7 30일 고점 −20%↓", "F8 30일 수익 ≤0", "F9 BTC 8시간 ≥0",
         "F10 바이낸스 미상장", "F11 EV 모델"]


def run_day(score, pool, r, y):
    idx = np.where(pool)[0]
    if not len(idx):
        return 0.0, []
    tp = idx[np.argsort(-score[idx])[:N]]
    return float(np.mean(r[tp] - COST)), [(k, bool(y[k]), float(r[k] - COST)) for k in tp]


def series(score, mask_f, period, t, r, y, coin):
    out = {}
    for d in sorted(set(t[period])):
        day = period & (t == d) & np.isfinite(r)
        if day.sum() < 20:
            continue
        v, picks = run_day(score, day & mask_f, r, y)
        out[int(d)] = (v, [(coin[k], p, x) for k, p, x in picks])
    return out


def stats(s):
    v = np.array([x[0] for x in s.values()])
    items = [p for x in s.values() for p in x[1]]
    hit = np.mean([p for _, p, _ in items]) if items else np.nan
    miss = np.mean([x for _, p, x in items if not p]) if items else np.nan
    gain = np.mean([x for _, p, x in items if p]) if any(p for _, p, _ in items) else np.nan
    return v, hit, miss, gain


def main():
    rows = up.build()
    coin = np.array([r_[0] for r_ in rows]); t = np.array([r_[1] for r_ in rows]); y = np.array([r_[2] for r_ in rows])
    r = np.array([r_[3] for r_ in rows]) + 0.0010
    X13 = np.array([r_[4] for r_ in rows], dtype=float)
    ex = extra(list(zip(coin, t)))
    X5 = np.array([ex[(cn, tt)] for cn, tt in zip(coin, t)], dtype=float)
    keep = (t < TEST1) & ~np.isin(coin, list(STABLE | {"USDT"}))
    coin, t, y, r, X13, X5 = coin[keep], t[keep], y[keep], r[keep], X13[keep], X5[keep]
    fit, sel, te = t < FIT1, (t >= FIT1) & (t < up.TEST0), t >= up.TEST0
    print(f"코인·날짜 {len(t):,}(스테이블 제외) · 학습 {fit.sum():,}(펌핑 {y[fit].sum()}) · 선택 {sel.sum():,}({y[sel].sum()}) · 확인 {te.sum():,}({y[te].sum()})")
    Z13 = prep(X13, up.FEAT, fit)
    w = up.logit(Z13[fit], y[fit].astype(float))
    lg = np.hstack([Z13, np.ones((len(Z13), 1))]) @ w
    p = 1 / (1 + np.exp(-lg))
    print(f"로지스틱 AUC  선택 {up.auc(y[sel], lg[sel]):.3f} · 확인 {up.auc(y[te], lg[te]):.3f}")
    Z = prep(np.hstack([X13, X5]), up.FEAT + NEW, fit)
    ok = fit & np.isfinite(r)
    G = r[ok & y].mean()
    wl = ridge(Z[ok & ~y], r[ok & ~y])
    L = np.hstack([Z, np.ones((len(Z), 1))]) @ wl
    ev = p * G + (1 - p) * L
    print(f"EV 모델: 학습 펌핑한 날 평균 G {G*100:+.2f}% · 안 터진 날 평균 {r[ok & ~y].mean()*100:+.2f}%")
    F = {k: i for i, k in enumerate(up.FEAT)}
    liq = X13[:, F["liq"]]
    q20, q80 = np.nanquantile(liq[fit], [0.2, 0.8])
    lp, offp, h8v, h8r, btc = (X5[:, i] for i in range(5))
    masks = [np.ones(len(t), bool), liq >= q20, liq <= q80, h8v >= 2, h8r >= 0, h8r < 0, lp <= 7, offp <= -0.2,
             X13[:, F["ret30"]] <= 0, btc >= 0, X13[:, F["bnc"]] == 0, np.ones(len(t), bool)]
    masks = [np.nan_to_num(m, nan=0).astype(bool) for m in masks]
    scores = [lg] * 11 + [ev]
    print("\n필터 | 선택 기간(2025H2): 순손익/일 · t · 적중 · 안 터진 날 · 터진 날 | 학습 기간 순손익/일 · 적중")
    sel_res = []
    for i, nm in enumerate(NAMES):
        vs, hs, ms, gs = stats(series(scores[i], masks[i], sel, t, r, y, coin))
        vf, hf, _, _ = stats(series(scores[i], masks[i], fit, t, r, y, coin))
        sel_res.append(vs.mean())
        print(f"  {nm:22} | {vs.mean()*100:+.3f}% · {tstat(vs):+.2f} · {hs:5.1%} · {ms*100:+.2f}% · {gs*100:+.2f}% | {vf.mean()*100:+.3f}% · {hf:5.1%}")
    b = int(np.argmax(sel_res))
    print(f"\n→ 선택 기간 최고: {NAMES[b]} ({sel_res[b]*100:+.3f}%/일)")
    base = series(lg, masks[0], te, t, r, y, coin)
    top = series(scores[b], masks[b], te, t, r, y, coin)
    days = sorted(top)
    v = np.array([top[d][0] for d in days])
    h1 = [top[d][0] for d in days if d < up.MID]; h2 = [top[d][0] for d in days if d >= up.MID]
    cnt = {}
    for d in days:
        for cn, _, _ in top[d][1]:
            cnt[cn] = cnt.get(cn, 0) + 1
    topc = max(cnt, key=cnt.get) if cnt else None
    wo_c = [np.mean([x for cn, _, x in top[d][1] if cn != topc]) for d in days if any(cn != topc for cn, _, _ in top[d][1])]
    wo_d = np.delete(v, int(np.argmax(v)))
    cut = np.sort(v)[: int(len(v) * 0.95)]
    diff = np.array([top[d][0] - base[d][0] for d in days if d in base])
    _, hit, miss, gain = stats(top)
    okk = [tstat(v) >= 2.0, np.mean(h1) > 0 and np.mean(h2) > 0, np.mean(wo_c) > 0 and wo_d.mean() > 0, cut.mean() > 0, tstat(diff) >= 2]
    print(f"■ 확인(2026): {len(v)}일 · {v.mean()*100:+.3f}%/일 · 중앙 {np.median(v)*100:+.3f} · t {tstat(v):+.2f} · 전/후 {np.mean(h1)*100:+.3f}/{np.mean(h2)*100:+.3f}")
    print(f"   적중 {hit:.1%} · 터진 날 {gain*100:+.2f}% · 안 터진 날 {miss*100:+.2f}% · 최다 코인 {topc}({cnt.get(topc)}) 빼면 {np.mean(wo_c)*100:+.3f} · "
          f"최고 날 빼면 {wo_d.mean()*100:+.3f} · 상위 5% 날 빼면 {cut.mean()*100:+.3f} · 기준 대비 {diff.mean()*100:+.3f}%p (t {tstat(diff):+.2f})")
    lab = ["① t≥2", "② 전·후반", "③ 코인·날 제외", "④ 상위5% 날 제외", "⑤ 기준 대비 t≥2"]
    print("   " + " · ".join(f"{a} {'✅' if o else '❌'}" for a, o in zip(lab, okk)) + f" → {'✅ 합격' if all(okk) else '❌ 불합격'}")
    print("\n━━ 참고(판정 아님): 확인 기간의 모든 필터 ━━")
    for i, nm in enumerate(NAMES):
        vt, ht, mt, gt = stats(series(scores[i], masks[i], te, t, r, y, coin))
        print(f"  {nm:22} {vt.mean()*100:+.3f}%/일 · t {tstat(vt):+.2f} · 적중 {ht:5.1%} · 안 터진 날 {mt*100:+.2f}% · 터진 날 {gt*100:+.2f}%")


if __name__ == "__main__":
    main()
