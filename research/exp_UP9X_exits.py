"""UP9X — 업비트 9시 펌핑 후보를 08:59 에 미리 사고, 9시 이후 움직임으로 손익절 (사용자 설계, 2026-11-20).

UP9: 후보 상위 5개를 08:59 매수 → 10:00 매도 −0.04%/일 · 고정 익절(+3/5/10%)도 차이 없음. **손절·조기 탈출·추격 익절은 안 봤다.**
가설: 안 터지는 날의 하락(UP9 를 망친 몫)을 손절·조기 탈출로 자르고, 터지는 날은 추격 익절로 길게 타면 플러스가 된다.

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
후보    UP9 모델 그대로(특징 13개 · 표준화 로지스틱 · 학습 2025 · 시험 2026-01 ~ 2026-11-18). 매일 시험 점수 상위 N.
진입    08:59 KST 1분봉 종가(= 23:59 UTC).  업비트 KRW 현물 · 롱만.
경로    09:00 ~ 10:00 KST 1분봉(업비트 공개 API). 분봉 안 순서는 모른다 →
        손절과 익절이 같은 분에 닿으면 **손절 먼저**(보수적) · 추격 익절은 그 분 고가로 고점을 갱신하기 **전에** 저가를 확인.
        손절가·익절가에 체결(시가가 이미 넘었으면 시가).
규칙    손절 SL ∈ {−2%, −3%, −5%, 없음} · 익절 TP ∈ {+3%, +5%, +10%, 추격(고점 대비 −3%)} · 시간 T ∈ {09:05, 09:15, 10:00} ·
        조기 탈출 E ∈ {없음, '09:03 종가까지 고가가 +1% 를 못 넘었으면 09:03 종가 매도'} · N ∈ {3, 5, 10}  → 288 조합.
비용    왕복 0.25%(수수료 0.05%×2 + 호가 차·급등 중 슬리피지 0.15%).
비교    같은 날 **무작위 10개 코인**(시험 대상 중, 고정 시드)에 같은 규칙 — '후보 선택'이 의미 있는지.
판정    단위 = 날짜(그날 N개 평균). 288개 중 날짜 t 최고 조합이
        ① t ≥ 3.5(288개 동시 비교)  ② 2026 전반(1~5월)·후반(6~11월) 모두 > 0
        ③ 최다 코인 빼도 · 최다 날짜 빼도 > 0  ④ 상위 5% 날짜 빼도 > 0
        ⑤ 같은 규칙의 무작위 10개보다 높다(날짜별 차이 t ≥ 2).
참고(판정 아님): 비용 0.10%(수수료만) · 규칙별 평균 · UP9 기준선(08:59 → 10:00, N=5) 재현.
주의    현재 상장 코인만(생존 편향) · 1분봉이라 분 안 경로를 모른다(보수 가정) · 같은 2026 기간을 UP9 에서 이미 봤다 → 통과해도 전진 기록 필요.

    .venv/bin/python -u research/exp_UP9X_exits.py --collect    # UP9 1시간봉(research.exp_UP9_upbit_pump.collect 그대로)
    .venv/bin/python -u research/exp_UP9X_exits.py --min        # 후보·무작위 코인의 08:59~10:00 1분봉
    .venv/bin/python -u research/exp_UP9X_exits.py              # 판정
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from itertools import product

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import exp_UP9_upbit_pump as up  # noqa: E402

M = 60_000
NS = (3, 5, 10)
SLS = (0.02, 0.03, 0.05, None)
TPS = (0.03, 0.05, 0.10, "trail")
TRAIL = 0.03
TS = (5, 15, 60)                      # 09:00 부터 몇 분째 종가에 시간 청산(09:05 · 09:15 · 10:00)
ES = (False, True)
COST = 0.0025
NRAND, SEED = 10, 20261120
T_MIN = 3.5


def mconn():
    c = sqlite3.connect(up.UDB, timeout=60)
    c.execute("CREATE TABLE IF NOT EXISTS up9x_1m (coin TEXT, d INTEGER, k INTEGER, o REAL, h REAL, l REAL, c REAL, PRIMARY KEY (coin, d, k))")
    c.execute("CREATE TABLE IF NOT EXISTS up9x_done (coin TEXT, d INTEGER, n INTEGER, PRIMARY KEY (coin, d))")
    return c


def scores():
    """UP9 main 과 같은 절차로 시험 기간 점수. 반환: coin, t, y, s(시험 점수), te 마스크, 시험 AUC."""
    rows = up.build()
    coin = np.array([r[0] for r in rows]); t = np.array([r[1] for r in rows]); y = np.array([r[2] for r in rows])
    X = np.array([r[4] for r in rows], dtype=float)
    X = np.where(np.isfinite(X), X, np.nan)
    Xf = np.where(np.isnan(X), np.nanmedian(X, axis=0), X)
    for k, nm in enumerate(up.FEAT):
        if nm in ("vtrend", "dryup", "squeeze", "pos", "updn30"):
            Xf[:, k] = np.log1p(np.clip(Xf[:, k], 0, None))
        if nm in ("p9_90", "pX_90"):
            Xf[:, k] = np.log1p(Xf[:, k])
    tr, te = t < up.TEST0, t >= up.TEST0
    mu, sd = Xf[tr].mean(0), Xf[tr].std(0) + 1e-9
    Z = (Xf - mu) / sd
    w = up.logit(Z[tr], y[tr].astype(float))
    s = np.hstack([Z, np.ones((len(Z), 1))]) @ w
    return coin, t, y, s, te, up.auc(y[te], s[te])


def picks():
    """날짜 → (후보 상위 10, 무작위 10). 무작위는 그날 시험 대상 중 고정 시드."""
    coin, t, y, s, te, a = scores()
    rng = np.random.default_rng(SEED)
    out = {}
    for d in sorted(set(t[te])):
        idx = np.where(te & (t == d))[0]
        if len(idx) < 20:
            continue
        top = list(coin[idx[np.argsort(-s[idx])[:max(NS)]]])
        rnd = list(coin[rng.choice(idx, NRAND, replace=False)])
        out[int(d)] = (top, rnd, {c: bool(v) for c, v in zip(coin[idx], y[idx])})
    return out, a


def collect_min():
    pk, a = picks()
    print(f"시험 AUC {a:.3f} (UP9 0.776 재현 확인) · 날짜 {len(pk)}", flush=True)
    c = mconn()
    done = {(r[0], r[1]) for r in c.execute("SELECT coin, d FROM up9x_done")}
    jobs = sorted({(cn, d) for d, (top, rnd, _) in pk.items() for cn in top + rnd} - done)
    print(f"1분봉 받을 것 {len(jobs)}", flush=True)
    for n, (cn, d) in enumerate(jobs, 1):
        to = datetime.fromtimestamp((d + 61 * M) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # 01:01 UTC 전 62개
        rows = up._get(f"{up.UP}/candles/minutes/1?market=KRW-{cn}&to={to}&count=62")
        time.sleep(0.11)
        vals = []
        for r in rows or []:
            tt = int(datetime.strptime(r["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
            k = (tt - d) // M                               # −1 = 08:59 KST, 0 = 09:00 …
            if -1 <= k <= 59:
                vals.append((cn, d, int(k), r["opening_price"], r["high_price"], r["low_price"], r["trade_price"]))
        c.executemany("INSERT OR REPLACE INTO up9x_1m VALUES (?,?,?,?,?,?,?)", vals)
        c.execute("INSERT OR REPLACE INTO up9x_done VALUES (?,?,?)", (cn, d, len(vals)))
        if n % 200 == 0:
            c.commit()
            print(f"  {n}/{len(jobs)}", flush=True)
    c.commit()
    c.close()


def load_paths():
    c = mconn()
    by = defaultdict(dict)
    for cn, d, k, o, h, l, cl in c.execute("SELECT coin, d, k, o, h, l, c FROM up9x_1m"):
        by[(cn, d)][k] = (o, h, l, cl)
    c.close()
    out = {}
    for key, m in by.items():
        if -1 not in m:
            continue
        rows, last = [], m[-1][3]
        for k in range(60):                                 # 거래 없는 분은 직전 종가로 채움(업비트는 체결 없는 분을 안 준다)
            if k in m:
                rows.append(m[k])
                last = m[k][3]
            else:
                rows.append((last, last, last, last))
        out[key] = (m[-1][3], np.array(rows))
    return out


def sim(entry, P, sl, tp, T, early):
    """한 코인 하루 수익률(비용 전). P = 60×(o,h,l,c), 09:00 부터."""
    stop = entry * (1 - sl) if sl else None
    peak = entry
    for k in range(T):
        o, h, l, c = P[k]
        if stop is not None and l <= stop:
            return min(o, stop) / entry - 1
        if tp == "trail":
            tstop = peak * (1 - TRAIL)
            if peak > entry * (1 + TRAIL) and l <= tstop:     # 고점이 +3% 위로 간 뒤에만 추격(바로 털리지 않게)
                return min(o, tstop) / entry - 1
            peak = max(peak, h)
        elif h >= entry * (1 + tp):
            return max(o, entry * (1 + tp)) / entry - 1
        if early and k == 3 and max(P[:4, 1]) < entry * 1.01:
            return c / entry - 1
    return P[T - 1, 3] / entry - 1


def day_series(pk, paths, n, rule, which):
    out = {}
    for d, (top, rnd, _) in pk.items():
        cs = top[:n] if which == "top" else rnd
        rs = [sim(*paths[(cn, d)], *rule) for cn in cs if (cn, d) in paths]
        if len(rs) >= max(1, len(cs) // 2):
            out[d] = (float(np.mean(rs)), [(cn, r) for cn, r in zip([x for x in cs if (x, d) in paths], rs)])
    return out


def tstat(v):
    v = np.asarray(v)
    return v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 2 and v.std() > 0 else float("nan")


def judge(top, rnd, cost):
    days = sorted(top)
    v = np.array([top[d][0] - cost for d in days])
    hi = {"전반": [top[d][0] - cost for d in days if d < up.MID], "후반": [top[d][0] - cost for d in days if d >= up.MID]}
    byc = defaultdict(float)
    for d in days:
        for cn, r in top[d][1]:
            byc[cn] += r - cost
    topc = max(byc, key=byc.get)
    wo_c = [np.mean([r - cost for cn, r in top[d][1] if cn != topc]) for d in days if any(cn != topc for cn, _ in top[d][1])]
    topd = days[int(np.argmax(v))]
    wo_d = [x for d, x in zip(days, v) if d != topd]
    cut = np.sort(v)[: int(len(v) * 0.95)]
    both = [d for d in days if d in rnd]
    diff = np.array([top[d][0] - rnd[d][0] for d in both])
    ok = [tstat(v) >= T_MIN, all(len(x) and np.mean(x) > 0 for x in hi.values()),
          np.mean(wo_c) > 0 and np.mean(wo_d) > 0, cut.mean() > 0, tstat(diff) >= 2]
    return dict(days=len(v), mean=v.mean(), med=float(np.median(v)), win=float(np.mean(v > 0)), t=tstat(v),
                h1=np.mean(hi["전반"]), h2=np.mean(hi["후반"]), wo_c=np.mean(wo_c), wo_d=np.mean(wo_d), cut5=cut.mean(),
                rnd=float(np.mean([rnd[d][0] - cost for d in both])), diff_t=tstat(diff), worst=v.min(), topc=topc, ok=ok)


def lab(n, rule):
    sl, tp, T, e = rule
    return (f"N{n} · 손절 {'-%d%%' % round(sl*100) if sl else '없음'} · 익절 {'추격' if tp == 'trail' else '+%d%%' % round(tp*100)} · "
            f"~{['09:05', '09:15', '10:00'][TS.index(T)]}{' · 조기탈출' if e else ''}")


def fmt(j):
    return (f"{j['days']}일 · 평균 {j['mean']*100:+.3f}%/일 · 중앙 {j['med']*100:+.3f} · 이익 {j['win']:.0%} · t {j['t']:+.2f} · "
            f"전/후 {j['h1']*100:+.2f}/{j['h2']*100:+.2f} · 무작위 {j['rnd']*100:+.3f}(차 t {j['diff_t']:+.2f}) · "
            f"상위5%뺌 {j['cut5']*100:+.3f} · 최악 {j['worst']*100:+.1f}% [{''.join('○' if x else '×' for x in j['ok'])}]")


def main():
    pk, a = picks()
    paths = load_paths()
    print(f"시험 AUC {a:.3f} · 날짜 {len(pk)} · 1분봉 경로 {len(paths)}개 · 비용 {COST*100:.2f}%\n")
    base = day_series(pk, paths, 5, (None, 1e9, 60, False), "top")
    bv = np.array([x[0] - 0.0010 for x in base.values()])
    print(f"UP9 기준선 재현(N5 · 08:59 → 10:00 · 비용 0.10%): {bv.mean()*100:+.3f}%/일 · t {tstat(bv):+.2f} (UP9 −0.044 · t +0.31 근처면 재현)\n")
    res = []
    for n, sl, tp, T, e in product(NS, SLS, TPS, TS, ES):
        rule = (sl, tp, T, e)
        top = day_series(pk, paths, n, rule, "top")
        rnd = day_series(pk, paths, n, rule, "rnd")
        if len(top) > 30:
            res.append((n, rule, judge(top, rnd, COST), top, rnd))
    res.sort(key=lambda x: -(x[2]["t"] if x[2]["t"] == x[2]["t"] else -99))
    print(f"■ {len(res)}개 조합 — 날짜 t 상위 12")
    for n, rule, j, _, _ in res[:12]:
        print(f"  {lab(n, rule):44} {fmt(j)}")
    pos = sum(j["mean"] > 0 for _, _, j, _, _ in res)
    print(f"\n  평균 > 0 인 조합 {pos}/{len(res)} · 무작위보다 높은 조합 {sum(j['mean'] > j['rnd'] for _, _, j, _, _ in res)}/{len(res)}")
    n, rule, j, top, rnd = res[0]
    oks = ["① t≥%.1f" % T_MIN, "② 전·후반", "③ 코인·날짜 제외", "④ 상위5% 뺌", "⑤ 무작위보다(차 t≥2)"]
    print(f"\n■ 판정 — 최고 조합 [{lab(n, rule)}]: {'✅ 합격' if all(j['ok']) else '❌'}  "
          + " · ".join(f"{o} {'○' if k else '×'}" for o, k in zip(oks, j["ok"])))
    print(f"   최다 코인({j['topc']}) 빼도 {j['wo_c']*100:+.3f} · 최다 날짜 빼도 {j['wo_d']*100:+.3f}")
    print("\n■ 참고(판정 아님)")
    print(f"  최고 조합 · 비용 0.10%: {fmt(judge(top, rnd, 0.0010))}")
    for key, name in ((0, "N"), (1, "손절"), (2, "익절"), (3, "시간"), (4, "조기탈출")):
        g = defaultdict(list)
        for n_, r_, j_, _, _ in res:
            k_ = (n_,) + r_
            g[k_[key]].append(j_["mean"])
        print(f"  {name}별 평균(조합 평균, %/일): " + " · ".join(f"{k}: {np.mean(v)*100:+.3f}" for k, v in g.items()))


if __name__ == "__main__":
    if "--collect" in sys.argv:
        up.collect()
    elif "--min" in sys.argv:
        collect_min()
    else:
        main()
