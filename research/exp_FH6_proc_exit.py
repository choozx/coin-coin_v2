"""FH6 — 처리 직후 예약 청산: 정산 직전 받는 쪽 진입 → 펀딩 처리(T+~0.3초) 직후 T+0.5초에 청산. 다른 심볼로 사전 고정 검증.

발견(사후, M 56심볼 fh_agg): 정산 후 하락은 한 번에 오지 않는다(하락 50% 도달 중앙 T+277ms). 받는 쪽 맨몸으로
T−10초 진입 → T+0.4~0.5초 청산(펀딩 포함, 비용 0.10% 후): |펀딩| 0.5~1% 후반 +0.25±0.06% · 1%+ +0.61±0.19%.
실측(fh_probe, 2026-11-17): 펀딩은 처리 순간(T+~260~390ms)에 들고 있어야 붙는다.

━━ 사전 고정(2026-11-18, 결과 보기 전에 작성 — 바꾸지 않는다) ━━
  표본   FH5 와 같은 M 밖 USDT 무기한 598심볼(폐지 포함) · 2024-01~ · |펀딩| ≥ 0.5% 정산(fh5_funding) 전부
         가격 = data.binance.vision 선물 aggTrades 의 '그 시각까지 마지막 체결가'
  규칙   받는 쪽(펀딩 + → 숏 · − → 롱) · 진입 T−10초 · 청산 **T+0.5초** · 펀딩은 붙는다고 가정(처리 ≤ T+0.39초 실측)
         순손익 = |펀딩| + 받는 쪽 기준 가격 수익률 − 비용 0.10%
  합격   ① 순평균 − 2×표준오차 > 0   ② 중앙값 > 0   ③ 2025-07 전·후 두 반쪽 모두 순평균 > 0
         ④ 최다 심볼을 빼도 순평균 > 0, 심볼별 순평균 중앙값 > 0          넷 다 통과해야 ✅.
  참고(판정 아님): 청산 T+0.4·0.7·1·2·5초 · |펀딩| 0.5~1% / 1~2% / 2%+ · 부호별 · 주기(1h·4h·8h)별 · 진입 직전 10초 체결 수(유동성).
  한계(측정 못 함 → 실서버 소액 측정으로): 처리 시각이 T+0.5초보다 늦은 정산(펀딩 없음) · 급락 중 시장가 체결 슬리피지.

데이터: 심볼-일 zip 수천 개(수 GB) — 창(T−15초 ~ T+6초)만 남기고 버린다. 다시 돌리면 이어서.

    python3 -u -m research.exp_FH6_proc_exit --collect
    python3 -u -m research.exp_FH6_proc_exit
"""
from __future__ import annotations

import csv
import io
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from research.exp_FH5_post_trend import TH, conn as fh5_conn, events as fh5_events
from research.xv_data import DB

SPLIT = int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
PRE, POST = 15_000, 6_000
ENTRY, EXIT, COST = -10.0, 0.5, 0.0010
EXITS = (0.4, 0.5, 0.7, 1.0, 2.0, 5.0)


def conn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS fh6_agg (symbol TEXT, ft INTEGER, t INTEGER, price REAL)")
    c.execute("CREATE INDEX IF NOT EXISTS fh6_agg_i ON fh6_agg(symbol, ft)")
    c.execute("CREATE TABLE IF NOT EXISTS fh6_done (symbol TEXT, day TEXT, n INTEGER, PRIMARY KEY (symbol, day))")
    # 규모 분석용(--collect-vol): 수량·방향까지
    c.execute("CREATE TABLE IF NOT EXISTS fh6_vol (symbol TEXT, ft INTEGER, t INTEGER, price REAL, qty REAL, buyer_maker INTEGER)")
    c.execute("CREATE INDEX IF NOT EXISTS fh6_vol_i ON fh6_vol(symbol, ft)")
    c.execute("CREATE TABLE IF NOT EXISTS fh6_vol_done (symbol TEXT, day TEXT, n INTEGER, PRIMARY KEY (symbol, day))")
    return c


