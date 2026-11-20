"""UP9R — 업비트 9시: '펌핑할 확률'이 아니라 '08:59 에 사서 10:00 에 팔면 버는 코인'을 직접 고른다 (2026-11-20).

UP9 진단: 모델 상위 5 는 적중 7.9% 지만 안 터진 날 −1.05% 라 손익분기 적중이 9.6% 로 올라간다(펌핑 체질 = 고변동 코인).
→ 분류(펌핑 여부) 대신 **수익 자체를 회귀/순위**로 맞혀 두 새는 곳(적중 부족 · 안 터진 날 손실)을 한 번에 다룬다.

━━ 사전 고정 (BACKLOG 'UP9R' 절, 2026-11-20 작성 · 결과 보기 전 — 그대로 옮김) ━━
단위·기간  UP9 와 같음(코인·날짜, 08:59 KST 까지 정보만, 학습 2025-01~12 · 시험 2026-01 ~ 2026-11-18).
목표 변수  r = 09시 봉 종가 / 08시 봉 종가 − 1 (08:59 → 10:00 수익, 비용 전). 분류가 아니라 회귀/순위.
특징      UP9 13개 + 새 5개(전부 08:59 까지):
          F14 last_pump_days = 마지막 펌핑(어느 시각이든, UP9 정의)부터 일수(없으면 180)
          F15 off_peak = 현재가 / 직전 30일 최고가 − 1
          F16 h8_vol = 08시 봉 거래대금 / 직전 168시간 중앙
          F17 h8_ret = 08시 봉 종가 / 시가 − 1
          F18 btc_8h = 업비트 BTC 직전 8시간 수익률(시장 분위기)
모델 2개  (a) 표준화 릿지 회귀(L2, λ=1)
          (b) 그래디언트 부스팅 sklearn HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=300,
              l2_regularization=1.0, 기본 손실). 튜닝 금지. 둘 중 하나를 고르지 않는다 — 판정은 각각.
매매      시험 기간 매일 예측 상위 N(N=5)을 08:59 매수 → 10:00 매도 · 비용 왕복 0.25% ·
          비교 = 같은 날 UP9 로지스틱 상위 5 · 같은 날 전체 코인 평균.
합격(모델별) ① 날짜 평균 t ≥ 2.3(모델 2개)  ② 2026 전반(1~5월)·후반(6~11월) 모두 > 0  ③ 최다 코인·최다 날짜 빼도 > 0
          ④ 상위 5% 날 빼도 > 0  ⑤ UP9 로지스틱 상위 5보다 높다(날짜별 차이 t ≥ 2).
참고(판정 아님) 적중률(펌핑) · 안 터진 날 평균 · 특징 중요도(부스팅 permutation) · N 3/10 · 비용 0.10%.
통과해도  같은 2026 을 이미 여러 번 봤으므로 바로 매매하지 않는다 → 서버 전진 기록(주문 없음) 8주 이상.
한계      현재 상장 코인만 · 08:59 는 08시 봉 종가로 근사(호가 차 미반영) · 호가창·공지·입출금 정보 없음.
구현 메모(결과 보기 전 · 규칙 아님) 결측은 학습 기간 중앙으로 채움(UP9 와 같음) · 변환은 UP9 와 같고 새 특징은 h8_vol 만 log1p ·
          부스팅 random_state=0(재현용, 기본 early_stopping 그대로) · '최다 날짜 빼기' = 가장 좋은 날 1개 제외(UP9X 와 같음).

    .venv/bin/python -u -m research.exp_UP9R_return
"""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from research import exp_UP9_upbit_pump as up

H, D = up.H, up.D
TEST1 = int(datetime(2026, 11, 19, tzinfo=timezone.utc).timestamp() * 1000)     # 시험 끝(11-18 포함)
NEW = ["last_pump_days", "off_peak", "h8_vol", "h8_ret", "btc_8h"]
COST = 0.0025
N = 5


