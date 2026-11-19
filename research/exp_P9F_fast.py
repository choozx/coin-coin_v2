"""P9F — 9시(00:00 UTC) 펌핑을 '더 빨리 보고, 더 짧게' 따라타기 (사용자: "짧게 먹고 나와야 해 · 초 싸움으로", 2026-11-20).

P9S v2 는 감지 5~60초 · 지연 0.5초 · 보유 30초~10분을 봤다. 지연이 짧을수록 이익이 컸으므로(0.5→1→2초: +0.55→+0.29→+0.08)
아직 안 본 구역 — **반응 0.1~0.2초 · 감지 1~5초 · 보유 5~30초** — 을 같은 데이터 규칙(v2, 누설·낙관 정정판)으로 본다.

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
데이터  P9S v2 와 같다. USDT 무기한(xs_sym, exchangeInfo 의 PERPETUAL·USDT·COIN 전부 — 폐지 포함) · 2025-07 ~ 2026-10 · 매일 00:00 UTC.
        후보 = 첫 3분 1분봉 고가 최대 / 23:59 종가 − 1 ≥ +2%  ·  유동성(진입 전 정보) = 23:00~23:59 거래대금 ≥ $50만.
        체결 = 그날 aggTrades 00:00 부터 12분(가격·금액·매수/매도 주도).
감지    00:00:00 부터 S초 안에 처음으로 체결가 ≥ 23:59 종가 × (1+θ).
진입    감지 + L 뒤 첫 '매수 주도' 체결가(롱) / '매도 주도'(숏).   청산  진입 + M 직전 마지막 반대 주도 체결가.
비용    왕복 0.20%(P9S 와 같음 — 수수료 0.05×2 + 급등 중 추가 슬리피지 0.05×2).
주 지연 **L = 200ms**(서울 EC2 왕복 ~50ms + 웹소켓 수신·판단·주문 처리). 100ms·500ms 는 참고(속도 민감도).
격자    θ ∈ {+1%, +2%} · S ∈ {1초, 2초, 5초} · M ∈ {5초, 10초, 15초, 30초} · 롱(따라타기)/숏(되돌림) = 48 (L=200ms).
판정    t 는 날짜 묶음. 48개 중 최고 조합이
        ① t ≥ 3.3 (48개 동시 비교)  ② 25H2 · 26H1 · 26H2(7~10월) 모두 > 0
        ③ 최다 심볼 빼도 · 최다 날짜 빼도 > 0 · 심볼별 평균의 중앙 > 0
        ④ 상위 5% 거래(이익 큰 쪽) 빼도 > 0  ← P9S 의 '꼬리 몇 건이 전부' 문제를 미리 막는다
참고(판정 아님): L 100·500ms · 비용 0.10%(수수료만) · 유동성 조건 없음 · 26년 10월 단독.

    .venv/bin/python -u research/exp_P9F_fast.py --setup    # 심볼 목록(xs_sym)
    .venv/bin/python -u research/exp_P9F_fast.py --cand     # 1분봉 월별 zip → 후보(P9S cands2 그대로)
    .venv/bin/python -u research/exp_P9F_fast.py --agg      # 유동성 통과 후보만 aggTrades(P9S _agg2 그대로)
    .venv/bin/python -u research/exp_P9F_fast.py            # 판정
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import exp_P9S_second as p9s  # noqa: E402
from research.exp_P9_hour_pump import HALVES, _ms  # noqa: E402

THS = (0.01, 0.02)
SS = (1_000, 2_000, 5_000)
MS = (5_000, 10_000, 15_000, 30_000)
LAT, LATS_REF = 200, (100, 500)
COST, PQ60 = 0.0020, 5e5
T_MIN = 3.3


def setup():
    c = p9s.conn()
    c.execute("CREATE TABLE IF NOT EXISTS xs_sym (symbol TEXT PRIMARY KEY, base TEXT, onboard INTEGER, done INTEGER)")
    info = json.loads(urllib.request.urlopen(urllib.request.Request(
        "https://fapi.binance.com/fapi/v1/exchangeInfo", headers={"User-Agent": "research/0.1"}), timeout=30).read())
    n = 0
    for s in info["symbols"]:
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("underlyingType") == "COIN":
            c.execute("INSERT OR IGNORE INTO xs_sym VALUES (?,?,?,0)", (s["symbol"], s["baseAsset"], int(s.get("onboardDate", 0))))
            n += 1
    c.commit()
    print(f"심볼 {n}개 (상태 무관)", flush=True)


def collect_agg_liquid():
    """P9S collect_agg2 와 같되 유동성 통과 후보만(판정에 쓰는 것만 받는다)."""
    c = p9s.conn()
    have = {(s, t) for s, t in c.execute("SELECT symbol, t FROM p9s2_tr")}
    jobs = [(s, t) for s, t, pq in c.execute("SELECT symbol, t, pq60 FROM p9s2_cand") if pq >= PQ60 and (s, t) not in have]
    print(f"aggTrades(유동성 통과) {len(jobs)}개", flush=True)

    def work(j):
        try:
            return j, *p9s._agg2(*j)
        except Exception:      # noqa: BLE001
            return j, None, -1
    n = 0
    with ThreadPoolExecutor(8) as ex:
        for f in as_completed([ex.submit(work, j) for j in jobs]):
            (s, t), arr, st = f.result()
            n += 1
            if st == -1:
                continue
            b = [x.tobytes() for x in arr] if arr is not None else [b"", b"", b"", b""]
            c.execute("INSERT OR REPLACE INTO p9s2_tr VALUES (?,?,?,?,?,?,?)", (s, t, *b, st))
            if n % 200 == 0:
                c.commit()
                print(f"  {n}/{len(jobs)}", flush=True)
    c.commit()
    c.close()


def trade(ev, th, S, M, side, lat=LAT, cost=COST, liq=True):
    """P9S trade2 와 같은 체결 규칙(매수 주도 진입 · 매도 주도 청산), 지연·비용만 인자."""
    s, t, ref, pq, ms, px, qv, buy = ev
    if liq and pq < PQ60:
        return None
    hit = np.where((px >= ref * (1 + th)) & (ms <= S))[0]
    if not len(hit):
        return None
    td = ms[hit[0]]
    k = np.searchsorted(ms, td + lat)
    ent = buy if side == 1 else ~buy
    cand = np.where(ent[k:])[0]
    if not len(cand):
        return None
    ie = k + cand[0]
    te = ms[ie]
    if te + M > p9s.WIN:
        return None
    j = np.searchsorted(ms, te + M, side="right") - 1
    ex = ~buy if side == 1 else buy
    prev = np.where(ex[ie + 1:j + 1])[0]
    if not len(prev):
        return None
    jx = ie + 1 + prev[-1]
    return side * (px[jx] / px[ie] - 1) - cost


def run(evs, th, S, M, side, **kw):
    out = []
    for ev in evs:
        r = trade(ev, th, S, M, side, **kw)
        if r is not None:
            out.append((ev[0], ev[1], r))
    return out


def day_t(rs):
    """날짜 묶음 t: 같은 날 여러 심볼은 평균 하나로."""
    by = defaultdict(list)
    for _, t, r in rs:
        by[t].append(r)
    d = np.array([np.mean(v) for v in by.values()])
    if len(d) < 3:
        return float("nan"), len(d)
    return d.mean() / (d.std(ddof=1) / np.sqrt(len(d))), len(d)


def half_of(t):
    hs = [(name, _ms(*ym)) for name, ym in HALVES]
    name = hs[0][0]
    for n_, start in hs:
        if t >= start:
            name = n_
    return name


def judge(rs):
    v = np.array([r for _, _, r in rs])
    t, nd = day_t(rs)
    halves = defaultdict(list)
    for _, tt, r in rs:
        halves[half_of(tt)].append(r)
    hmean = {k: float(np.mean(x)) for k, x in halves.items()}
    bys, byd = defaultdict(list), defaultdict(list)
    for s, tt, r in rs:
        bys[s].append(r)
        byd[tt].append(r)
    tops = max(bys, key=lambda k: sum(bys[k]))
    topd = max(byd, key=lambda k: sum(byd[k]))
    wo_s = [r for s, _, r in rs if s != tops]
    wo_d = [r for _, tt, r in rs if tt != topd]
    sym_med = float(np.median([np.mean(x) for x in bys.values()]))
    cut = np.sort(v)[: int(len(v) * 0.95)] if len(v) >= 20 else v
    ok = [t >= T_MIN,
          all(hmean.get(name, -1) > 0 for name, _ in HALVES),
          bool(wo_s) and np.mean(wo_s) > 0 and bool(wo_d) and np.mean(wo_d) > 0 and sym_med > 0,
          len(cut) > 0 and cut.mean() > 0]
    return dict(n=len(v), days=nd, mean=float(v.mean()), med=float(np.median(v)), win=float(np.mean(v > 0)), t=t,
                halves=hmean, wo_sym=float(np.mean(wo_s)) if wo_s else float("nan"),
                wo_day=float(np.mean(wo_d)) if wo_d else float("nan"), sym_med=sym_med,
                cut5=float(cut.mean()) if len(cut) else float("nan"), worst=float(v.min()), ok=ok)


def fmt(name, j):
    oks = "".join("○" if x else "×" for x in j["ok"])
    return (f"{name:28} {j['n']:5d}건 {j['days']:4d}일 · 평균 {j['mean']*100:+.3f}% · 중앙 {j['med']*100:+.3f}% · 이익 {j['win']:.0%} · "
            f"t {j['t']:+.2f} · 반기 " + "/".join(f"{j['halves'].get(n, float('nan'))*100:+.2f}" for n, _ in HALVES)
            + f" · 상위5%뺌 {j['cut5']*100:+.3f} · 최악 {j['worst']*100:+.1f}% [{oks}]")


def main():
    evs = p9s.load2()
    liq = [e for e in evs if e[3] >= PQ60]
    print(f"후보(체결 있음) {len(evs)}일 · 유동성 통과 {len(liq)}일 · 비용 {COST*100:.2f}% · 주 지연 {LAT}ms\n")
    res = []
    for th in THS:
        for S in SS:
            for M in MS:
                for side in (1, -1):
                    rs = run(liq, th, S, M, side)
                    if len(rs) >= 20:
                        res.append(((th, S, M, side), rs, judge(rs)))
    res.sort(key=lambda x: -(x[2]["t"] if x[2]["t"] == x[2]["t"] else -9))
    lab = lambda k: f"+{k[0]*100:.0f}%·{k[1]//1000}초·{k[2]//1000}초·{'롱' if k[3] == 1 else '숏'}"
    print(f"■ 격자 {len(res)}개(20건 이상) — t 상위 10")
    for k, rs, j in res[:10]:
        print("  " + fmt(lab(k), j))
    longs = [x for x in res if x[0][3] == 1]
    print(f"\n  롱 {len(longs)}개 중 평균 > 0: {sum(x[2]['mean'] > 0 for x in longs)} · 숏 {len(res) - len(longs)}개 중 > 0: "
          f"{sum(x[2]['mean'] > 0 for x in res if x[0][3] == -1)}")
    if not res:
        print("판정할 조합 없음")
        return
    k, rs, j = res[0]
    print(f"\n■ 판정 — 최고 조합 {lab(k)}: {'✅ 합격' if all(j['ok']) else '❌'} "
          f"(① t≥{T_MIN} {'○' if j['ok'][0] else '×'} ② 반기 {'○' if j['ok'][1] else '×'} ③ 제외·중앙 {'○' if j['ok'][2] else '×'} "
          f"④ 상위5% 뺌 {'○' if j['ok'][3] else '×'})")
    print(f"   최다 심볼 뺌 {j['wo_sym']*100:+.3f}% · 최다 날짜 뺌 {j['wo_day']*100:+.3f}% · 심볼 중앙 {j['sym_med']*100:+.3f}%")
    print("\n■ 참고(판정 아님) — 최고 조합 그대로")
    for lat in (0,) + LATS_REF:
        print("  " + fmt(f"지연 {lat}ms", judge(run(liq, *k, lat=lat))) if run(liq, *k, lat=lat) else f"  지연 {lat}ms: 없음")
    print("  " + fmt("비용 0.10%(수수료만)", judge(run(liq, *k, cost=0.0010))))
    al = run(evs, *k, liq=False)
    if len(al) >= 20:
        print("  " + fmt("유동성 조건 없음", judge(al)))
    oct_ = [x for x in rs if x[1] >= _ms(2026, 10)]
    if oct_:
        v = [r for _, _, r in oct_]
        print(f"  26년 10월 단독: {len(v)}건 · 평균 {np.mean(v)*100:+.3f}% · 중앙 {np.median(v)*100:+.3f}%")
    print("\n■ 참고 — 최고 롱 조합의 지연 민감도")
    kl = next((x for x in res if x[0][3] == 1), None)
    if kl:
        for lat in (0, 100, 200, 500, 1000):
            r_ = run(liq, *kl[0], lat=lat)
            if r_:
                v = np.array([r for _, _, r in r_])
                print(f"  {lab(kl[0])} 지연 {lat:4d}ms: {len(v)}건 · 평균 {v.mean()*100:+.3f}% · 중앙 {np.median(v)*100:+.3f}% · t {day_t(r_)[0]:+.2f}")


if __name__ == "__main__":
    if "--setup" in sys.argv:
        setup()
    elif "--cand" in sys.argv:
        p9s.collect_cands2()
    elif "--agg" in sys.argv:
        collect_agg_liquid()
    else:
        main()
