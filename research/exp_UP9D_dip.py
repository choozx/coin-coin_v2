"""UP9D — 업비트 9시 펌핑 직전 '가짜 하락'의 크기(틱·%) (사용자 관찰, 2026-11-20).

사용자: "펌핑 오는 코인은 보통 9시 시작하고 몇 틱 내려가는 가짜 움직임을 보인 뒤 펌핑이 온다. 평균 몇 틱 내려가나?" (보유는 길어도 10분.)

━━ 측정 정의 (2026-11-20 작성 · 결과 보기 전) ━━
대상    UP9 펌핑(09시 1시간봉 고가 +10% & 거래대금 5배) 코인·날짜 전부 · 비교군 = 같은 날 펌핑 안 한 코인 무작위 1개씩(고정 시드).
        스테이블 제외. 기간 = UP9 전체(2025-01 ~ 2026-11-18).
데이터  업비트 1분봉 08:58 ~ 09:59 KST(공개 API candles/minutes/1).
기준가  08:59 1분봉 종가(9시 직전 마지막 체결가).
펌핑 시작 = 09:00 부터 처음으로 1분봉 고가 ≥ 기준가 × 1.03 인 분 k (비교군도 같은 정의, 없으면 '시작 없음').
가짜 하락 = 09:00 부터 펌핑 시작 전까지 최저가 vs 기준가.  1분봉이라 분 안 순서를 몰라 두 가지로 보고:
        '엄격' = k 이전 분들만(k=09:00 이면 측정 불가) · '느슨' = k 분의 저가까지 포함.
틱      그 코인 그날 1분봉 가격(시고저종)들의 서로 다른 값 간 최소 양의 차이(실측 호가 단위).
보고    펌핑 시작 분 분포 · 하락 있었던 비율 · 하락 깊이(틱·%) 분위 · 비교군과 비교(같은 깊이 하락이 펌핑 안 한 날에도 흔한가).

    python3 -u -m research.exp_UP9D_dip --collect
    python3 -u -m research.exp_UP9D_dip
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

from engine.xs_mom import STABLE
from research import exp_UP9_upbit_pump as up

SEED = 20261120


def conn():
    c = sqlite3.connect(up.UDB, timeout=60)
    c.execute("CREATE TABLE IF NOT EXISTS up9d_1m (coin TEXT, d INTEGER, grp INTEGER, k INTEGER, o REAL, h REAL, l REAL, c REAL,"
              " PRIMARY KEY (coin, d, k))")
    c.execute("CREATE TABLE IF NOT EXISTS up9d_done (coin TEXT, d INTEGER, grp INTEGER, n INTEGER, PRIMARY KEY (coin, d))")
    return c


def targets():
    rows = up.build()
    coin = np.array([r[0] for r in rows]); t = np.array([r[1] for r in rows]); y = np.array([r[2] for r in rows])
    ok = ~np.isin(coin, list(STABLE | {"USDT"}))
    rng = np.random.default_rng(SEED)
    out = []
    for d in sorted(set(t[ok & y])):
        day = ok & (t == d)
        for k in np.where(day & y)[0]:
            out.append((coin[k], int(d), 1))
        non = np.where(day & ~y)[0]
        if len(non):
            for k in rng.choice(non, min(len(non), int((day & y).sum())), replace=False):
                out.append((coin[k], int(d), 0))
    return out


def fetch(coin, d):
    """d = 09:00 KST(00:00 UTC) ms. 08:58 ~ 09:59 KST 1분봉 → k = −2 … 59."""
    to = datetime.fromtimestamp((d + 3_600_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{up.UP}/candles/minutes/1?market=KRW-{coin}&to={to}&count=62"
    rows = up._get(url)
    out = []
    for r in rows or []:
        tt = int(datetime.strptime(r["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
        k = (tt - d) // 60_000
        if -2 <= k <= 59:
            out.append((int(k), r["opening_price"], r["high_price"], r["low_price"], r["trade_price"]))
    return out


def collect():
    c = conn()
    done = {(a, b) for a, b in c.execute("SELECT coin, d FROM up9d_done")}
    jobs = [j for j in targets() if (j[0], j[1]) not in done]
    print(f"받을 것 {len(jobs)} (펌핑 {sum(j[2] for j in jobs)} · 비교군 {sum(1-j[2] for j in jobs)})", flush=True)
    for n, (coin, d, g) in enumerate(jobs, 1):
        rows = fetch(coin, d)
        time.sleep(0.11)
        c.executemany("INSERT OR REPLACE INTO up9d_1m VALUES (?,?,?,?,?,?,?,?)", [(coin, d, g, *r) for r in rows])
        c.execute("INSERT OR REPLACE INTO up9d_done VALUES (?,?,?,?)", (coin, d, g, len(rows)))
        if n % 200 == 0:
            c.commit()
            print(f"  {n}/{len(jobs)}", flush=True)
    c.commit()
    c.close()


def tick_of(vals):
    v = np.unique(np.round(np.asarray(vals, dtype=float), 10))
    dif = np.diff(v)
    dif = dif[dif > 1e-12]
    return dif.min() if len(dif) else np.nan


def measure():
    c = conn()
    out = []
    for coin, d, g in c.execute("SELECT coin, d, grp FROM up9d_done WHERE n > 0").fetchall():
        a = c.execute("SELECT k, o, h, l, c FROM up9d_1m WHERE coin=? AND d=? ORDER BY k", (coin, d)).fetchall()
        m = {r[0]: r[1:] for r in a}
        if -1 not in m:
            continue
        ref = m[-1][3]
        ks = [k for k in range(0, 60) if k in m]
        if not any(k <= 9 for k in ks) or ref <= 0:          # 09:00~09:09 체결 없음 → 제외(건수는 main 에서 보고)
            continue
        tick = tick_of([x for r in a for x in r[1:]])
        start = next((k for k in ks if m[k][1] >= ref * 1.03), None)
        pre = [m[k][2] for k in ks if start is None or k < start]
        loose = pre + ([m[start][2]] if start is not None else [])
        strict_low = min(pre) if pre else np.nan
        loose_low = min(loose) if loose else np.nan
        hi10 = max(m[k][1] for k in ks if k <= 9)
        c10 = m[9][3] if 9 in m else (m[max(k for k in ks if k <= 9)][3])
        out.append(dict(coin=coin, d=d, g=g, ref=ref, tick=tick, start=start,
                        s_pct=strict_low / ref - 1 if pre else np.nan, l_pct=loose_low / ref - 1,
                        s_tk=(ref - strict_low) / tick if pre and tick > 0 else np.nan,
                        l_tk=(ref - loose_low) / tick if tick > 0 else np.nan,
                        hi10=hi10 / ref - 1, c10=c10 / ref - 1,
                        e2=min(m[k][2] for k in ks if k <= 2) / ref - 1 if any(k <= 2 for k in ks) else np.nan))
    c.close()
    return out


def q(v, qs=(25, 50, 75, 90)):
    v = np.asarray([x for x in v if x == x])
    return " / ".join(f"{np.percentile(v, p):.1f}" for p in qs) if len(v) else "-"


def main():
    if "--collect" in sys.argv:
        return collect()
    ev = measure()
    P = [e for e in ev if e["g"] == 1]; Nn = [e for e in ev if e["g"] == 0]
    c = conn()
    tot = dict(c.execute("SELECT grp, COUNT(*) FROM up9d_done GROUP BY grp").fetchall())
    c.close()
    print(f"측정: 펌핑 {len(P)}/{tot.get(1)}건 · 비교군(같은 날 펌핑 안 한 코인) {len(Nn)}/{tot.get(0)}건 (나머지는 09:00~09:09 체결 없음 등)")
    st = [e["start"] for e in P if e["start"] is not None]
    print(f"\n펌핑 시작(첫 고가 +3%) 분: 09:00 {np.mean(np.array(st)==0):.0%} · 09:01 {np.mean(np.array(st)==1):.0%} · 09:02~04 "
          f"{np.mean((np.array(st)>=2)&(np.array(st)<=4)):.0%} · 09:05~09 {np.mean((np.array(st)>=5)&(np.array(st)<=9)):.0%} · "
          f"09:10 이후 {np.mean(np.array(st)>=10):.0%} · (+3% 못 감 {sum(e['start'] is None for e in P)}건)")
    for lab, grp in (("펌핑", P), ("비교군", Nn)):
        s = [e for e in grp if e["start"] is not None]
        print(f"\n[{lab}] +3% 도달 {len(s)}/{len(grp)}")
        ss = [e for e in s if e["start"] >= 1]
        print(f"  엄격(시작 전 분들만, 시작이 09:01 이후인 {len(ss)}건): 기준가 아래로 내려간 비율 {np.mean([e['s_pct'] < 0 for e in ss]):.0%} · "
              f"하락 틱 25/50/75/90% = {q([max(0, e['s_tk']) for e in ss])} · 하락 % = {q([min(0, e['s_pct'])*100 for e in ss], (10, 25, 50, 75))}")
        print(f"  느슨(시작 분 저가 포함, {len(s)}건): 내려간 비율 {np.mean([e['l_pct'] < 0 for e in s]):.0%} · "
              f"하락 틱 25/50/75/90% = {q([max(0, e['l_tk']) for e in s])} · 하락 % = {q([min(0, e['l_pct'])*100 for e in s], (10, 25, 50, 75))}")
        dn = [e for e in ss if e["s_pct"] < 0]
        if dn:
            print(f"  (내려간 경우만 {len(dn)}건) 틱 중앙 {np.median([e['s_tk'] for e in dn]):.0f} · % 중앙 {np.median([e['s_pct'] for e in dn])*100:.2f}%")
    print("\n━━ 신호가 되나: 09:00~09:02 사이 기준가 아래로 X% 이상 내려간 코인 중 그날 펌핑 비율 (펌핑·비교군 같은 수) ━━")
    for th in (0.002, 0.005, 0.01, 0.02):
        def early_dip(e):                              # 09:00~09:02 최저가 기준(펌핑·비교군 같은 창)
            return e["e2"] == e["e2"] and e["e2"] <= -th
        a = sum(early_dip(e) for e in P); b = sum(early_dip(e) for e in Nn)
        print(f"  −{th*100:.1f}% 이상: 펌핑 {a}/{len(P)}({a/len(P):.0%}) · 비교군 {b}/{len(Nn)}({b/len(Nn):.0%})")
    print("\n━━ 10분 안 (보유 ≤ 10분 관점, 기준가 대비) ━━")
    for lab, grp in (("펌핑", P), ("비교군", Nn)):
        print(f"  [{lab}] 09:00~09:09 최고가 25/50/75% = {q([e['hi10']*100 for e in grp], (25, 50, 75))}% · 09:09 종가 25/50/75% = {q([e['c10']*100 for e in grp], (25, 50, 75))}%")


if __name__ == "__main__":
    main()