def series(c, coin, t_lo, nh):
    a = np.array(c.execute("SELECT t, o, h, l, c, v FROM up9_1h WHERE coin=? ORDER BY t", (coin,)).fetchall())
    O, Hh, C, V = (np.full(nh, np.nan) for _ in range(4))
    if not len(a):
        return O, Hh, C, V
    ix = ((a[:, 0].astype(np.int64) - t_lo) // H).astype(np.int64)
    ok = (ix >= 0) & (ix < nh)
    O[ix[ok]], Hh[ix[ok]], C[ix[ok]], V[ix[ok]] = a[ok, 1], a[ok, 2], a[ok, 4], a[ok, 5]
    return O, Hh, C, V


def extra(keys):
    """(coin, t) → 새 특징 5개. t = 09:00 KST 봉 시작(UTC 00:00). j = 08시 봉."""
    c = up.conn()
    t_lo = up.START // H * H
    nh = (int(time.time() * 1000) // H * H - t_lo) // H + 1
    _, _, BC, _ = series(c, "BTC", t_lo, nh)
    by = defaultdict(list)
    for cn, t in keys:
        by[cn].append(t)
    out = {}
    for cn, ts in by.items():
        O, Hh, C, V = series(c, cn, t_lo, nh)
        med = np.full(nh, np.nan)
        for i in range(168, nh):
            w = V[i - 168:i]
            if np.sum(~np.isnan(w)) > 100:
                med[i] = np.nanmedian(w)
        pump = np.where(np.isnan(med) | np.isnan(O), False, (Hh / O - 1 >= 0.10) & (V >= 5 * med))
        last = -1
        lastp = np.full(nh, -1)
        for i in range(nh):
            if pump[i]:
                last = i
            lastp[i] = last
        for t in ts:
            i = (t - t_lo) // H
            j = i - 1
            lp = (j - lastp[j]) / 24 if lastp[j] >= 0 and j - lastp[j] <= 180 * 24 else 180.0
            pk = np.nanmax(C[j - 30 * 24 + 1:j + 1])
            out[(cn, t)] = [lp, C[j] / pk - 1 if pk > 0 else np.nan,
                            V[j] / med[j] if med[j] > 0 else np.nan,
                            C[j] / O[j] - 1 if O[j] > 0 else np.nan,
                            BC[j] / BC[j - 8] - 1 if BC[j - 8] > 0 else np.nan]
    c.close()
    return out


def prep(X, names, tr):
    X = np.where(np.isfinite(X), X, np.nan).copy()
    for k, nm in enumerate(names):
        if nm in ("vtrend", "dryup", "squeeze", "pos", "updn30", "h8_vol"):
            X[:, k] = np.log1p(np.clip(X[:, k], 0, None))
        if nm in ("p9_90", "pX_90"):
            X[:, k] = np.log1p(X[:, k])
    med = np.nanmedian(X[tr], axis=0)
    X = np.where(np.isnan(X), med, X)
    mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
    return (X - mu) / sd


def ridge(Z, y, lam=1.0):
    Zb = np.hstack([Z, np.ones((len(Z), 1))])
    R = lam * np.eye(Zb.shape[1]); R[-1, -1] = 0
    return np.linalg.solve(Zb.T @ Zb + R, Zb.T @ y)


def tstat(v):
    v = np.asarray(v, dtype=float)
    return v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 2 and v.std() > 0 else float("nan")


def daily(score, coin, t, r, y, te, n=N, cost=COST):
    """날짜 → (상위 n 순수익 평균, [(코인, 순수익, 펌핑)])."""
    out = {}
    for d in sorted(set(t[te])):
        idx = np.where(te & (t == d) & np.isfinite(r))[0]
        if len(idx) < 20:
            continue
        tp = idx[np.argsort(-score[idx])[:n]]
        out[int(d)] = (float(np.mean(r[tp] - cost)), [(coin[k], float(r[k] - cost), bool(y[k])) for k in tp])
    return out


def judge(top, base):
    days = sorted(top)
    v = np.array([top[d][0] for d in days])
    h1 = [top[d][0] for d in days if d < up.MID]
    h2 = [top[d][0] for d in days if d >= up.MID]
    byc = defaultdict(int)
    for d in days:
        for cn, _, _ in top[d][1]:
            byc[cn] += 1
    topc = max(byc, key=byc.get)
    wo_c = [np.mean([x for cn, x, _ in top[d][1] if cn != topc]) for d in days if any(cn != topc for cn, _, _ in top[d][1])]
    wo_d = np.delete(v, int(np.argmax(v)))
    cut = np.sort(v)[: int(len(v) * 0.95)]
    both = [d for d in days if d in base]
    diff = np.array([top[d][0] - base[d][0] for d in both])
    ok = [tstat(v) >= 2.3, np.mean(h1) > 0 and np.mean(h2) > 0, np.mean(wo_c) > 0 and wo_d.mean() > 0, cut.mean() > 0, tstat(diff) >= 2]
    items = [x for d in days for x in top[d][1]]
    hit = np.mean([p for _, _, p in items])
    miss = np.mean([x for _, x, p in items if not p])
    return dict(days=len(v), mean=v.mean(), med=float(np.median(v)), t=tstat(v), h1=np.mean(h1), h2=np.mean(h2),
                topc=topc, nc=byc[topc], wo_c=np.mean(wo_c), wo_d=wo_d.mean(), cut5=cut.mean(), diff=diff.mean(), diff_t=tstat(diff),
                hit=hit, miss=miss, ok=ok)


def show(nm, j):
    print(f"■ {nm}: {j['days']}일 · 평균 {j['mean']*100:+.3f}%/일 · 중앙 {j['med']*100:+.3f} · t {j['t']:+.2f} · 전/후 {j['h1']*100:+.3f}/{j['h2']*100:+.3f}")
    print(f"   최다 코인 {j['topc']}({j['nc']}회) 빼면 {j['wo_c']*100:+.3f} · 최고 날 빼면 {j['wo_d']*100:+.3f} · 상위 5% 날 빼면 {j['cut5']*100:+.3f}")
    print(f"   UP9 로지스틱 상위5 대비 {j['diff']*100:+.3f}%p (t {j['diff_t']:+.2f}) · 적중(펌핑) {j['hit']:.1%} · 안 터진 날 평균 {j['miss']*100:+.2f}%")
    lab = ["① t≥2.3", "② 전·후반", "③ 코인·날 제외", "④ 상위5% 날 제외", "⑤ UP9 대비 t≥2"]
    print("   " + " · ".join(f"{a} {'✅' if o else '❌'}" for a, o in zip(lab, j["ok"])) + f" → {'✅ 합격' if all(j['ok']) else '❌ 불합격'}")


def main():
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.inspection import permutation_importance
    rows = up.build()
    coin = np.array([r[0] for r in rows]); t = np.array([r[1] for r in rows]); y = np.array([r[2] for r in rows])
    r = np.array([r[3] for r in rows]) + 0.0010                # UP9 ret 은 비용 0.10% 를 뺀 값 → 비용 전으로
    X13 = np.array([r_[4] for r_ in rows], dtype=float)
    ex = extra(list(zip(coin, t)))
    X5 = np.array([ex[(cn, tt)] for cn, tt in zip(coin, t)], dtype=float)
    keep = t < TEST1
    coin, t, y, r, X13, X5 = coin[keep], t[keep], y[keep], r[keep], X13[keep], X5[keep]
    tr, te = t < up.TEST0, t >= up.TEST0
    print(f"코인·날짜 {len(t):,} · 학습 {tr.sum():,} · 시험 {te.sum():,}({len(set(t[te]))}일) · 시험 r 평균 {np.nanmean(r[te])*100:+.3f}%")
    # UP9 로지스틱(비교 기준) — UP9 와 같은 절차
    Z13 = prep(X13, up.FEAT, tr)
    w = up.logit(Z13[tr], y[tr].astype(float))
    s_log = np.hstack([Z13, np.ones((len(Z13), 1))]) @ w
    print(f"UP9 로지스틱 시험 AUC {up.auc(y[te], s_log[te]):.3f}")
    names = up.FEAT + NEW
    Z = prep(np.hstack([X13, X5]), names, tr)
    fit = tr & np.isfinite(r)
    wr = ridge(Z[fit], r[fit])
    s_ridge = np.hstack([Z, np.ones((len(Z), 1))]) @ wr
    hgb = HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=300, l2_regularization=1.0, random_state=0)
    hgb.fit(Z[fit], r[fit])
    s_hgb = hgb.predict(Z)
    ok_te = te & np.isfinite(r)
    for nm, s in (("릿지", s_ridge), ("부스팅", s_hgb)):
        rc = np.corrcoef(np.argsort(np.argsort(s[ok_te])), np.argsort(np.argsort(r[ok_te])))[0, 1]
        print(f"  {nm} 시험 순위상관(예측·실제 r) {rc:+.3f} · 펌핑 AUC {up.auc(y[ok_te], s[ok_te]):.3f}")
    base = daily(s_log, coin, t, r, y, te)
    allc = {}
    for d in sorted(set(t[te])):
        idx = np.where(te & (t == d) & np.isfinite(r))[0]
        if len(idx) >= 20:
            allc[int(d)] = float(np.mean(r[idx] - COST))
    bl = np.array([base[d][0] for d in sorted(base)])
    print(f"\n비교 기준: UP9 로지스틱 상위5 {bl.mean()*100:+.3f}%/일 (t {tstat(bl):+.2f}) · 전체 코인 {np.mean(list(allc.values()))*100:+.3f}%/일")
    res = {}
    for nm, s in (("(a) 릿지", s_ridge), ("(b) 부스팅", s_hgb)):
        top = daily(s, coin, t, r, y, te)
        res[nm] = judge(top, base)
        print()
        show(nm, res[nm])
    print("\n릿지 계수(표준화, 큰 순): " + " · ".join(f"{n_} {wr[k]*100:+.3f}" for k, n_ in sorted(enumerate(names), key=lambda x: -abs(wr[x[0]]))[:10]))
    print("\n━━ 참고(판정 아님) ━━")
    for nm, s in (("릿지", s_ridge), ("부스팅", s_hgb)):
        for n in (3, 10):
            v = np.array([x[0] for x in daily(s, coin, t, r, y, te, n=n).values()])
            print(f"  {nm} N{n}: {v.mean()*100:+.3f}%/일 (t {tstat(v):+.2f})")
        v = np.array([x[0] for x in daily(s, coin, t, r, y, te, cost=0.0010).values()])
        print(f"  {nm} N5 비용 0.10%: {v.mean()*100:+.3f}%/일 (t {tstat(v):+.2f})")
    sub = np.where(ok_te)[0]
    pi = permutation_importance(hgb, Z[sub], r[sub], n_repeats=5, random_state=0)
    print("  부스팅 permutation 중요도(시험, 상위 8): " + " · ".join(
        f"{names[k]} {pi.importances_mean[k]*1e4:+.2f}" for k in np.argsort(-pi.importances_mean)[:8]) + " (×1e−4 R²)")


if __name__ == "__main__":
    main()
