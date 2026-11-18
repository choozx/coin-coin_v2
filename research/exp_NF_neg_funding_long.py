"""NF — 펀딩이 깊은 마이너스로 고정된 코인을 선물 롱으로 며칠 들고 펀딩을 받는다(사용자 선택 B1, 2026-11-18).

가설: 현물 차입이 막힌 알트는 헤지 숏이 선물로 몰려 펀딩이 −1~−2%/정산으로 몇 주씩 고정된다(CC 알트 손실의 원인 —
AXS 펀딩 −82% 등). 롱은 그 펀딩을 받는다(4h 주기 −2% 면 하루 +12%). 가격 하락이 펀딩보다 작으면 이익, 숏 스퀴즈면 크게 이익.
반론: 그런 코인은 상장폐지·언락 등으로 계속 흘러내린다 → 가격이 펀딩보다 더 빠질 수 있다. 데이터로 가른다.

━━ 사전 고정 (2026-11-18 작성 · 결과 보기 전) ━━
대상    M 밖 USDT 무기한 598심볼(폐지 포함, exp_FH5 universe·fh5_funding) · 2024-01~.
신호    심볼의 정산 시각 t 마다 S24(t) = (t−24h, t] 펀딩 합. **S24 ≤ −1.0%** 이고 그 심볼을 안 들고 있으면 진입.
진입    t 가 속한 1시간봉 다음 봉(floor_h(t)+1h)의 시가 — 정산 직후 급변을 피하고 신호 확인 뒤 들어간다. 1배 롱.
보유 중 진입 < 정산 시각 ≤ 청산 인 정산마다 −rate 를 받는다(롱).
청산    (가) 보유 중 정산에서 S24 > −0.25% 가 되면 그 다음 1시간봉 시가 · (나) 진입 후 7일(168h) 봉 시가 · (다) 데이터 끝(폐지) 이면 마지막 종가.
비용    왕복 0.10%(기본) · 참고로 0.30%(얇은 코인 슬리피지).
결과    거래당 수익(1배) = 가격 수익 + 받은 펀딩 − 비용.
합격(넷 다)  ① 평균 − 2·se > 0  ② 중앙값 > 0  ③ 최다 심볼 빼도 평균 > 0, 그리고 심볼별 평균의 중앙값 > 0
             ④ **반기별(2025-H2 · 2026-H1 · 2026-H2) 평균이 모두 > 0** (FH6 교훈: 최근성)
참고(판정 아님)  S24 −0.5% · −2% 문턱, 보유 3·14일, 손절, 보유 중 최대 역행(레버리지 위험).

    python3 -u -m research.exp_NF_neg_funding_long --collect   # 신호 구간 1시간봉 받기
    python3 -u -m research.exp_NF_neg_funding_long             # 결과
"""
from __future__ import annotations

import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from research.exp_FH5_post_trend import _get, conn as fh5_conn
from research.xv_data import DB

H = 3_600_000
DAY = 24 * H
SINCE = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
TH, TH_EXIT, MAXH = -0.010, -0.0025, 168
COST, COST_HI = 0.0010, 0.0030
HALVES = [("2024-H1", 2024, 1), ("2024-H2", 2024, 7), ("2025-H1", 2025, 1), ("2025-H2", 2025, 7), ("2026-H1", 2026, 1), ("2026-H2", 2026, 7)]


