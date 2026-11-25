"""LWM — 시장 전체 폭락 때만 되사기, 안 본 2020~2023 에서 (사용자: "2번부터 해봐", 2026-11-25).

LW(exp_LW_liq_wick, 2024-01~2026-10)의 사후 발견: 15분 안 −10% 급락이 '같은 15분에 5심볼 이상'일 때만 사면
25일 · 날짜 평균 +3.41 ±1.11%(1시간) · 이익인 날 80%. 결과를 보고 고른 규칙이라 안 본 구간으로 다시 본다.
정의는 LW 데이터로 먼저 재현해 고정했다(win=3 → 25일 · +3.41 ±1.11% · 80% 일치, 2026-11-25).

━━ 사전 고정 (2026-11-25 작성 · 2020~2023 결과 보기 전) ━━
데이터  data.binance.vision 선물 월별 5분봉 · USDT 무기한 코인 680개(xs_long.db 의 sym — 폐지 포함, 지수·스테이블·TradFi 제외)
        · 2020-01 ~ 2024-01 받고, 사건은 2020-01-01 ~ 2023-12-31 만.
급락    LW 그대로 — 5분봉 종가 / 최근 3봉 최고가 − 1 ≤ −10% · 최근 3봉 거래대금 ≥ 5 × (직전 24h 5분 중앙 × 3)
        · 직전 24h 5분 거래대금 중앙 ≥ $20,000 · 같은 심볼 4시간 쿨다운.
시장 폭락  그 급락 봉 t 에서 [t − 15분, t] 의 5분봉 4개 안에 급락한 서로 다른 심볼 ≥ 5 (진입 시점에 알 수 있는 것만).
거래    조건을 만족한 급락마다 다음 봉 시가 롱 → 12봉 뒤 종가(1시간). 1배. 비용 왕복 0.20%.
표본    날짜(UTC) 단위 — 그날 거래들의 평균 = 하루 표본 하나(같은 폭락 안의 심볼들은 독립이 아니다).
판정(다섯 다)  ① 날짜 평균 − 2·se > 0  ② 이익인 날 ≥ 60%  ③ 최고의 날 빼도 날짜 평균 > 0
               ④ 사건 있는 연도(2020~2023) 중 3/4 이상 평균 > 0(사건 2일 미만 연도는 세지 않음)
               ⑤ 비용 0.40%(폭락 중 슬리피지 두 배)에서도 날짜 평균 > 0
               다섯 다 → '안 본 구간에서도 재현' · ①만 → '약하게' · ① 실패 → 기각.
참고(판정 아님)  4시간 보유 · K ≥ 3·10 · 2024~2026(LW 데이터) 합친 전체 · 연도별 · 최악의 날 · 사건 수/년 · BTC 급락 동반 여부.

    python3 -u -m research.exp_LWM_market_crash --collect   # 5분봉 받아 사건·창만 저장(data/lwm_oos.db)
    python3 -u -m research.exp_LWM_market_crash             # 결과
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from research import exp_LW_liq_wick as lw
from research.xs_long_data import DB as LONG_DB

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lwm_oos.db")
OOS_END = lw._ms(2024, 1)
WIN, K = 3, 5
YEARS = (2020, 2021, 2022, 2023)


def conn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS lw_event (symbol TEXT, t INTEGER, move REAL, volx REAL, medq REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS lw_5m (symbol TEXT, ev INTEGER, k INTEGER, open REAL, high REAL, low REAL, close REAL, "
              "PRIMARY KEY (symbol, ev, k))")
    c.execute("CREATE TABLE IF NOT EXISTS lw_done (symbol TEXT PRIMARY KEY, months INTEGER, bars INTEGER)")
    return c


def collect():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    lw.START, lw.END = (2020, 1), (2024, 1)          # lw._months 가 이 둘을 읽는다
    lc = sqlite3.connect(LONG_DB)
    syms = [(s, int(f)) for s, f in lc.execute("SELECT symbol, first FROM sym WHERE first IS NOT NULL") if f < OOS_END]
    lc.close()
    c = conn()
    done = {r[0] for r in c.execute("SELECT symbol FROM lw_done")}
    todo = [x for x in syms if x[0] not in done]
    print(f"심볼 {len(syms)}개(2024 전 상장) · 받을 것 {len(todo)}개", flush=True)
    t0, k = time.time(), 0
    with ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(lw._symbol, s, ob): s for s, ob in todo}
        for fu in as_completed(futs):
            s = futs[fu]
            try:
                sym, n_m, n, ev, win = fu.result()
            except Exception as e:      # noqa: BLE001
                print(f"  {s} 실패 {e}", flush=True)
                continue
            c.executemany("INSERT OR REPLACE INTO lw_event VALUES (?,?,?,?,?)", ev)
            c.executemany("INSERT OR REPLACE INTO lw_5m VALUES (?,?,?,?,?,?,?)", win)
            c.execute("INSERT OR REPLACE INTO lw_done VALUES (?,?,?)", (sym, n_m, n))
            c.commit()
            k += 1
            if k % 25 == 0:
                el = time.time() - t0
                print(f"  {k}/{len(todo)}  경과 {el/60:.1f}분 · 남은 {el/k*(len(todo)-k)/60:.0f}분", flush=True)
    c.close()


def load(path):
    c = sqlite3.connect(path)
    E = list(c.execute("SELECT symbol, t, move, volx, medq FROM lw_event ORDER BY symbol, t"))
    W = defaultdict(dict)
    for s, ev, k, o, h, l, cl in c.execute("SELECT symbol, ev, k, open, high, low, close FROM lw_5m"):
        W[(s, ev)][k] = (o, h, l, cl)
    c.close()
    return E, W


def market(tr, k=K, win=WIN):
    """급락 거래 중 '그 봉까지 15분 안에 급락한 심볼 ≥ k' 인 것만."""
    at = defaultdict(set)
    for x in tr:
        at[x["t"]].add(x["s"])
    out = []
    for x in tr:
        n = len({s for tt in range(x["t"] - win * lw.M5, x["t"] + 1, lw.M5) for s in at.get(tt, ())})
        if n >= k:
            out.append(dict(x, n=n))
    return out


def by_day(tr, extra_cost=0.0):
    d = defaultdict(list)
    for x in tr:
        d[x["day"]].append(x["r"] - extra_cost)
    return {k: float(np.mean(v)) for k, v in sorted(d.items())}, {k: len(v) for k, v in d.items()}


def show(name, tr):
    days, n = by_day(tr)
    v = np.array(list(days.values()))
    if len(v) < 2:
        print(f"\n■ {name}: {len(v)}일")
        return days
    se = v.std(ddof=1) / np.sqrt(len(v))
    print(f"\n■ {name} · {len(v)}일 · 거래 {len(tr)}건")
    print(f"  날짜 평균 {v.mean()*100:+.2f} ±{se*100:.2f}% · 중앙 {np.median(v)*100:+.2f}% · 이익인 날 {np.mean(v > 0):.0%}"
          f" · 최악 {v.min()*100:+.1f}% · 최고 {v.max()*100:+.1f}%")
    ys = defaultdict(list)
    for d, r in days.items():
        ys[d[:4]].append(r)
    print("  연도별 " + " · ".join(f"{y} {np.mean(r)*100:+.2f}({len(r)}일)" for y, r in sorted(ys.items())))
    return days


def main():
    if "--collect" in sys.argv:
        collect()
        return
    E, W = load(DB)
    E = [e for e in E if e[1] < OOS_END]
    c = conn()
    n_sym = c.execute("SELECT COUNT(*), SUM(bars > 0) FROM lw_done").fetchone()
    c.close()
    print(f"심볼 {n_sym[0]}개(5분봉 있는 것 {n_sym[1]}) · 급락 후보 저장 {len(E):,}건 · 2020-01 ~ 2023-12")
    tr = lw.trades(E, W)
    mk = market(tr)
    print(f"전체 급락(−10% · 거래량 5배) {len(tr):,}건 · 그중 시장 폭락 조건 {len(mk):,}건")

    days = show("사전 고정 · 시장 폭락(15분 안 ≥ 5심볼) → 롱 1시간 · 비용 0.20%", mk)
    v = np.array(list(days.values()))
    if len(v) >= 2:
        se = v.std(ddof=1) / np.sqrt(len(v))
        best = max(days, key=days.get)
        ys = defaultdict(list)
        for d, r in days.items():
            ys[int(d[:4])].append(r)
        cnt = [y for y in YEARS if len(ys[y]) >= 2]
        pos = [y for y in cnt if np.mean(ys[y]) > 0]
        v2 = np.array(list(by_day(mk, 0.0020)[0].values()))
        c1 = v.mean() - 2 * se > 0
        c2 = np.mean(v > 0) >= 0.60
        c3 = np.mean([r for d, r in days.items() if d != best]) > 0
        c4 = bool(cnt) and len(pos) >= 0.75 * len(cnt)
        c5 = v2.mean() > 0
        cs = [c1, c2, c3, c4, c5]
        print(f"\n■ 판정  ① 평균−2se {(v.mean()-2*se)*100:+.2f}% · ② 이익인 날 {np.mean(v>0):.0%} · "
              f"③ 최고의 날 {best}({days[best]*100:+.1f}) 빼면 {np.mean([r for d, r in days.items() if d != best])*100:+.2f}% · "
              f"④ {len(pos)}/{len(cnt)} 연도 + · ⑤ 비용 0.40% {v2.mean()*100:+.2f}%")
        print(f"  [{''.join('○' if x else '×' for x in cs)}] → "
              + ("안 본 구간에서도 재현 ✅" if all(cs) else "약하게 △" if c1 else "기각 ❌"))
        print("\n  날짜별: " + " · ".join(f"{d} {r*100:+.1f}" for d, r in days.items()))

    print("\n──── 참고(판정 아님) ────")
    show("4시간 보유", market(lw.trades(E, W, hold=48)))
    for k in (3, 10):
        show(f"K ≥ {k}", market(tr, k=k))
    btc = {x["day"] for x in lw.trades(E, W, th=-0.05, minq=0) if x["s"] == "BTCUSDT"}
    show("BTC 도 15분 −5% 급락한 날", [x for x in mk if x["day"] in btc])
    show("BTC 는 안 빠진 날", [x for x in mk if x["day"] not in btc])

    E2, W2 = load(lw.DB)
    tr2 = lw.trades(E2, W2)
    show("재현 2024-01 ~ 2026-10(LW 데이터)", market(tr2))
    show("전체 2020 ~ 2026-10", market(tr) + market(tr2))


if __name__ == "__main__":
    main()
