"""FH5 — 정산 후 '내는 쪽' 추세 타기: 다른 심볼로 사전 고정 검증.

발견(사후, M 유니버스 56심볼 · 2024-01~): |펀딩| ≥ 0.5% 정산 직후 가격이 받는 쪽 반대로 1~10분 더 간다.
  내는 쪽(펀딩 + → 롱 · − → 숏)으로 T+1분 시가 진입 → T+10분 시가 청산: 전반 +0.63%(44건) · 후반 +0.47±0.32%(234건), 비용 전.
정산 처리(T+~0.4초) 뒤에 들어가므로 펀딩은 주고받지 않는다.

━━ 사전 고정(2026-11-18, 이 파일을 쓰고 나서 결과를 처음 본다 — 바꾸지 않는다) ━━
  표본   바이낸스 USDT 무기한 전부(거래 중 + 폐지 SETTLING, 기초자산 COIN) **중 M 유니버스 56심볼을 뺀 것**
         2024-01-01 ~ 수집일, |펀딩| ≥ 0.5% 정산 전부(주기 1h·4h·8h 무관)
  규칙   내는 쪽 · 진입 T+1분 1분봉 시가 · 청산 T+10분 1분봉 시가 · 비용 0.10%(테이커 왕복)
  합격   ① 순평균(비용 후) − 2×표준오차 > 0
         ② 심볼 집중 아님: 건수 상위 1심볼을 빼도 순평균 > 0, 그리고 심볼별 순평균 중앙값 > 0
         ③ 시간 안정: 2025-07 전·후 두 반쪽 모두 순평균 > 0
         셋 다 통과해야 ✅. 하나라도 실패면 ❌(표본 내 발견은 우연/선택으로 본다).
  참고(판정 아님): T+5분 청산 · 진입 분봉 거래대금(체결 가능 규모) 구간별 · 부호별 · 주기별.

데이터 비용: fundingRate 는 IP 당 5분 500회 한도 → 0.65초 간격. 심볼 ~600 · 2~3 시간 예상(다시 돌리면 이어서).

    python3 -u -m research.exp_FH5_post_trend --collect-funding
    python3 -u -m research.exp_FH5_post_trend --collect-1m
    python3 -u -m research.exp_FH5_post_trend
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from engine import candle_store as cs
from research.xv_data import DB

M = 60_000
SINCE = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
SPLIT = int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
FAPI = "https://fapi.binance.com/fapi/v1"
TH = 0.005
ENTRY, EXIT, COST = 1, 10, 0.0010


def _get(path, params, retries=5):
    q = urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            req = urllib.request.Request(f"{FAPI}/{path}?{q}", headers={"User-Agent": "research-client/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
                print(f"   한도 {e.code} — 60초 쉼", flush=True)
                time.sleep(60)
                continue
            if e.code == 400:
                return []
            if i == retries - 1:
                raise
        except urllib.error.URLError:
            if i == retries - 1:
                raise
        time.sleep(2 * (i + 1))
    return []


def conn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS fh5_funding (symbol TEXT, t INTEGER, rate REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS fh5_done (symbol TEXT PRIMARY KEY, upto INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS fh5_1m (symbol TEXT, ft INTEGER, t INTEGER, open REAL, qvol REAL, "
              "PRIMARY KEY (symbol, ft, t))")
    return c


def m_universe():
    c = sqlite3.connect(cs.DB_PATH)
    s = {r[0][:-3] for r in c.execute("SELECT DISTINCT symbol FROM candle") if r[0].endswith("USDT_1H")}
    c.close()
    return s


def universe():
    info = _get("exchangeInfo", {})
    mu = m_universe()
    return sorted(s["symbol"] for s in info["symbols"]
                  if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
                  and s.get("underlyingType") == "COIN" and s.get("status") in ("TRADING", "SETTLING")
                  and s["symbol"] not in mu)


def collect_funding():
    c = conn()
    syms = universe()
    done = dict(c.execute("SELECT symbol, upto FROM fh5_done"))
    now = int(time.time() * 1000)
    todo = [s for s in syms if done.get(s, 0) < now - 6 * 3600_000]
    print(f"심볼 {len(syms)}개(M 유니버스 제외) · 받을 것 {len(todo)}개", flush=True)
    t0 = time.time()
    for n, s in enumerate(todo, 1):
        cur = max(SINCE, done.get(s, 0) + 1)
        got = 0
        while True:
            rows = _get("fundingRate", {"symbol": s, "startTime": cur, "limit": 1000})
            time.sleep(0.65)
            if not rows:
                break
            c.executemany("INSERT OR REPLACE INTO fh5_funding VALUES (?,?,?)",
                          [(s, int(r["fundingTime"]), float(r["fundingRate"])) for r in rows])
            got += len(rows)
            cur = int(rows[-1]["fundingTime"]) + 1
            if len(rows) < 1000:
                break
        c.execute("INSERT OR REPLACE INTO fh5_done VALUES (?,?)", (s, now))
        c.commit()
        if n % 25 == 0:
            el = time.time() - t0
            print(f"  {n}/{len(todo)}  경과 {el/60:.1f}분 · 남은 {el/n*(len(todo)-n)/60:.0f}분", flush=True)
    c.close()


def events(c):
    return [(s, int(t) // M * M, float(r)) for s, t, r in
            c.execute("SELECT symbol, t, rate FROM fh5_funding WHERE ABS(rate) >= ? AND t >= ? ORDER BY t", (TH, SINCE))]


def collect_1m():
    c = conn()
    have = {(s, ft) for s, ft in c.execute("SELECT DISTINCT symbol, ft FROM fh5_1m")}
    ev = [(s, ft) for s, ft, _ in events(c) if (s, ft) not in have]
    print(f"받을 정산 {len(ev):,}건", flush=True)
    for n, (s, ft) in enumerate(ev, 1):
        rows = _get("klines", {"symbol": s, "interval": "1m", "startTime": ft - 2 * M, "endTime": ft + 12 * M,
                               "limit": 15})
        c.executemany("INSERT OR REPLACE INTO fh5_1m VALUES (?,?,?,?,?)",
                      [(s, ft, int(k[0]), float(k[1]), float(k[7])) for k in rows] or [(s, ft, 0, 0.0, 0.0)])
        if n % 200 == 0:
            c.commit()
            print(f"  {n:,}/{len(ev):,}", flush=True)
        time.sleep(0.06)
    c.commit()
    c.close()


def _stats(v):
    v = np.asarray(v)
    return v.mean(), 2 * v.std() / np.sqrt(len(v)) if len(v) > 1 else np.nan


def main():
    if "--collect-funding" in sys.argv:
        return collect_funding()
    if "--collect-1m" in sys.argv:
        return collect_1m()
    c = conn()
    w = defaultdict(dict)
    qv = {}
    for s, ft, t, o, q in c.execute("SELECT symbol, ft, t, open, qvol FROM fh5_1m WHERE t > 0"):
        w[(s, ft)][(t - ft) // M] = o
        if t - ft == ENTRY * M:
            qv[(s, ft)] = q
    nsym = c.execute("SELECT COUNT(DISTINCT symbol) FROM fh5_funding").fetchone()[0]
    ev = events(c)
    c.close()
    rows = []
    for s, ft, r in ev:
        x = w.get((s, ft))
        if not x or ENTRY not in x or EXIT not in x or x[ENTRY] <= 0:
            continue
        d = np.sign(r)                                                 # 내는 쪽: + 면 롱, − 면 숏
        rows.append(dict(s=s, ft=ft, r=r, net=d * (x[EXIT] / x[ENTRY] - 1) - COST,
                         net5=(d * (x[5] / x[ENTRY] - 1) - COST) if 5 in x else np.nan, qv=qv.get((s, ft), 0.0)))
    print(f"FH5 · 사전 고정 검증 · M 밖 {nsym}심볼 · |펀딩| ≥ {TH*100:g}% 정산 {len(ev):,}건 중 분봉 있는 {len(rows):,}건")
    print(f"   규칙: 내는 쪽 · T+{ENTRY}분 시가 → T+{EXIT}분 시가 · 비용 {COST*100:.2f}%   (값은 % · ±2se)")
    if not rows:
        return
    net = np.array([x["net"] for x in rows]) * 100
    m, se = _stats(net)
    by = defaultdict(list)
    for x in rows:
        by[x["s"]].append(x["net"] * 100)
    top = max(by, key=lambda k: len(by[k]))
    rest = np.array([x["net"] * 100 for x in rows if x["s"] != top])
    sym_med = np.median([np.mean(v) for v in by.values()])
    h0 = np.array([x["net"] * 100 for x in rows if x["ft"] < SPLIT])
    h1 = np.array([x["net"] * 100 for x in rows if x["ft"] >= SPLIT])
    ok1, ok2, ok3 = m - se > 0, rest.mean() > 0 and sym_med > 0, len(h0) > 0 and h0.mean() > 0 and h1.mean() > 0
    print(f"\n① 순평균 {m:+.3f} ±{se:.3f} (중앙 {np.median(net):+.3f} · 이익 {np.mean(net > 0):.0%} · {len(net)}건)"
          f"  → {'통과' if ok1 else '실패'}")
    print(f"② 최다 심볼 {top}({len(by[top])}건) 빼면 {rest.mean():+.3f} · 심볼별 순평균 중앙값 {sym_med:+.3f} "
          f"({len(by)}심볼)  → {'통과' if ok2 else '실패'}")
    print(f"③ 전반 {h0.mean() if len(h0) else float('nan'):+.3f} ({len(h0)}건) · 후반 {h1.mean():+.3f} ({len(h1)}건)"
          f"  → {'통과' if ok3 else '실패'}")
    print(f"\n판정: {'✅ 통과' if ok1 and ok2 and ok3 else '❌ 실패'}")

    print("\n— 참고(판정 아님) —")
    n5 = np.array([x["net5"] for x in rows if not np.isnan(x["net5"])]) * 100
    print(f"   T+5분 청산: {n5.mean():+.3f} ±{2*n5.std()/np.sqrt(len(n5)):.3f}")
    for sg, nm in ((-1, "펀딩 − (숏)"), (1, "펀딩 + (롱)")):
        v = [x["net"] * 100 for x in rows if np.sign(x["r"]) == sg]
        if len(v) >= 5:
            a, b = _stats(v)
            print(f"   {nm:12} {len(v):5d}건 {a:+.3f} ±{b:.3f}")
    for lo, hi, nm in ((0.005, 0.01, "0.5~1%"), (0.01, 0.0199, "1~2%"), (0.0199, 1, "2% 이상(상한)")):
        v = [x["net"] * 100 for x in rows if lo <= abs(x["r"]) < hi]
        if len(v) >= 5:
            a, b = _stats(v)
            print(f"   |펀딩| {nm:12} {len(v):5d}건 {a:+.3f} ±{b:.3f}")
    q = [x["qv"] for x in rows]
    for lo, hi, nm in ((0, 2e4, "<$20k"), (2e4, 1e5, "$20k~100k"), (1e5, 1e18, "≥$100k")):
        v = [x["net"] * 100 for x in rows if lo <= x["qv"] < hi]
        if len(v) >= 5:
            a, b = _stats(v)
            print(f"   진입 분봉 거래대금 {nm:10} {len(v):5d}건 {a:+.3f} ±{b:.3f}")
    print(f"   진입 분봉 거래대금 중앙 ${np.median(q):,.0f}")


if __name__ == "__main__":
    main()
