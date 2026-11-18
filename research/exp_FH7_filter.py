"""FH7 — 정산 걸치기(받는 쪽 T−10초 → T+5초)를 '펀딩 > 하락'이 될 만한 정산만 골라서. 사전 고정 + 전진 검증.

배경(2026-11-18, 사후 탐색): |펀딩| ≥ 0.5% 정산(M 밖 598심볼) 6,000건 중 받는 쪽 걸치기는 평균 −0.05%(비용 후)지만,
진입 전에 알 수 있는 특징으로 점수를 매겨 상위 10%만 고르면 학습(~2026-04) 밖에서 26-05~08 +0.42% · 26-09~11 +0.16 ±0.04%
(스프레드 뺀 +0.11%). 이익 쪽 공통점: 정산 2분 전부터 가격이 받는 쪽에 불리하게 움직임(미리 산 무리가 없음) · 거래량 적음 ·
펀딩이 이번에 처음 치솟음 · 진입 직전 5초 체결이 받는 쪽 반대 우세. 처리 직후 청산(FH6)과 달리 속도 경쟁이 없다(T+5초 청산).
특징 고르기는 전체 기간 표를 보고 했으므로 이 결과는 낙관적일 수 있다 → **아래 규칙을 고정하고 앞으로의 정산으로만 판정한다.**

━━ 사전 고정 (2026-11-18 작성 · 전진 구간 결과를 보기 전) ━━
대상    M 밖 USDT 무기한(COIN, TRADING+SETTLING — exp_FH5 universe) 정산 중 |펀딩| ≥ 0.5%.
특징 9개 (전부 T−10초까지 알 수 있는 값. d = 받는 쪽 +1 롱 / −1 숏, p0 = T−10초 이하 마지막 체결가)
  pre2m      d·(p0 / T−2분 1분봉 시가 − 1) / |f|
  pre5s      d·(p0 / T−15초 이후 첫 체결가 − 1) / |f|
  rcv_share  T−15~−10초 체결대금 중 받는 쪽 방향 테이커 비율
  prev_ratio 직전 펀딩이 같은 부호면 |직전| / |f|, 아니면 0
  streak     직전부터 거슬러 같은 부호 · |펀딩| ≥ 0.5% 가 연속된 횟수(최근 6회 안)
  qvol       log10(T−2분 1분봉 거래대금 + 1)     ← 탐색 때의 (T−2분+T−1분)/2 는 진입 뒤 10초가 섞여 바꿈
  nsim       같은 정산 시각에 |펀딩| ≥ 0.5% 인 대상 심볼 수
  age        상장 후 일수(exchangeInfo onboardDate)  ← 탐색 때 2023 전 상장은 결측이었음, 바꿈
  f          log10(|f|)
점수    학습 = 정산 시각 < 2026-05-01. 결측 → 학습 중앙값 · 학습 1~99% 분위로 자르기 · 학습 평균/표준편차로 표준화 ·
        릿지(λ=50) 로 y5 를 맞춘 가중치(FROZEN). **기준 = 학습 점수의 90% 분위 이상이면 거래.**
거래    받는 쪽 · 진입 = p0 · 청산 = T+5초 이하 마지막 체결가 · 비용 0.10% + 실효 반스프레드(진입 T−15~−9초 · 청산 T+4~6초,
        이어진 매수·매도 테이커 체결가 차이의 중앙/2, 없으면 0.02%).
전진    정산 시각 ≥ 2026-11-17 00:00 UTC (11-16 까지는 탐색에 썼다).
판정 시점  60건이 쌓이거나 2027-01-12(8주) 중 먼저. 8주에 30건 미만이면 30건까지 연장.
합격(넷 다)  ① 평균 − 1.65·se > 0  ② 중앙값 > 0  ③ 최다 심볼 빼도 평균 > 0, 그리고 심볼별 평균의 중앙값 > 0
             ④ 시간순 뒤쪽 절반 평균 > 0 (교훈: 반쪽·최근성 — FH6 는 최근에 소멸했다)
참고(판정 아님)  같은 기간 필터 없는 전체 정산 평균 — 필터가 값을 더하는지.
고정 시점 --fit (위 특징 정의 그대로, 스프레드 뺀 순): 학습 상위10% +0.072 ±0.023(449건) · 26-05~08 +0.276 ±0.061(168) ·
  26-09~11-16 **+0.056 ±0.034**(149, 이익 62%, 기준① 경계) — 필터 없음은 각 −0.137 · +0.049 · −0.075.
  탐색 때(+0.11)보다 낮은 건 qvol 에 섞였던 진입 뒤 10초를 뺐기 때문. 기대치는 건당 +0.05% 안팎으로 본다.

    python3 -u -m research.exp_FH7_filter --fit       # 고정값 재현 확인 + 탐색 구간 검증 (주문 없음)
    python3 -u -m research.exp_FH7_filter --update    # 전진 구간 데이터 받기(펀딩·1분봉·aggTrades, 하루 늦게 공개)
    python3 -u -m research.exp_FH7_filter --report    # 전진 결과·판정
"""
from __future__ import annotations