def _file(sym, day, fts, full=False):
    u = f"https://data.binance.vision/data/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip"
    raw = None
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "research-client/0.1"}),
                                        timeout=300) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == 3:
                raise
        except Exception:
            if i == 3:
                raise
        time.sleep(3 * (i + 1))
    wins = sorted((ft - PRE, ft + POST, ft) for ft in fts)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        lines = z.read(z.namelist()[0]).split(b"\n")
    del raw
    start = 1 if lines and not lines[0][:1].isdigit() else 0          # 머리줄
    while lines and not lines[-1].strip():
        lines.pop()

    def tt(i):
        return int(lines[i].split(b",", 6)[5])

    def first_ge(t):                                                   # 체결은 시간순 → 이진 탐색
        lo, hi = start, len(lines)
        while lo < hi:
            mid = (lo + hi) // 2
            if tt(mid) < t:
                lo = mid + 1
            else:
                hi = mid
        return lo

    out = []
    for a, b, ft in wins:
        i = first_ge(a)
        while i < len(lines):
            f = lines[i].split(b",", 6)
            t = int(f[5])
            if t > b:
                break
            if full:
                out.append((sym, ft, t, float(f[1]), float(f[2]), int(f[6][:1] in (b"t", b"T"))))
            else:
                out.append((sym, ft, t, float(f[1])))
            i += 1
    return out


def collect(full=False):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    c = conn()
    tbl, dtbl = ("fh6_vol", "fh6_vol_done") if full else ("fh6_agg", "fh6_done")
    f5 = fh5_conn()
    done = {(s, d) for s, d in c.execute(f"SELECT symbol, day FROM {dtbl}")}
    by = defaultdict(list)
    for s, ft, r in fh5_events(f5):
        d = datetime.fromtimestamp(ft / 1000, timezone.utc).strftime("%Y-%m-%d")
        if (s, d) not in done:
            by[(s, d)].append(ft)
    f5.close()
    print(f"받을 파일 {len(by):,}개", flush=True)
    t0, n = time.time(), 0
    with ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(_file, s, d, fts, full): (s, d) for (s, d), fts in by.items()}
        for fu in as_completed(futs):
            s, d = futs[fu]
            try:
                rows = fu.result()
            except Exception as e:
                print(f"  {s} {d} 실패 {e}", flush=True)
                continue
            if rows:
                c.executemany(f"INSERT INTO {tbl} VALUES ({','.join('?' * len(rows[0]))})", rows)
            c.execute(f"INSERT OR REPLACE INTO {dtbl} VALUES (?,?,?)", (s, d, -1 if rows is None else len(rows)))
            c.commit()
            n += 1
            if n % 100 == 0:
                el = time.time() - t0
                print(f"  {n:,}/{len(by):,}  경과 {el/60:.1f}분 · 남은 {el/n*(len(by)-n)/60:.0f}분", flush=True)
    c.close()


def _last(ts, ps, t):
    i = np.searchsorted(ts, t, side="right") - 1
    return ps[i] if i >= 0 else np.nan