def _ms(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def conn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS nf_1h (symbol TEXT, t INTEGER, open REAL, high REAL, low REAL, close REAL, qvol REAL, "
              "PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS nf_done (symbol TEXT, a INTEGER, b INTEGER, PRIMARY KEY (symbol, a))")
    return c


def funding():
    f5 = fh5_conn()
    h = defaultdict(list)
    for s, t, r in f5.execute("SELECT symbol, t, rate FROM fh5_funding WHERE t >= ? ORDER BY symbol, t", (SINCE - DAY,)):
        h[s].append((int(t) // 60_000 * 60_000, float(r)))
    f5.close()
    out = {}
    for s, a in h.items():
        t = np.array([x[0] for x in a], dtype=np.int64)
        r = np.array([x[1] for x in a])
        cs = np.r_[0, np.cumsum(r)]
        j = np.searchsorted(t, t - DAY, side="right")
        out[s] = (t, r, cs[1:] - cs[j])
    return out


def windows(F, th=-0.005):
    """1시간봉이 필요한 구간: 문턱(가장 느슨한 −0.5%) 신호 −1일 ~ +15일, 겹치면 합침."""
    w = {}
    for s, (t, r, s24) in F.items():
        hit = t[(s24 <= th) & (t >= SINCE)]
        if not len(hit):
            continue
        iv = []
        for x in hit:
            a, b = x // H * H - DAY, x // H * H + 15 * DAY
            if iv and a <= iv[-1][1]:
                iv[-1][1] = max(iv[-1][1], b)
            else:
                iv.append([a, b])
        w[s] = iv
    return w


def collect():
    c = conn()
    F = funding()
    W = windows(F)
    done = {(s, a) for s, a in c.execute("SELECT symbol, a FROM nf_done")}
    todo = [(s, a, b) for s, iv in W.items() for a, b in iv if (s, a) not in done]
    now = int(time.time() * 1000)
    print(f"심볼 {len(W)}개 · 구간 {len(todo)}개 · 총 {sum(b-a for _, a, b in todo)/H:,.0f}시간", flush=True)
    t0 = time.time()
    for n, (s, a, b) in enumerate(todo, 1):
        cur, end = a, min(b, now)
        while cur < end:
            rows = _get("klines", {"symbol": s, "interval": "1h", "startTime": cur, "endTime": end, "limit": 1500})
            time.sleep(0.3)                                           # limit 1500 = weight 10 → 분당 2400 아래
            if not rows:
                break
            c.executemany("INSERT OR REPLACE INTO nf_1h VALUES (?,?,?,?,?,?,?)",
                          [(s, int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7])) for k in rows])
            cur = int(rows[-1][0]) + H
            if len(rows) < 1500:
                break
        if b <= now - DAY:
            c.execute("INSERT OR REPLACE INTO nf_done VALUES (?,?,?)", (s, a, b))
        c.commit()
        if n % 100 == 0:
            el = time.time() - t0
            print(f"  {n}/{len(todo)}  경과 {el/60:.1f}분 · 남은 {el/n*(len(todo)-n)/60:.0f}분", flush=True)
    c.close()


def bars():
    c = conn()
    B = defaultdict(dict)
    for s, t, o, h, l, cl, q in c.execute("SELECT symbol, t, open, high, low, close, qvol FROM nf_1h"):
        B[s][t] = (o, h, l, cl, q)
    c.close()
    return B


def trades(F, B, th=TH, th_exit=TH_EXIT, maxh=MAXH, stop=None):
    out = []
    for s, (t, r, s24) in F.items():
        b = B.get(s)
        if not b:
            continue
        last_bar = max(b)
        i, n = 0, len(t)
        while i < n:
            if t[i] < SINCE or s24[i] > th:
                i += 1
                continue
            et = t[i] // H * H + H
            if et not in b:
                i += 1
                continue
            ep = b[et][0]
            fund, xt, why, k = 0.0, None, "", i + 1
            limit_t = et + maxh * H
            while k < n and t[k] < limit_t:
                if t[k] > et:
                    if stop is not None:                               # 손절: 이 정산 전까지 봉 저가 확인
                        for hh in range(max(et, (t[k - 1] // H) * H), t[k] // H * H + 1, H):
                            if hh in b and b[hh][2] <= ep * (1 + stop):
                                xt, why = hh, "손절"
                                break
                        if xt:
                            break
                    fund -= r[k]
                    if s24[k] > th_exit:
                        xt, why = t[k] // H * H + H, "정상화"
                        break
                k += 1
            if xt is None:
                xt, why = limit_t, "기한"
            if why == "손절":
                xp = ep * (1 + stop)
            elif xt in b:
                xp = b[xt][0]
            else:
                cand = [x for x in b if et <= x <= min(xt, last_bar)]
                if not cand:
                    i = k
                    continue
                xt = max(cand)
                xp, why = b[xt][3], "데이터끝"
            lows = [b[x][2] for x in range(et, xt + 1, H) if x in b]
            mae = min(lows) / ep - 1 if lows else 0.0
            highs = [b[x][1] for x in range(et, xt + 1, H) if x in b]
            out.append(dict(s=s, et=et, xt=xt, price=xp / ep - 1, fund=fund, why=why, mae=mae,
                            mfe=max(highs) / ep - 1 if highs else 0.0, hours=(xt - et) / H, s24=s24[i]))
            while i < n and t[i] < xt:                                 # 보유 중 재진입 없음
                i += 1
    return out


def judge(tr, cost, title):
    if not tr:
        print(f"\n■ {title}: 0건")
        return
    v = np.array([x["price"] + x["fund"] - cost for x in tr])
    se = v.std(ddof=1) / np.sqrt(len(v))
    per = defaultdict(list)
    for x, y in zip(tr, v):
        per[x["s"]].append(y)
    top = max(per, key=lambda k: len(per[k]))
    no_top = np.array([y for x, y in zip(tr, v) if x["s"] != top])
    sym_med = np.median([np.mean(a) for a in per.values()])
    halves = []
    for i, (nm, y, m) in enumerate(HALVES):
        a = _ms(y, m)
        b = _ms(*HALVES[i + 1][1:]) if i + 1 < len(HALVES) else 1 << 62
        hv = np.array([y_ for x, y_ in zip(tr, v) if a <= x["et"] < b])
        halves.append((nm, hv))
    c1, c2 = v.mean() - 2 * se > 0, np.median(v) > 0
    c3 = no_top.mean() > 0 and sym_med > 0
    c4 = all(len(h) and h.mean() > 0 for nm, h in halves[3:])
    print(f"\n■ {title} · {len(v):,}건 · {len(per)}심볼 · 평균 보유 {np.mean([x['hours'] for x in tr]):.0f}시간")
    print(f"   거래당 순 {v.mean()*100:+.2f} ±{se*100:.2f}% · 중앙 {np.median(v)*100:+.2f}% · 이익 {np.mean(v>0):.0%} · "
          f"가격 {np.mean([x['price'] for x in tr])*100:+.2f}% · 펀딩 {np.mean([x['fund'] for x in tr])*100:+.2f}%")
    print(f"   ① 평균−2se {(v.mean()-2*se)*100:+.2f} {'✅' if c1 else '❌'} · ② 중앙 {'✅' if c2 else '❌'} · "
          f"③ 최다 {top}({len(per[top])}) 빼면 {no_top.mean()*100:+.2f} / 심볼 중앙 {sym_med*100:+.2f} {'✅' if c3 else '❌'} · "
          f"④ 25H2·26H1·26H2 {'✅' if c4 else '❌'}")
    print("   반기: " + " · ".join(f"{nm} {h.mean()*100:+.2f}({len(h)})" if len(h) else f"{nm} -" for nm, h in halves))
    q = np.percentile(v, [1, 5, 50, 95, 99]) * 100
    print(f"   분포 1% {q[0]:+.1f} · 5% {q[1]:+.1f} · 95% {q[3]:+.1f} · 99% {q[4]:+.1f} · 최악 {v.min()*100:+.1f} · 최고 {v.max()*100:+.1f} · "
          f"보유 중 최대 역행 중앙 {np.median([x['mae'] for x in tr])*100:+.1f}% · 5% {np.percentile([x['mae'] for x in tr],5)*100:+.1f}%")
    why = defaultdict(list)
    for x, y in zip(tr, v):
        why[x["why"]].append(y)
    print("   청산 사유: " + " · ".join(f"{k} {len(a)}건 {np.mean(a)*100:+.2f}%" for k, a in why.items()))
    return c1 and c2 and c3 and c4


def main():
    if "--collect" in sys.argv:
        collect()
        return
    F, B = funding(), bars()
    tr = trades(F, B)
    ok = judge(tr, COST, f"사전 고정 · S24 ≤ {TH*100:.1f}% · 7일 · 비용 {COST*100:.2f}%")
    print(f"\n판정: {'✅ 합격' if ok else '❌ 불합격'}")
    judge(tr, COST_HI, f"같은 규칙 · 비용 {COST_HI*100:.2f}%")
    print("\n──── 참고(판정 아님) ────")
    for th in (-0.005, -0.02, -0.05):
        judge(trades(F, B, th=th), COST, f"S24 ≤ {th*100:.1f}%")
    for mh in (24, 72, 336):
        judge(trades(F, B, maxh=mh), COST, f"보유 최대 {mh}h")
    for st in (-0.1, -0.2):
        judge(trades(F, B, stop=st), COST, f"손절 {st*100:.0f}%")


if __name__ == "__main__":
    main()
