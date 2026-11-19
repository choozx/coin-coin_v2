"""UP9 — 업비트 아침 9시 펌핑 코인의 사전 특징 (사용자 질문, 2026-11-19).

질문: "업비트에서 아침 9시마다 펌핑하는 코인이 있다. 그런 코인들의 특징은? 예: 몇 달 전부터 세력의 매집 모습."
09:00 KST = 업비트 일봉 리셋(그날 상승률 순위가 새로 시작). 매집은 지갑·주문 데이터 없이는 직접 못 본다 →
1시간봉의 거래대금·가격 흔적으로 간접 측정한다(아래 F9·F10·F11).

━━ 사전 고정 (2026-11-19 작성 · 결과 보기 전) ━━
데이터  업비트 KRW 마켓 현재 상장분(300개) 1시간봉 2024-07-01 ~ 2026-11-18 (폐지 코인 없음 = 생존 편향, 아래 주의).
펌핑    그날 09:00 KST(00:00 UTC) 1시간봉: 고가/시가 − 1 ≥ +10%  그리고  그 1시간 거래대금 ≥ 5 × 직전 168시간 1시간 거래대금 중앙.
단위    코인·날짜(매일 9시 1회). 대상 = 직전 180일 1시간봉이 90% 이상 있는 코인·날짜. 분석 날짜 2025-01-01 ~ 2026-11-18.
특징    전부 그날 08:59 KST 까지의 데이터만(미래 정보 없음).
  F1 p9_90    직전 90일 9시 펌핑 횟수            F2 pX_90   직전 90일 다른 시각 펌핑 횟수(같은 정의, 9시 제외)
  F3 liq      30일 일 거래대금 중앙(log10 원)     F4 vtrend  30일 평균 / 180일 평균 일 거래대금
  F5 dryup    7일 평균 / 30일 평균 일 거래대금     F6 squeeze 30일 고저폭 / 180일 고저폭
  F7 pos      현재가 / 180일 최저가 − 1          F8 ret30   30일 수익률
  F9 updn30   30일 양봉 시간 거래대금 / 음봉 시간 거래대금(매집 흔적 1)
  F10 net90   90일 Σ(부호(종가−시가) × 거래대금) / Σ거래대금(순매수 압력, 매집 흔적 2)
  F11 nine30  30일 거래대금 중 9시 1시간 비중(9시 집중도)
  F12 bnc     바이낸스 USDT 무기한 상장 여부(지금 기준)   F13 age  업비트 첫 봉부터 일수
분석
  (a) 특징별 5분위 펌핑률 · 상위/하위 분위 배수(기준 = 전체 펌핑률).
  (b) 예측: 표준화 로지스틱(L2) — 학습 2025-01~2025-12 · 시험 2026-01~2026-11-18. 시험 AUC · 매일 상위 5개 중 펌핑 비율.
  (c) 매매 가능성(판정): 시험 기간 매일 모델 상위 5개를 08:59 종가(08시 봉 종가)에 사서 10:00(09시 봉 종가)에 판다.
      비용 왕복 0.10%(업비트 0.05%×2). 비교 = 같은 시간 전체 코인 동일 가중.
      합격 = ① 날짜별 평균 − 2se > 0  ② 전체 코인 평균보다 높음(날짜별 차이 t ≥ 2)  ③ 2026 전반·후반 모두 > 0  ④ 최다 코인 빼도 > 0.
주의  현재 상장 코인만 → 펌핑 후 상장폐지된 코인이 빠져 있다(그런 코인일수록 펌핑이 잦았을 수 있음).
      펌핑 정의는 고가 기준(못 먹는 값) · 매매는 종가 기준(먹을 수 있는 값)으로 일부러 나눴다.

    python3 -u -m research.exp_UP9_upbit_pump --collect
    python3 -u -m research.exp_UP9_upbit_pump
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import numpy as np

from research.xv_data import DB

START = int(datetime(2024, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
A0 = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
TEST0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
MID = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
H = 3_600_000
D = 86_400_000
UP = "https://api.upbit.com/v1"
FEAT = ["p9_90", "pX_90", "liq", "vtrend", "dryup", "squeeze", "pos", "ret30", "updn30", "net90", "nine30", "bnc", "age"]


UDB = DB.replace("xvenue.db", "upbit9.db")              # P9 수집과 쓰기 잠금이 겹치지 않게 따로


def conn():
    c = sqlite3.connect(UDB, timeout=60)
    c.execute("CREATE TABLE IF NOT EXISTS up9_1h (coin TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY (coin, t))")
    c.execute("CREATE TABLE IF NOT EXISTS up9_done (coin TEXT PRIMARY KEY, n INTEGER)")
    return c


def _get(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research/0.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1 + i)
                continue
            if e.code in (400, 404):
                return []
            time.sleep(2)
        except urllib.error.URLError:
            time.sleep(2)
    return []


def collect():
    c = conn()
    mk = [x["market"][4:] for x in _get(f"{UP}/market/all") if x["market"].startswith("KRW-")]
    done = {r[0] for r in c.execute("SELECT coin FROM up9_done")}
    todo = [m for m in mk if m not in done]
    print(f"KRW 마켓 {len(mk)} · 받을 것 {len(todo)}", flush=True)
    for n, coin in enumerate(todo, 1):
        to, tot = None, 0
        while True:
            q = f"{UP}/candles/minutes/60?market=KRW-{coin}&count=200" + (f"&to={to}" if to else "")
            rows = _get(q)
            time.sleep(0.11)
            if not rows:
                break
            vals = []
            for r in rows:
                t = int(datetime.strptime(r["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
                vals.append((coin, t, r["opening_price"], r["high_price"], r["low_price"], r["trade_price"], r["candle_acc_trade_price"]))
            c.executemany("INSERT OR REPLACE INTO up9_1h VALUES (?,?,?,?,?,?,?)", vals)
            tot += len(vals)
            oldest = min(v[1] for v in vals)
            if oldest <= START or len(rows) < 200:
                break
            to = datetime.fromtimestamp(oldest / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        c.execute("INSERT OR REPLACE INTO up9_done VALUES (?,?)", (coin, tot))
        c.commit()
        if n % 20 == 0:
            print(f"  {n}/{len(todo)} · {coin} {tot}봉", flush=True)
    c.close()


def build():
    """코인·날짜 표 — 특징(그날 09시 전까지) · 펌핑 여부 · 매매 손익(08시 봉 종가 → 09시 봉 종가)."""
    c = conn()
    coins = [r[0] for r in c.execute("SELECT coin FROM up9_done WHERE n > 0")]
    x = sqlite3.connect(DB, timeout=60)
    bnc = {r[0] for r in x.execute("SELECT base FROM xs_sym")}
    x.close()
    t_lo = START // H * H
    t_hi = int(time.time() * 1000) // H * H
    nh = (t_hi - t_lo) // H + 1
    rows = []
    for coin in coins:
        a = np.array(c.execute("SELECT t, o, h, l, c, v FROM up9_1h WHERE coin=? ORDER BY t", (coin,)).fetchall())
        if len(a) < 24 * 120:
            continue
        O, Hh, L, C, V = (np.full(nh, np.nan) for _ in range(5))
        ix = ((a[:, 0].astype(np.int64) - t_lo) // H).astype(np.int64)
        ok = (ix >= 0) & (ix < nh)
        for arr, col in ((O, 1), (Hh, 2), (L, 3), (C, 4), (V, 5)):
            arr[ix[ok]] = a[ok, col]
        first = int(a[0, 0])
        hrs = np.arange(nh)
        tt = t_lo + hrs * H
        hod = (tt // H) % 24                                # 0 = 09:00 KST
        # 시간별 펌핑(같은 정의) — 직전 168시간 거래대금 중앙
        med168 = np.full(nh, np.nan)
        for i in range(168, nh):
            w = V[i - 168:i]
            if np.sum(~np.isnan(w)) > 100:
                med168[i] = np.nanmedian(w)
        pump = (Hh / O - 1 >= 0.10) & (V >= 5 * med168)
        pump = np.where(np.isnan(med168) | np.isnan(O), False, pump)
        cumP9 = np.cumsum(pump & (hod == 0))
        cumPX = np.cumsum(pump & (hod != 0))
        up = np.nan_to_num(np.where(C > O, V, 0.0))
        dn = np.nan_to_num(np.where(C < O, V, 0.0))
        sgn = np.nan_to_num(np.sign(C - O) * V)
        Vn = np.nan_to_num(V)
        cV, cUp, cDn, cS = np.cumsum(Vn), np.cumsum(up), np.cumsum(dn), np.cumsum(sgn)
        cV9 = np.cumsum(np.where(hod == 0, Vn, 0.0))
        have = np.cumsum(~np.isnan(C))
        for i in np.where((hod == 0) & (tt >= A0))[0]:
            j = i - 1                                        # 08시 봉(08:00~08:59) — 그날 9시 전 마지막
            if j - 180 * 24 < 0 or have[j] - have[j - 180 * 24] < 0.9 * 180 * 24 or np.isnan(C[j]) or np.isnan(O[i]):
                continue

            def S(cs, h):                                    # 직전 h 시간 합(j 포함)
                return cs[j] - cs[j - h]
            dv = Vn[j - 180 * 24 + 1:j + 1].reshape(180, 24).sum(1)
            px = C[j - 180 * 24 + 1:j + 1]
            lo180, hi180 = np.nanmin(px), np.nanmax(px)
            px30 = px[-30 * 24:]
            c30 = C[j - 30 * 24] if not np.isnan(C[j - 30 * 24]) else np.nan
            f = dict(
                p9_90=cumP9[j] - cumP9[j - 90 * 24], pX_90=cumPX[j] - cumPX[j - 90 * 24],
                liq=np.log10(np.median(dv[-30:]) + 1), vtrend=dv[-30:].mean() / (dv.mean() + 1),
                dryup=dv[-7:].mean() / (dv[-30:].mean() + 1),
                squeeze=(np.nanmax(px30) - np.nanmin(px30)) / ((hi180 - lo180) or np.nan),
                pos=C[j] / lo180 - 1, ret30=C[j] / c30 - 1,
                updn30=S(cUp, 720) / (S(cDn, 720) + 1), net90=S(cS, 2160) / (S(cV, 2160) + 1),
                nine30=S(cV9, 720) / (S(cV, 720) + 1), bnc=float(coin in bnc), age=(tt[i] - first) / D)
            ret = C[i] / C[j] - 1 - 0.0010 if not np.isnan(C[i]) else np.nan
            hi = Hh[i] / C[j] - 1 if not np.isnan(Hh[i]) else np.nan
            rows.append((coin, int(tt[i]), bool(pump[i]), ret, [f[k] for k in FEAT], hi))
    c.close()
    return rows


def auc(y, s):
    o = np.argsort(s)
    r = np.empty(len(s)); r[o] = np.arange(1, len(s) + 1)
    n1 = y.sum(); n0 = len(y) - n1
    return (r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def logit(X, y, lam=1.0, it=300):
    w = np.zeros(X.shape[1] + 1)
    Xb = np.hstack([X, np.ones((len(X), 1))])
    for _ in range(it):                                       # 뉴턴법
        p = 1 / (1 + np.exp(-Xb @ w))
        g = Xb.T @ (p - y) + lam * np.r_[w[:-1], 0]
        Hs = (Xb * (p * (1 - p))[:, None]).T @ Xb + lam * np.diag(np.r_[np.ones(len(w) - 1), 0])
        w -= np.linalg.solve(Hs, g)
    return w


def main():
    if "--collect" in sys.argv:
        collect()
        return
    rows = build()
    coin = np.array([r[0] for r in rows]); t = np.array([r[1] for r in rows]); y = np.array([r[2] for r in rows])
    ret = np.array([r[3] for r in rows]); X = np.array([r[4] for r in rows], dtype=float)
    X = np.where(np.isfinite(X), X, np.nan)
    base = y.mean()
    print(f"코인·날짜 {len(y):,} · 코인 {len(set(coin))} · 날짜 {len(set(t))} · 9시 펌핑 {y.sum():,}건 (펌핑률 {base:.2%})")
    pc = {}
    for cn in coin[y]:
        pc[cn] = pc.get(cn, 0) + 1
    top = sorted(pc.items(), key=lambda x: -x[1])[:15]
    print("  펌핑 많은 코인: " + ", ".join(f"{k} {v}" for k, v in top))
    print(f"  펌핑 1회 이상 코인 {len(pc)}개 / {len(set(coin))}개")
    print("\n(a) 특징별 5분위 펌핑률 (기준 대비 배수) — 낮은 분위 → 높은 분위")
    for k, nm in enumerate(FEAT):
        x = X[:, k]
        good = np.isfinite(x)
        if nm in ("p9_90", "pX_90", "bnc"):
            cats = [("0", x == 0), ("1", x == 1), ("2+", x >= 2)] if nm != "bnc" else [("없음", x == 0), ("있음", x == 1)]
        else:
            qs = np.nanquantile(x, [0.2, 0.4, 0.6, 0.8])
            b = np.digitize(x, qs)
            cats = [(f"Q{q+1}", good & (b == q)) for q in range(5)]
        s = " · ".join(f"{lab} {y[mk].mean()/base:4.1f}배({mk.sum()})" if mk.sum() else f"{lab} -" for lab, mk in cats)
        print(f"  {nm:8} {s}")
    # (b) 예측
    Xf = np.where(np.isnan(X), np.nanmedian(X, axis=0), X)
    for k, nm in enumerate(FEAT):
        if nm in ("vtrend", "dryup", "squeeze", "pos", "updn30"):
            Xf[:, k] = np.log1p(np.clip(Xf[:, k], 0, None))
        if nm in ("p9_90", "pX_90"):
            Xf[:, k] = np.log1p(Xf[:, k])
    tr, te = t < TEST0, t >= TEST0
    mu, sd = Xf[tr].mean(0), Xf[tr].std(0) + 1e-9
    Z = (Xf - mu) / sd
    w = logit(Z[tr], y[tr].astype(float))
    s = np.hstack([Z, np.ones((len(Z), 1))]) @ w
    print(f"\n(b) 예측 — 학습 {tr.sum():,}(펌핑 {y[tr].sum()}) · 시험 {te.sum():,}(펌핑 {y[te].sum()})")
    print(f"  시험 AUC {auc(y[te], s[te]):.3f} · 학습 AUC {auc(y[tr], s[tr]):.3f}")
    print("  계수(표준화): " + " · ".join(f"{nm} {w[k]:+.2f}" for k, nm in sorted(enumerate(FEAT), key=lambda x: -abs(w[x[0]]))))
    for nm1 in ("p9_90",):
        k = FEAT.index(nm1)
        a1 = auc(y[te], Z[te, k])
        print(f"  참고: {nm1} 하나만 쓴 시험 AUC {a1:.3f}")
    # (c) 매매
    days = sorted(set(t[te]))
    pick, allr, hit, n9 = [], [], [], []
    for d in days:
        mk = te & (t == d) & np.isfinite(ret)
        if mk.sum() < 20:
            continue
        idx = np.where(mk)[0]
        tp = idx[np.argsort(-s[idx])[:5]]
        pick.append((d, ret[tp].mean(), list(coin[tp])))
        allr.append(ret[idx].mean())
        hit.append(y[tp].mean())
    pr = np.array([x[1] for x in pick]); ar = np.array(allr); dd = np.array([x[0] for x in pick])
    se = pr.std(ddof=1) / np.sqrt(len(pr))
    df = pr - ar
    tdf = df.mean() / (df.std(ddof=1) / np.sqrt(len(df)))
    h1, h2 = pr[dd < MID], pr[dd >= MID]
    cc = {}
    for _, r, cs in pick:
        for x in cs:
            cc[x] = cc.get(x, 0) + 1
    topc = max(cc, key=cc.get)
    ex = np.mean([r for (_, r, cs) in pick if topc not in cs]) if any(topc not in cs for _, _, cs in pick) else np.nan
    print(f"\n(c) 매매 — 시험 {len(pr)}일 · 매일 상위 5개 08:59 매수 → 10:00 매도 · 비용 0.10%")
    print(f"  상위 5개 평균 {pr.mean()*100:+.3f}%/일 (±{se*100:.3f}) · 중앙 {np.median(pr)*100:+.3f}% · 그중 펌핑 비율 {np.mean(hit):.1%}(기준 {base:.1%})")
    print(f"  전체 코인 같은 시간 {ar.mean()*100:+.3f}% · 차이 {df.mean()*100:+.3f}%p · t {tdf:+.2f}")
    print(f"  2026 전반 {h1.mean()*100:+.3f}% · 후반 {h2.mean()*100:+.3f}% · 최다 코인 {topc}({cc[topc]}일) 빼면 {ex*100:+.3f}%")
    c1, c2, c3, c4 = pr.mean() - 2 * se > 0, tdf >= 2, h1.mean() > 0 and h2.mean() > 0, ex > 0
    print(f"  ① {'✅' if c1 else '❌'} · ② {'✅' if c2 else '❌'} · ③ {'✅' if c3 else '❌'} · ④ {'✅' if c4 else '❌'} → "
          f"{'✅ 합격' if c1 and c2 and c3 and c4 else '❌ 불합격'}")
    # 참고(판정 아님, 결과 본 뒤): 미리 사 두고 +tp 지정가 매도(09시 봉 고가가 닿으면 tp 에 체결, 아니면 10:00 종가)
    hi = np.array([r[5] for r in rows])
    print("\n━━ 참고(판정 아님 · 사후): 상위 5개 08:59 매수 + 지정가 매도 +tp (안 닿으면 10:00 종가) ━━")
    for tp in (0.03, 0.05, 0.10):
        rr2, al2 = [], []
        for d in days:
            mk = te & (t == d) & np.isfinite(ret) & np.isfinite(hi)
            if mk.sum() < 20:
                continue
            idx = np.where(mk)[0]
            tpk = idx[np.argsort(-s[idx])[:5]]
            r_ = np.where(hi[tpk] >= tp, tp - 0.0010, ret[tpk])
            ra = np.where(hi[idx] >= tp, tp - 0.0010, ret[idx])
            rr2.append(r_.mean()); al2.append(ra.mean())
        rr2, al2 = np.array(rr2), np.array(al2)
        dd2 = rr2 - al2
        print(f"  +{tp*100:.0f}%: 상위 5개 {rr2.mean()*100:+.3f}%/일 (±{rr2.std(ddof=1)/np.sqrt(len(rr2))*100:.3f}) · "
              f"전체 코인 {al2.mean()*100:+.3f}% · 차이 t {dd2.mean()/(dd2.std(ddof=1)/np.sqrt(len(dd2))):+.2f}")


if __name__ == "__main__":
    main()
