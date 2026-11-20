"""UP9A — '세력 매집' 지표 2개를 제대로 만들어 업비트 9시 펌핑 예측에 보탬이 되는지 (사용자 선택 1·2번, 2026-11-20).

UP9 의 매집 대용(양봉/음봉 거래대금 · 순매수 압력)은 단독으론 펌핑률 2배였지만 다른 특징과 겹쳐 모델 기여가 작았다.
진짜 매집의 모습 = '가격은 안 오르는데 물량이 흡수된다'. 두 가지로 잰다.

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
데이터·단위  UP9 와 같음(업비트 KRW 1시간봉, 코인·날짜, 08:59 KST 까지 정보만, 스테이블 제외 — USDT + xs_mom.STABLE).
지표 1  OBV 다이버전스  nf30 = 최근 30일(720시간) Σ 부호(종가 − 직전 종가) × 거래대금 ÷ Σ 거래대금 (OBV 30일 증가분 비율, −1~+1)
        점수 obv_div = 백분위(nf30) − 백분위(ret30)  (높을수록 '돈은 들어오는데 가격은 안 올랐다')
        조건 OBVD = nf30 ≥ 학습 상위 20% 문턱 그리고 ret30 ≤ 0.
지표 2  흡수형 횡보  vr = 최근 30일 평균 시간 거래대금 ÷ 그 전 31~120일 평균 · rr = 최근 30일 (최고−최저)/현재가 ÷ 31~120일 같은 값
        점수 absorb = 백분위(vr) − 백분위(rr)  (높을수록 '거래는 늘었는데 변동폭은 줄었다')
        조건 ABS = vr ≥ 1 그리고 rr ≤ 학습 하위 20% 문턱.
백분위·문턱은 학습 기간 분포로 정한다.
판정 A '매집 지표가 펌핑을 알려 주나' (둘 다 각각)
  ① 점수 상위 20% 펌핑률 ÷ 전체 ≥ 1.5배 — 2025 와 2026 둘 다
  ② UP9 로지스틱(13개)에 이 점수 하나를 더하면 시험 AUC 가 +0.010 이상 — 두 창 모두(학습 2025H1 → 2025H2, 학습 2025 → 2026)
판정 B '돈이 되나' (둘 다 각각, 시험 2026, 08:59 → 10:00, 비용 0.25%, 매일 상위 5)
  (i) 13개 + 점수 모델 상위 5  (ii) UP9 로지스틱 상위 5 중 조건(OBVD 또는 ABS) 통과 코인만(없으면 그날 쉼)
  합격 = ① 날짜 평균 t ≥ 2.3(4개 비교)  ② 2026 전반·후반 > 0  ③ 상위 5% 날 빼도 > 0  ④ UP9 로지스틱 상위 5보다 높다(차이 t ≥ 2).
참고(판정 아님) 조건 통과 코인 수·펌핑률 · 두 지표 상관 · 특징 계수.

    .venv/bin/python -u -m research.exp_UP9A_accum      (또는 python3 — sklearn 안 씀)
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from engine.xs_mom import STABLE
from research import exp_UP9_upbit_pump as up
from research.exp_UP9R_return import TEST1, prep, series, tstat

H = up.H
FIT1 = int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
Y26 = up.TEST0
COST, N = 0.0025, 5


def accum(keys):
    """(coin, t) → [nf30, vr, rr]."""
    c = up.conn()
    t_lo = up.START // H * H
    nh = (int(time.time() * 1000) // H * H - t_lo) // H + 1
    by = defaultdict(list)
    for cn, t in keys:
        by[cn].append(t)
    out = {}
    for cn, ts in by.items():
        O, Hh, C, V = series(c, cn, t_lo, nh)
        Vn = np.nan_to_num(V)
        Cf = C.copy()
        for i in range(1, nh):                              # 거래 없는 시간은 직전 종가
            if np.isnan(Cf[i]):
                Cf[i] = Cf[i - 1]
        dC = np.r_[0.0, np.diff(Cf)]
        inc = np.nan_to_num(np.sign(dC)) * Vn
        cI, cV = np.cumsum(inc), np.cumsum(Vn)
        for t in ts:
            j = (t - t_lo) // H - 1
            if j < 2880:
                out[(cn, t)] = [np.nan] * 3
                continue
            v30 = cV[j] - cV[j - 720]
            nf = (cI[j] - cI[j - 720]) / v30 if v30 > 0 else np.nan
            vp = (cV[j - 720] - cV[j - 2880]) / 2160
            vr = (v30 / 720) / vp if vp > 0 else np.nan
            p30, pp = Cf[j - 719:j + 1], Cf[j - 2879:j - 719]
            r30 = (np.nanmax(p30) - np.nanmin(p30)) / Cf[j] if Cf[j] > 0 else np.nan
            rpp = (np.nanmax(pp) - np.nanmin(pp)) / Cf[j - 720] if Cf[j - 720] > 0 else np.nan
            out[(cn, t)] = [nf, vr, r30 / rpp if rpp and rpp > 0 else np.nan]
    c.close()
    return out


def pct(x, ref):
    s = np.sort(ref[np.isfinite(ref)])
    return np.where(np.isfinite(x), np.searchsorted(s, x) / len(s), np.nan)


def top5(score, coin, t, r, y, te, allow=None):
    out = {}
    for d in sorted(set(t[te])):
        idx = np.where(te & (t == d) & np.isfinite(r))[0]
        if len(idx) < 20:
            continue
        tp = idx[np.argsort(-score[idx])[:N]]
        if allow is not None:
            tp = tp[allow[tp]]
        out[int(d)] = (float(np.mean(r[tp] - COST)) if len(tp) else 0.0, [(coin[k], bool(y[k])) for k in tp])
    return out


def judge(s, base):
    days = sorted(s)
    v = np.array([s[d][0] for d in days])
    h1 = [s[d][0] for d in days if d < up.MID]; h2 = [s[d][0] for d in days if d >= up.MID]
    cut = np.sort(v)[: int(len(v) * 0.95)]
    diff = np.array([s[d][0] - base[d][0] for d in days if d in base])
    items = [p for d in days for _, p in s[d][1]]
    ok = [tstat(v) >= 2.3, np.mean(h1) > 0 and np.mean(h2) > 0, cut.mean() > 0, tstat(diff) >= 2]
    return v, ok, diff, (np.mean(items) if items else np.nan, len(items))


def main():
    rows = up.build()
    coin = np.array([x[0] for x in rows]); t = np.array([x[1] for x in rows]); y = np.array([x[2] for x in rows])
    r = np.array([x[3] for x in rows]) + 0.0010
    X13 = np.array([x[4] for x in rows], dtype=float)
    keep = (t < TEST1) & ~np.isin(coin, list(STABLE | {"USDT"}))
    coin, t, y, r, X13 = coin[keep], t[keep], y[keep], r[keep], X13[keep]
    A = accum(list(zip(coin, t)))
    nf, vr, rr = (np.array([A[(cn, tt)][k] for cn, tt in zip(coin, t)], dtype=float) for k in range(3))
    ret30 = X13[:, up.FEAT.index("ret30")]
    print(f"코인·날짜 {len(t):,}(스테이블 제외) · 펌핑 {y.sum()} · 지표 결측 nf {np.mean(~np.isfinite(nf)):.1%} · vr {np.mean(~np.isfinite(vr)):.1%}")
    windows = [("학습 2025H1 → 시험 2025H2", t < FIT1, (t >= FIT1) & (t < Y26)), ("학습 2025 → 시험 2026", t < Y26, t >= Y26)]
    res = {}
    for nm, fitm, tem in windows:
        obv = pct(nf, nf[fitm]) - pct(ret30, ret30[fitm])
        ab = pct(vr, vr[fitm]) - pct(rr, rr[fitm])
        Z13 = prep(X13, up.FEAT, fitm)
        w = up.logit(Z13[fitm], y[fitm].astype(float))
        s0 = np.hstack([Z13, np.ones((len(Z13), 1))]) @ w
        a0 = up.auc(y[tem], s0[tem])
        out = dict(s0=s0, a0=a0, fit=fitm, te=tem)
        for key, sc in (("obv", obv), ("abs", ab)):
            Z = prep(np.c_[X13, sc], up.FEAT + [key], fitm)
            w2 = up.logit(Z[fitm], y[fitm].astype(float))
            s1 = np.hstack([Z, np.ones((len(Z), 1))]) @ w2
            out[key] = dict(score=sc, s1=s1, auc=up.auc(y[tem], s1[tem]), coef=w2[len(up.FEAT)])
        res[nm] = out
    base_rate = {}
    print("\n판정 A — 점수 상위 20% 펌핑률 배수 (연도별) · AUC 증가")
    okA = {}
    for key, lab in (("obv", "1 OBV 다이버전스"), ("abs", "2 흡수형 횡보")):
        lifts = []
        for yr_nm, ym in (("2025", t < Y26), ("2026", t >= Y26)):
            sc = res["학습 2025 → 시험 2026"][key]["score"] if yr_nm == "2026" else res["학습 2025H1 → 시험 2025H2"][key]["score"]
            ok_ = ym & np.isfinite(sc)
            q = np.nanquantile(sc[ok_], 0.8)
            lift = y[ok_ & (sc >= q)].mean() / y[ok_].mean()
            qs = np.nanquantile(sc[ok_], [0.2, 0.4, 0.6, 0.8])
            b = np.digitize(sc[ok_], qs)
            lifts.append(lift)
            print(f"  {lab} {yr_nm}: 5분위 펌핑률 배수 " + " · ".join(f"Q{k+1} {y[ok_][b==k].mean()/y[ok_].mean():.1f}" for k in range(5)))
        da = [res[w][key]["auc"] - res[w]["a0"] for w in res]
        okA[key] = [all(l >= 1.5 for l in lifts), all(d >= 0.010 for d in da)]
        print(f"     AUC: " + " · ".join(f"{w} {res[w]['a0']:.3f} → {res[w][key]['auc']:.3f} ({d:+.3f}) · 계수 {res[w][key]['coef']:+.2f}"
                                       for w, d in zip(res, da)))
        print(f"     ① 상위 20% ≥ 1.5배(두 해) {'✅' if okA[key][0] else '❌'} · ② AUC +0.010(두 창) {'✅' if okA[key][1] else '❌'}")
    w26 = res["학습 2025 → 시험 2026"]
    te, fitm = w26["te"], w26["fit"]
    base = top5(w26["s0"], coin, t, r, y, te)
    bv = np.array([x[0] for x in base.values()])
    print(f"\n판정 B — 2026 매일 상위 5 · 08:59→10:00 · 비용 0.25% · 기준 UP9 로지스틱 {bv.mean()*100:+.3f}%/일 (t {tstat(bv):+.2f})")
    q80 = np.nanquantile(nf[fitm], 0.8)
    q20 = np.nanquantile(rr[fitm], 0.2)
    cond = {"obv": np.nan_to_num((nf >= q80) & (ret30 <= 0)).astype(bool), "abs": np.nan_to_num((vr >= 1) & (rr <= q20)).astype(bool)}
    for key, lab in (("obv", "1 OBV 다이버전스"), ("abs", "2 흡수형 횡보")):
        for vnm, s in (("(i) 점수 더한 모델", top5(w26[key]["s1"], coin, t, r, y, te)),
                       ("(ii) 기준 상위 5 중 조건 통과만", top5(w26["s0"], coin, t, r, y, te, allow=cond[key]))):
            v, ok, diff, (hit, n) = judge(s, base)
            print(f"  {lab} {vnm}: {v.mean()*100:+.3f}%/일 · t {tstat(v):+.2f} · 기준 대비 {diff.mean()*100:+.3f}%p (t {tstat(diff):+.2f}) · "
                  f"적중 {hit:.1%}({n}건) · [{''.join('○' if o else '×' for o in ok)}] → {'✅' if all(ok) else '❌'}")
    print("\n━━ 참고 ━━")
    for key, lab in (("obv", "OBVD(OBV 상위 20% & 30일 ≤0)"), ("abs", "ABS(거래 ≥ 이전 & 변동폭 하위 20%)")):
        m = cond[key]
        print(f"  {lab}: 해당 {m.mean():.1%} · 펌핑률 {y[m].mean():.2%} vs 나머지 {y[~m].mean():.2%} ({y[m].mean()/y[~m].mean():.1f}배) · "
              f"그날 08:59→10:00 평균 {np.nanmean(r[m]-COST)*100:+.2f}% vs 나머지 {np.nanmean(r[~m]-COST)*100:+.2f}%")
    o, a_ = res["학습 2025 → 시험 2026"]["obv"]["score"], res["학습 2025 → 시험 2026"]["abs"]["score"]
    k = np.isfinite(o) & np.isfinite(a_)
    nf_ok = np.isfinite(nf)
    net90 = X13[:, up.FEAT.index("net90")]
    k2 = nf_ok & np.isfinite(net90)
    print(f"  상관: OBV점수·흡수점수 {np.corrcoef(o[k], a_[k])[0,1]:+.2f} · nf30·UP9 순매수 압력(net90) {np.corrcoef(nf[k2], net90[k2])[0,1]:+.2f}")


if __name__ == "__main__":
    main()