import bisect
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import numpy as np

from research.exp_FH5_post_trend import _get, collect_1m, collect_funding, conn as fh5_conn, events as fh5_events
from research.exp_FH6_proc_exit import _file, conn as fh6_conn
from research.xv_data import DB


def _ms(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


COST = 0.0010
HS_DEFAULT = 0.0002
TRAIN_END = _ms(2026, 5)
FORWARD = _ms(2026, 11, 17)
JUDGE_N, JUDGE_DATE, MIN_N = 60, _ms(2027, 1, 12), 30
LAMBDA = 50
FE = ["pre2m", "pre5s", "rcv_share", "prev_ratio", "streak", "qvol", "nsim", "age", "f"]

# --fit 으로 만든 고정값(2026-11-18). 바꾸지 말 것 — 바꾸면 사전 고정이 깨진다.
FROZEN = {"med": [0.0363751342, 0.0200683968, 0.5404913347, 0.7417804827, 1.0, 5.4985126243, 2.0, 167.8958333333, -2.0897107821], "lo": [-2.8868256376, -0.5804112386, 0.0340102065, 0.0, 0.0, 3.7579171079, 1.0, 0.4760416667, -2.2977123143], "hi": [2.6650283774, 0.7341612099, 1.0, 2.8288884387, 6.0, 6.6784404584, 8.0, 1671.1875, -1.6989700043], "mu": [0.0107674057, 0.0278370448, 0.5412003149, 0.7783553259, 1.7510031208, 5.4315560778, 2.1832367365, 316.7984427633, -2.0487657187], "sd": [0.7996403955, 0.1985513679, 0.2153975227, 0.6070543328, 2.1346174661, 0.6277232971, 1.456256155, 375.9889412059, 0.1863608562], "w": [-0.0008737371, -0.0006170045, -0.0005448669, -0.0001760737, -0.0004589065, -0.0001534658, -0.0006980809, 9.70065e-05, 0.0003318354, 0.000236676], "thresh": 0.0009100656398981254, "n_train": 4486}


def onboard_dates(c):
    c.execute("CREATE TABLE IF NOT EXISTS fh7_onboard (symbol TEXT PRIMARY KEY, onboard INTEGER)")
    have = dict(c.execute("SELECT symbol, onboard FROM fh7_onboard"))
    if not have or "--update" in sys.argv:
        info = _get("exchangeInfo", {})
        rows = [(s["symbol"], int(s["onboardDate"])) for s in info["symbols"] if s.get("onboardDate")]
        c.executemany("INSERT OR REPLACE INTO fh7_onboard VALUES (?,?)", rows)
        c.commit()
        have = dict(rows) | have
    return have


def half_spread(a, lo, hi):
    """창 안에서 바로 이어진 매수테이커·매도테이커 체결가 차이의 중앙 / 2 (실효 스프레드 어림)."""
    a = [x for x in a if lo <= x[0] < hi]
    d = [abs(v[1] / u[1] - 1) for u, v in zip(a, a[1:]) if u[3] != v[3] and v[0] - u[0] < 2_000]
    return float(np.median(d) / 2) if d else np.nan


def build(lo=0, hi=1 << 62):
    """정산마다 특징·결과. 반환: dict 목록."""
    f5 = fh5_conn()
    ev = [e for e in fh5_events(f5) if lo <= e[1] < hi]
    hist = defaultdict(list)
    for s, t, r in f5.execute("SELECT symbol, t, rate FROM fh5_funding ORDER BY symbol, t"):
        hist[s].append((int(t) // 60_000 * 60_000, float(r)))
    bars = defaultdict(dict)
    for s, ft, t, o, q in f5.execute("SELECT symbol, ft, t, open, qvol FROM fh5_1m WHERE abs(t-ft) < 3600000 AND ft >= ? AND ft < ?",
                                     (lo, hi)):
        bars[(s, ft)][(t - ft) // 60_000] = (o, q)
    f5.close()
    c = sqlite3.connect(DB)
    onboard = onboard_dates(c)
    tr = defaultdict(list)
    for s, ft, t, p, q, bm in c.execute("SELECT symbol, ft, t, price, qty, buyer_maker FROM fh6_vol WHERE ft >= ? AND ft < ? "
                                        "ORDER BY symbol, ft, t", (lo, hi)):
        tr[(s, ft)].append((t - ft, p, q, bm))
    c.close()
    nsim = Counter(ft for _, ft, _ in ev)
    rows = []
    for s, ft, r in ev:
        a = tr.get((s, ft))
        b = bars.get((s, ft))
        if not a or not b or -2 not in b:
            continue
        arr = np.array(a)
        tt, pp, qq, bm = arr.T
        i0 = np.where(tt <= -10_000)[0]
        if not len(i0) or tt.max() < 4_000:
            continue
        p0 = pp[i0[-1]]
        d = -np.sign(r)
        f = abs(r)
        p5 = pp[np.where(tt <= 5_000)[0][-1]]
        h = hist[s]
        k = bisect.bisect_left(h, (ft, -9))
        prev = [x[1] for x in h[max(0, k - 6):k]]
        if len(prev) < 2:
            continue
        g = round((h[k][0] - h[k - 1][0]) / 3_600_000) if 0 < k < len(h) else 0
        streak = 0
        for x in reversed(prev):
            if abs(x) >= 0.005 and np.sign(x) == np.sign(r):
                streak += 1
            else:
                break
        pre = (tt >= -15_000) & (tt < -10_000)
        usd = pp * qq
        buy, tot = usd[pre & (bm == 0)].sum(), usd[pre].sum()
        o2, q2 = b[-2]
        hs_in = half_spread(a, -15_000, -9_000)
        hs_out = half_spread(a, 4_000, 6_000)
        gross = f + d * (p5 / p0 - 1)
        rows.append(dict(
            s=s, ft=ft, rate=r, ivl=1 if g <= 2 else (4 if g <= 5 else 8),
            y5=gross - COST,
            net=gross - COST - (HS_DEFAULT if np.isnan(hs_in) else hs_in) - (HS_DEFAULT if np.isnan(hs_out) else hs_out),
            pre2m=d * (p0 / o2 - 1) / f,
            pre5s=d * (p0 / pp[np.where(tt >= -15_000)[0][0]] - 1) / f,
            rcv_share=((buy / tot) if d > 0 else (1 - buy / tot)) if tot > 0 else np.nan,
            prev_ratio=abs(prev[-1]) / f if np.sign(prev[-1]) == np.sign(r) else 0.0,
            streak=streak,
            qvol=np.log10(q2 + 1),
            nsim=nsim[ft],
            age=(ft - onboard[s]) / 86_400_000 if s in onboard else np.nan,
            f=np.log10(f),
        ))
    return rows


def _matrix(rows):
    return np.array([[r[k] for k in FE] for r in rows], float)


def fit(rows):
    tr = [r for r in rows if r["ft"] < TRAIN_END]
    X = _matrix(tr)
    med = np.nanmedian(X, 0)
    X = np.where(np.isnan(X), med, X)
    lo, hi = np.percentile(X, [1, 99], axis=0)
    X = np.clip(X, lo, hi)
    mu, sd = X.mean(0), X.std(0)
    A = np.c_[np.ones(len(tr)), (X - mu) / sd]
    y = np.array([r["y5"] for r in tr])
    w = np.linalg.solve(A.T @ A + LAMBDA * np.eye(A.shape[1]), A.T @ y)
    sc = A @ w
    return dict(med=med.tolist(), lo=lo.tolist(), hi=hi.tolist(), mu=mu.tolist(), sd=sd.tolist(), w=w.tolist(),
                thresh=float(np.percentile(sc, 90)), n_train=len(tr))


def score(rows, m):
    if not rows:
        return np.array([])
    X = _matrix(rows)
    X = np.where(np.isnan(X), m["med"], X)
    X = np.clip(X, m["lo"], m["hi"])
    Z = (X - np.array(m["mu"])) / np.array(m["sd"])
    return np.c_[np.ones(len(rows)), Z] @ np.array(m["w"])


def summary(sel, title, allrows=None):
    if not sel:
        print(f"\n■ {title}: 0건")
        return None
    v = np.array([r["net"] for r in sel])
    se = v.std(ddof=1) / np.sqrt(len(v)) if len(v) > 1 else np.nan
    cnt = Counter(r["s"] for r in sel)
    top = cnt.most_common(1)[0][0]
    no_top = np.array([r["net"] for r in sel if r["s"] != top])
    per = defaultdict(list)
    for r in sel:
        per[r["s"]].append(r["net"])
    sym_med = float(np.median([np.mean(x) for x in per.values()]))
    late = np.array([r["net"] for r in sorted(sel, key=lambda r: r["ft"])][len(sel) // 2:])
    c1 = v.mean() - 1.65 * se > 0
    c2 = np.median(v) > 0
    c3 = (no_top.mean() > 0 if len(no_top) else False) and sym_med > 0
    c4 = late.mean() > 0
    print(f"\n■ {title} · {len(v)}건 · {len(cnt)}심볼")
    print(f"   순(비용 0.10% + 실효 스프레드) 평균 {v.mean()*100:+.3f} ±{se*100:.3f}% · 중앙 {np.median(v)*100:+.3f}% · 이익 {np.mean(v>0):.0%}")
    print(f"   ① 평균−1.65se {(v.mean()-1.65*se)*100:+.3f} {'✅' if c1 else '❌'} · ② 중앙 {'✅' if c2 else '❌'} · "
          f"③ 최다 {top}({cnt[top]}) 빼면 {no_top.mean()*100 if len(no_top) else float('nan'):+.3f} / 심볼 중앙 {sym_med*100:+.3f} "
          f"{'✅' if c3 else '❌'} · ④ 뒤쪽 절반 {late.mean()*100:+.3f} {'✅' if c4 else '❌'}")
    if allrows:
        a = np.array([r["net"] for r in allrows])
        print(f"   참고: 같은 기간 필터 없음 {len(a)}건 평균 {a.mean()*100:+.3f}%")
    return c1 and c2 and c3 and c4


def update():
    print("1) 펀딩 기록", flush=True)
    collect_funding()
    print("2) 정산 전후 1분봉", flush=True)
    collect_1m()
    print("3) aggTrades (전진 구간 · 공개된 날만)", flush=True)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    c = fh6_conn()
    done = {(s, d) for s, d in c.execute("SELECT symbol, day FROM fh6_vol_done")}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    f5 = fh5_conn()
    by = defaultdict(list)
    for s, ft, r in fh5_events(f5):
        if ft < FORWARD:
            continue
        d = datetime.fromtimestamp(ft / 1000, timezone.utc).strftime("%Y-%m-%d")
        if d < today and (s, d) not in done:
            by[(s, d)].append(ft)
    f5.close()
    print(f"   받을 파일 {len(by)}개", flush=True)
    miss = 0
    with ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(_file, s, d, fts, True): (s, d) for (s, d), fts in by.items()}
        for fu in as_completed(futs):
            s, d = futs[fu]
            try:
                rows = fu.result()
            except Exception as e:
                print(f"   {s} {d} 실패 {e}", flush=True)
                continue
            if rows is None:                       # 아직 공개 전 — 표시하지 않고 다음에 다시
                miss += 1
                continue
            if rows:
                c.executemany("INSERT INTO fh6_vol VALUES (?,?,?,?,?,?)", rows)
            c.execute("INSERT OR REPLACE INTO fh6_vol_done VALUES (?,?,?)", (s, d, len(rows)))
            c.commit()
    c.close()
    if miss:
        print(f"   아직 공개 안 된 파일 {miss}개 — 내일 다시 --update", flush=True)
    c = sqlite3.connect(DB)
    onboard_dates(c)
    c.close()


def main():
    if "--update" in sys.argv:
        update()
        return
    if "--fit" in sys.argv:
        rows = build(hi=FORWARD)
        m = fit(rows)
        print(f"학습 {m['n_train']}건 · 기준(학습 90% 분위) {m['thresh']*100:+.4f}")
        print("가중치(표준화 1단위당, %): " + " · ".join(f"{k} {v*100:+.3f}" for k, v in zip(FE, m["w"][1:])))
        if FROZEN:
            same = all(np.allclose(m[k], FROZEN[k]) for k in ("w", "mu", "sd")) and np.isclose(m["thresh"], FROZEN["thresh"])
            print(f"고정값 재현: {'✅ 같음' if same else '❌ 다름 — 데이터가 바뀌었나 확인'}")
        else:
            print("FROZEN = " + json.dumps({k: (np.round(v, 10).tolist() if isinstance(v, list) else v) for k, v in m.items()}))
        sc = score(rows, FROZEN or m)
        th = (FROZEN or m)["thresh"]
        for name, a, b in (("학습 ~26-04", 0, TRAIN_END), ("탐색 검증 26-05~08", TRAIN_END, _ms(2026, 9)),
                           ("탐색 검증 26-09~11-16", _ms(2026, 9), FORWARD)):
            part = [r for r in rows if a <= r["ft"] < b]
            summary([r for r, s in zip(rows, sc) if a <= r["ft"] < b and s >= th], f"{name} 상위 10%", part)
        return
    if "--report" in sys.argv:
        if not FROZEN:
            sys.exit("FROZEN 이 비어 있어요 — --fit 결과를 붙여 고정하세요")
        rows = build(lo=FORWARD)
        sc = score(rows, FROZEN)
        sel = [r for r, s in zip(rows, sc) if s >= FROZEN["thresh"]]
        last = max((r["ft"] for r in rows), default=FORWARD)
        days = max((last - FORWARD) / 86_400_000, 1e-9)
        print(f"FH7 전진 검증 · {datetime.fromtimestamp(FORWARD/1000, timezone.utc):%Y-%m-%d} ~ "
              f"{datetime.fromtimestamp(last/1000, timezone.utc):%Y-%m-%d %H시} UTC · 대상 정산 {len(rows)}건 · 거래 {len(sel)}건 "
              f"(하루 {len(sel)/days:.1f}건)")
        for r, s in sorted(zip(rows, sc), key=lambda x: x[0]["ft"]):
            if s >= FROZEN["thresh"]:
                print(f"   {datetime.fromtimestamp(r['ft']/1000, timezone.utc):%m-%d %H시} {r['s']:<16} 펀딩 {r['rate']*100:+.3f}% "
                      f"{r['ivl']}h · 점수 {s*100:+.3f} · 순 {r['net']*100:+.3f}%")
        ok = summary(sel, "전진 구간 · 상위 10%", rows)
        due = len(sel) >= JUDGE_N or (last >= JUDGE_DATE and len(sel) >= MIN_N)
        print(f"\n판정 시점: {'도달 → ' + ('✅ 합격' if ok else '❌ 불합격') if due else f'아직 ({len(sel)}/{JUDGE_N}건 · 기한 2027-01-12, 최소 {MIN_N}건)'}")
        return
    print(__doc__)


if __name__ == "__main__":
    main()