def main():
    if "--collect-vol" in sys.argv:
        return collect(full=True)
    if "--collect" in sys.argv:
        return collect()
    c = conn()
    tr = defaultdict(lambda: ([], []))
    for s, ft, t, p in c.execute("SELECT symbol, ft, t, price FROM fh6_agg ORDER BY symbol, ft, t"):
        tr[(s, ft)][0].append(t)
        tr[(s, ft)][1].append(p)
    missing = c.execute("SELECT COUNT(*) FROM fh6_done WHERE n < 0").fetchone()[0]
    c.close()
    f5 = fh5_conn()
    ev = fh5_events(f5)
    gap = {}
    last_t = {}
    for s, t, r in f5.execute("SELECT symbol, t, rate FROM fh5_funding ORDER BY symbol, t"):
        t = int(t) // 60_000 * 60_000
        if s in last_t:
            gap[(s, t)] = round((t - last_t[s]) / 3_600_000)
        last_t[s] = t
    f5.close()
    rows = []
    for s, ft, r in ev:
        if (s, ft) not in tr:
            continue
        ts, ps = np.array(tr[(s, ft)][0]), np.array(tr[(s, ft)][1])
        p0 = _last(ts, ps, ft + ENTRY * 1000)
        if not p0 > 0:
            continue
        d = -np.sign(r)
        rec = dict(s=s, ft=ft, r=r, gap=gap.get((s, ft), 0),
                   npre=int(((ts >= ft - 10_000) & (ts < ft)).sum()))
        for x in EXITS:
            p1 = _last(ts, ps, ft + x * 1000)
            rec[x] = abs(r) + d * (p1 / p0 - 1) - COST if p1 > 0 else np.nan
        rows.append(rec)
    print(f"FH6 · 사전 고정 검증 · |펀딩| ≥ {TH*100:g}% 정산 {len(ev):,}건 중 체결 데이터 있는 {len(rows):,}건 "
          f"(아카이브 없음 {missing}파일)")
    print(f"   규칙: 받는 쪽 · T{ENTRY:+g}초 진입 · T+{EXIT}초 청산 · 펀딩 포함 · 비용 {COST*100:.2f}%   (값은 % · ±2se)")
    v = np.array([x[EXIT] for x in rows]) * 100
    m, se = v.mean(), 2 * v.std() / np.sqrt(len(v))
    h0 = np.array([x[EXIT] for x in rows if x["ft"] < SPLIT]) * 100
    h1 = np.array([x[EXIT] for x in rows if x["ft"] >= SPLIT]) * 100
    by = defaultdict(list)
    for x in rows:
        by[x["s"]].append(x[EXIT] * 100)
    top = max(by, key=lambda k: len(by[k]))
    rest = np.array([x[EXIT] * 100 for x in rows if x["s"] != top])
    smed = np.median([np.mean(a) for a in by.values()])
    ok = [m - se > 0, np.median(v) > 0, len(h0) > 0 and h0.mean() > 0 and h1.mean() > 0, rest.mean() > 0 and smed > 0]
    print(f"\n① 순평균 {m:+.3f} ±{se:.3f}  → {'통과' if ok[0] else '실패'}")
    print(f"② 중앙값 {np.median(v):+.3f} (이익 {np.mean(v > 0):.0%})  → {'통과' if ok[1] else '실패'}")
    print(f"③ 전반 {h0.mean():+.3f} ({len(h0)}건) · 후반 {h1.mean():+.3f} ({len(h1)}건)  → {'통과' if ok[2] else '실패'}")
    print(f"④ 최다 {top}({len(by[top])}건) 빼면 {rest.mean():+.3f} · 심볼 중앙 {smed:+.3f} ({len(by)}심볼)  → "
          f"{'통과' if ok[3] else '실패'}")
    print(f"판정: {'✅ 통과' if all(ok) else '❌ 실패'}")

    print("\n— 참고(판정 아님) —")
    print("   청산 시각별: " + " · ".join(
        f"T+{x}s {np.nanmean([r[x] for r in rows])*100:+.3f}" for x in EXITS))
    for lo, hi, nm in ((0.005, 0.01, "0.5~1%"), (0.01, 0.0199, "1~2%"), (0.0199, 1, "2%+")):
        a = np.array([x[EXIT] for x in rows if lo <= abs(x["r"]) < hi]) * 100
        if len(a) > 5:
            print(f"   |펀딩| {nm:7} {len(a):5d}건 {a.mean():+.3f} ±{2*a.std()/np.sqrt(len(a)):.3f} · 중앙 {np.median(a):+.3f}")
    for sg, nm in ((-1, "펀딩 − (롱)"), (1, "펀딩 + (숏)")):
        a = np.array([x[EXIT] for x in rows if np.sign(x["r"]) == sg]) * 100
        if len(a) > 5:
            print(f"   {nm:10} {len(a):5d}건 {a.mean():+.3f} ±{2*a.std()/np.sqrt(len(a)):.3f}")
    for g, nm in ((1, "1h 주기"), (4, "4h"), (8, "8h")):
        a = np.array([x[EXIT] for x in rows if (x["gap"] <= 2 if g == 1 else (3 <= x["gap"] <= 5 if g == 4 else x["gap"] >= 6))]) * 100
        if len(a) > 5:
            print(f"   {nm:10} {len(a):5d}건 {a.mean():+.3f} ±{2*a.std()/np.sqrt(len(a)):.3f}")
    q = np.quantile([x["npre"] for x in rows], [1 / 3, 2 / 3])
    for lo, hi, nm in ((0, q[0], f"직전10초 체결 <{q[0]:.0f}"), (q[0], q[1], "중간"), (q[1], 1e9, f"≥{q[1]:.0f}")):
        a = np.array([x[EXIT] for x in rows if lo <= x["npre"] < hi]) * 100
        if len(a) > 5:
            print(f"   {nm:16} {len(a):5d}건 {a.mean():+.3f} ±{2*a.std()/np.sqrt(len(a)):.3f}")


if __name__ == "__main__":
    main()
