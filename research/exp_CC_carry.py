"""CC — 캐리(현물 롱 + 선물 숏): 펀딩 양수를 가격 위험 없이 받는다. 차입 불필요.

배경: M(펀딩 횡단면)은 펀딩 +7~8%p 를 받고 가격 다리에서 −24~−40%p 를 잃었다. FH3(정산만 걸치기 + 현물 숏)은
현물 차입 풀이 비어 막혔다. 남은 깨끗한 형태 = **펀딩이 양수인 코인을 현물로 사고 같은 양을 선물로 숏.**
가격은 상쇄되고 남는 건  펀딩 + 베이시스 변화(현물 수익률 − 선물 수익률) − 비용.

판정 기준(사전 고정):
  · 비교 대상은 '랜덤 선택'이 아니라 **무위험 4%/년**과 **BTC·ETH 상시 캐리**(가장 단순한 대안)
  · 자본 = 명목 × 1.33 (현물 1 + 선물 숏 증거금 3배 레버리지 0.33). 포트폴리오 마진이면 ~1.0 이지만 보수적으로.
  · 비용 = 한쪽(현물+선물) 0.15% (현물 테이커 0.10% + 선물 0.05%) · 낙관 0.10%
  · 연도별로 보고, **2024 이후 연 4% 를 넘는가**가 핵심(XV 는 2021 만 컸다)
  · 위험: 보유 중 선물 가격 최대 상승(3배면 +33% 에 증거금 소진 → 보충 필요) 횟수

선택 규칙(동적): R 시간마다 직전 L 일 평균 펀딩(하루 합) 이 thr 초과인 코인 중 상위 k 개, 동일 명목 1/k.
모자라면 그만큼 현금(수익 0). 현물이 없는(상장 전·폐지) 시점은 대상에서 뺀다.

데이터: candles.db 선물 1h(M 유니버스 56심볼 · 2021-01~) · funding · 현물 1h(공개 API, --collect).

    python3 -u -m research.exp_CC_carry --collect
    python3 -u -m research.exp_CC_carry
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np

from engine import candle_store as cs
from research.xv_data import DB

H = 3_600_000
DAY = 24 * H
START = int(datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
SAPI = "https://api.binance.com/api/v3/klines"
CAP = 1.33
RF = 0.04


def _symbols():
    c = sqlite3.connect(cs.DB_PATH)
    s = sorted(r[0][:-3] for r in c.execute("SELECT DISTINCT symbol FROM candle") if r[0].endswith("USDT_1H"))
    c.close()
    return s


def sconn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS spot_1h (symbol TEXT, t INTEGER, close REAL, PRIMARY KEY (symbol, t))")
    return c


def collect():
    c = sconn()
    for n, s in enumerate(_symbols(), 1):
        last = c.execute("SELECT MAX(t) FROM spot_1h WHERE symbol=?", (s,)).fetchone()[0]
        t = (last + H) if last else START
        got = 0
        while True:
            q = urllib.parse.urlencode({"symbol": s, "interval": "1h", "startTime": t, "limit": 1000})
            try:
                with urllib.request.urlopen(urllib.request.Request(f"{SAPI}?{q}",
                                            headers={"User-Agent": "research-client/0.1"}), timeout=30) as r:
                    rows = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    rows = []                                          # 현물 없음
                else:
                    time.sleep(3)
                    continue
            except urllib.error.URLError:
                time.sleep(3)
                continue
            if not rows:
                break
            c.executemany("INSERT OR REPLACE INTO spot_1h VALUES (?,?,?)", [(s, int(k[0]), float(k[4])) for k in rows])
            got += len(rows)
            t = int(rows[-1][0]) + H
            if len(rows) < 1000:
                break
            time.sleep(0.12)
        c.commit()
        print(f"  {n:2d} {s:12} +{got:,}", flush=True)
    c.close()


def load():
    syms = _symbols()
    cc = sqlite3.connect(cs.DB_PATH)
    end = cc.execute("SELECT MAX(open_time) FROM candle WHERE symbol='BTCUSDT_1H'").fetchone()[0]
    grid = np.arange(START, end + 1, H, dtype=np.int64)            # 봉 시작 시각 · 가격 = 그 봉 종가(= 시각 grid+H)
    n, m = len(grid), len(syms)
    P = np.full((n, m), np.nan)
    S = np.full((n, m), np.nan)
    F = np.zeros((n, m))
    for j, s in enumerate(syms):
        rows = cc.execute("SELECT open_time, close FROM candle WHERE symbol=? AND open_time>=?", (s + "_1H", START)).fetchall()
        idx = (np.array([r[0] for r in rows], dtype=np.int64) - START) // H
        ok = (idx >= 0) & (idx < n)
        P[idx[ok], j] = np.array([r[1] for r in rows])[ok]
        for ft, r in cc.execute("SELECT funding_time, rate FROM funding WHERE symbol=? AND funding_time>=?", (s, START)):
            i = (int(ft) // H * H - H - START) // H                    # 정산 시각 = 봉 i 의 끝
            if 0 <= i < n:
                F[i, j] += float(r)
    cc.close()
    c = sconn()
    for j, s in enumerate(syms):
        rows = c.execute("SELECT t, close FROM spot_1h WHERE symbol=? AND t>=?", (s, START)).fetchall()
        if rows:
            idx = (np.array([r[0] for r in rows], dtype=np.int64) - START) // H
            ok = (idx >= 0) & (idx < n)
            S[idx[ok], j] = np.array([r[1] for r in rows])[ok]
    c.close()
    # 짧은 결측은 직전 값으로(최대 3시간), 그 이상은 결측 유지
    for A in (P, S):
        for j in range(m):
            col = A[:, j]
            gap = 0
            for i in range(1, n):
                if np.isnan(col[i]) and not np.isnan(col[i - 1]) and gap < 3:
                    col[i] = col[i - 1]
                    gap += 1
                elif not np.isnan(col[i]):
                    gap = 0
    return grid, syms, P, S, F


def simulate(grid, P, S, F, L=7, R=168, k=5, thr=0.0, cost=0.0015, fixed=None, keep=1.0, comp=None):
    """→ 시간별 (손익 비율·명목 기준), 회전, 투입 비율, 선물 최대 상승 목록."""
    n, m = P.shape
    cumF = np.cumsum(F, axis=0)
    w = np.zeros(m)
    pnl = np.zeros(n)
    deployed = np.zeros(n)
    turn = 0.0
    entry = np.full(m, np.nan)
    runup = []
    peak = np.full(m, np.nan)
    for i in range(1, n):
        live = ~np.isnan(P[i]) & ~np.isnan(S[i]) & ~np.isnan(P[i - 1]) & ~np.isnan(S[i - 1])
        if w.any():
            hold = w > 0
            bad = hold & ~live                                         # 데이터 끊김 → 그 봉에서 청산(손익 0)
            g = np.where(live, (S[i] / np.where(live, S[i - 1], 1) - 1) - (P[i] / np.where(live, P[i - 1], 1) - 1), 0)
            pnl[i] += (w * (g + F[i])).sum()
            if comp is not None:
                comp["fund"][i] += (w * F[i]).sum()
                comp["basis"][i] += (w * g).sum()
            if bad.any():
                turn += w[bad].sum()
                w[bad] = 0
            peak = np.where(w > 0, np.fmax(peak, P[i]), np.nan)
        if i % R == 0 and i >= L * 24:
            if fixed is not None:
                sel = [j for j in fixed if live[j]]
            else:
                avg = (cumF[i] - cumF[i - L * 24]) / L                 # 하루 합 평균
                cand = [j for j in np.argsort(-avg) if live[j] and avg[j] > thr]
                if keep > 1:                                           # 히스테리시스: 보유 중이면 상위 keep×k 안·펀딩>0 이면 유지
                    held = [j for j in cand[:int(keep * k)] if w[j] > 0]
                    sel = held + [j for j in cand if j not in held][:max(0, k - len(held))]
                else:
                    sel = cand[:k]
            nw = np.zeros(m)
            for j in sel:
                nw[j] = 1.0 / (len(fixed) if fixed is not None else k)
            for j in np.where((w > 0) & (nw == 0))[0]:
                if not np.isnan(entry[j]) and not np.isnan(peak[j]):
                    runup.append(peak[j] / entry[j] - 1)
            for j in np.where((nw > 0) & (w == 0))[0]:
                entry[j] = P[i, j]
                peak[j] = P[i, j]
            dt = np.abs(nw - w).sum()
            turn += dt
            pnl[i] -= dt * cost
            if comp is not None:
                comp["cost"][i] -= dt * cost
            w = nw
        deployed[i] = w.sum()
    for j in np.where(w > 0)[0]:
        if not np.isnan(entry[j]) and not np.isnan(peak[j]):
            runup.append(peak[j] / entry[j] - 1)
    return pnl, turn, deployed, np.array(runup)


def by_year(grid, pnl, deployed, label):
    yrs = np.array([datetime.fromtimestamp(t / 1000, timezone.utc).year for t in grid])
    cells = []
    for y in range(2021, 2027):
        s = yrs == y
        if not s.any():
            continue
        frac = s.sum() / (365 * 24)
        r = pnl[s].sum() / frac / CAP                                  # 자본 기준 연율(단순 합)
        cells.append(f"{y} {r*100:+6.1f}%")
    s24 = grid >= int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    r24 = pnl[s24].sum() / (s24.sum() / (365 * 24)) / CAP
    eq = np.cumsum(pnl) / CAP
    mdd = (eq - np.maximum.accumulate(eq)).min()
    print(f"   {label:34} " + " · ".join(cells) + f" | 2024~ {r24*100:+5.1f}% · 투입 {deployed.mean():.0%} · MDD {mdd*100:+.1f}%p")
    return r24


def main():
    if "--collect" in sys.argv:
        return collect()
    grid, syms, P, S, F = load()
    has = (~np.isnan(S)).any(axis=0)
    print(f"CC 캐리 · {len(syms)}심볼(현물 있는 {has.sum()}) · {datetime.fromtimestamp(grid[0]/1000, timezone.utc):%Y-%m}~"
          f"{datetime.fromtimestamp(grid[-1]/1000, timezone.utc):%Y-%m} · 자본 = 명목×{CAP} · 연율 = 단순 합 · 무위험 {RF:.0%}")
    j = {s: i for i, s in enumerate(syms)}
    print("\n■ 상시 캐리(고정 종목, 매주 재조정, 비용 0.15%/쪽)")
    for lab, fx in (("BTC", ["BTCUSDT"]), ("ETH", ["ETHUSDT"]), ("BTC+ETH", ["BTCUSDT", "ETHUSDT"]),
                    ("전 종목 동일가중", syms)):
        pnl, turn, dep, ru = simulate(grid, P, S, F, fixed=[j[s] for s in fx if s in j and has[j[s]]])
        by_year(grid, pnl, dep, lab)
    print("\n■ 동적 선택 — 직전 L일 평균 펀딩 > 0 인 상위 k (R 시간마다)")
    for L, R, k in ((7, 168, 5), (7, 168, 10), (3, 24, 5), (14, 168, 5), (30, 720, 5), (7, 168, 3)):
        for cost in (0.0015, 0.0010):
            pnl, turn, dep, ru = simulate(grid, P, S, F, L=L, R=R, k=k, cost=cost)
            yrs = (len(grid) / (365 * 24))
            r24 = by_year(grid, pnl, dep, f"L{L}d R{R}h k{k} 비용{cost*100:.2f}%")
        print(f"      회전 연 {turn/yrs:.1f}배 · 보유 중 선물 최대 상승 중앙 {np.median(ru)*100:+.1f}% · "
              f"+33% 초과 {np.mean(ru > 0.33):.1%} ({(ru > 0.33).sum()}회/{len(ru)})")
    print("\n■ 문턱(연율 환산 펀딩 > x 일 때만) — L7d R168h k5 비용 0.15%")
    for thr_y in (0.05, 0.10, 0.20):
        pnl, turn, dep, ru = simulate(grid, P, S, F, L=7, R=168, k=5, thr=thr_y / 365)
        by_year(grid, pnl, dep, f"펀딩 연 {thr_y:.0%} 초과만")


if __name__ == "__main__":
    main()
