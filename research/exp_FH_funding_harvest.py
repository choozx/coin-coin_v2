"""FH — 펀딩 수확: 정산 직전에 '받는 쪽'으로 들어가 정산 직후 바로 나온다.

사용자 아이디어(2026-11-17): M 은 24시간 들고 있다가 가격 다리에서 졌다. 펀딩은 **정산 시각에 포지션이
있기만 하면** 주고받는다 → 정산을 몇 분만 걸치면 가격 노출을 거의 없애고 펀딩만 받을 수 있지 않나.
(정산 확인은 사용자 데이터 스트림 ACCOUNT_UPDATE 의 reason=FUNDING_FEE 로 받을 수 있다.)

반론(정직한 사전 확률: 낮음): 이건 누구나 아는 공짜 점심이라 시장이 막는다. 받는 쪽이 정산 직전에 몰리면
가격이 그쪽으로 밀리고(+ 내는 쪽은 직전에 빠져나가고), 직후 되돌린다 → **정산 전후 가격 변동이 펀딩을 상쇄**할
것이다. 게다가 매번 왕복 수수료(테이커 0.10%)를 낸다 — M 은 24시간에 세 번 받으며 회전 13% 만 냈다.
그래서 판정 대상은 하나다: **받는 쪽 기준 정산 전후 가격 손익 + 펀딩 − 비용 > 0 인가.**

데이터: 2024-01 이후 M 유니버스 56심볼에서 |펀딩| ≥ 0.05% 인 정산(실제 정산 시각 T, 4h·1h 주기 포함)마다
T−10분 ~ T+10분 1분봉(공개 API). 진입 = T+a 분 시가, 청산 = T+b 분 시가 (a<0≤b 라 T 에 보유 중).
받는 쪽 = 펀딩 양수면 숏, 음수면 롱.

    python3 -u -m research.exp_FH_funding_harvest --collect     # 1분봉 받기(~20분, 다시 돌리면 빠진 것만)
    python3 -u -m research.exp_FH_funding_harvest
    python3 -u -m research.exp_FH_funding_harvest --collect-agg   # |펀딩|≥0.2% 정산의 T−30초~T+90초 체결(아카이브)
    python3 -u -m research.exp_FH_funding_harvest --seconds       # 초 단위 경로

초 단위: API 의 aggTrades 시간 검색은 **최근 2일만** 된다 → data.binance.vision 일별 aggTrades zip 을 받아
정산 창만 남기고 버린다(437 파일 · ~2.3GB 내려받기, 저장은 창만).
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timezone

import numpy as np

from engine import candle_store as cs
from research.xs_data import _get
from research.xv_data import DB

M = 60_000
SINCE = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
TH = 0.0005
WIN = 10
ENTRIES = (-5, -1)          # 분 (T 기준)
EXITS = (0, 1, 5)           # 0 = T 분봉 시가(정산 직후 즉시)
COSTS = (("메이커 양쪽 0.04%", 0.0004), ("테이커 0.10%", 0.0010), ("테이커+슬리피지 0.20%", 0.0020))


def events():
    c = sqlite3.connect(cs.DB_PATH)
    syms = [r[0][:-3] for r in c.execute("SELECT DISTINCT symbol FROM candle") if r[0].endswith("USDT_1H")]
    q = ",".join("?" * len(syms))
    ev = c.execute(f"SELECT symbol, funding_time, rate FROM funding WHERE symbol IN ({q}) "
                   f"AND ABS(rate) >= ? AND funding_time >= ? ORDER BY funding_time", syms + [TH, SINCE]).fetchall()
    c.close()
    return [(s, int(t) // M * M, float(r)) for s, t, r in ev]


def conn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS fh_1m (symbol TEXT, ft INTEGER, t INTEGER, open REAL, close REAL, "
              "PRIMARY KEY (symbol, ft, t))")
    return c


def collect():
    c = conn()
    have = {(s, ft) for s, ft in c.execute("SELECT DISTINCT symbol, ft FROM fh_1m")}
    ev = [e for e in events() if (e[0], e[1]) not in have]
    print(f"받을 정산 {len(ev):,}건", flush=True)
    t0 = time.time()
    for n, (s, ft, _) in enumerate(ev, 1):
        try:
            rows = _get("klines", {"symbol": s, "interval": "1m", "startTime": ft - WIN * M,
                                   "endTime": ft + WIN * M, "limit": 2 * WIN + 1})
        except Exception as e:
            print(f"  {s} {ft} 실패 {e}", flush=True)
            continue
        c.executemany("INSERT OR REPLACE INTO fh_1m VALUES (?,?,?,?,?)",
                      [(s, ft, int(k[0]), float(k[1]), float(k[4])) for k in rows])
        if n % 200 == 0:
            c.commit()
            el = time.time() - t0
            print(f"  {n:,}/{len(ev):,}  경과 {el/60:.1f}분 · 남은 {el/n*(len(ev)-n)/60:.1f}분", flush=True)
        time.sleep(0.08)
    c.commit()
    c.close()


AGG_TH = 0.002
AGG_PRE, AGG_POST = 30_000, 90_000


def _agg_file(sym, day, fts):
    import csv
    import io
    import urllib.request
    import zipfile
    u = f"https://data.binance.vision/data/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip"
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "research-client/0.1"}),
                                        timeout=120) as r:
                raw = r.read()
            break
        except Exception:
            if i == 3:
                raise
            time.sleep(2 * (i + 1))
    out = []
    wins = [(ft, ft - AGG_PRE, ft + AGG_POST) for ft in fts]
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        with z.open(z.namelist()[0]) as f:
            for row in csv.reader(io.TextIOWrapper(f)):
                if not row or not row[0].isdigit():          # 머리줄
                    continue
                t = int(row[5])
                for ft, a, b in wins:
                    if a <= t <= b:
                        out.append((sym, ft, t, float(row[1]), float(row[2]), int(row[6] in ("true", "True"))))
    return out


def collect_agg():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS fh_agg (symbol TEXT, ft INTEGER, t INTEGER, price REAL, qty REAL, "
              "buyer_maker INTEGER)")
    c.execute("CREATE INDEX IF NOT EXISTS fh_agg_i ON fh_agg(symbol, ft)")
    have = {(s, ft) for s, ft in c.execute("SELECT DISTINCT symbol, ft FROM fh_agg")}
    by = {}
    for s, ft, r in events():
        if abs(r) >= AGG_TH and (s, ft) not in have:
            day = datetime.fromtimestamp(ft / 1000, timezone.utc).strftime("%Y-%m-%d")
            by.setdefault((s, day), []).append(ft)
    print(f"받을 파일 {len(by)}개", flush=True)
    t0, done = time.time(), 0
    with ThreadPoolExecutor(4) as ex:
        futs = {ex.submit(_agg_file, s, d, fts): (s, d) for (s, d), fts in by.items()}
        for f in as_completed(futs):
            s, d = futs[f]
            try:
                rows = f.result()
            except Exception as e:
                print(f"  {s} {d} 실패 {e}", flush=True)
                continue
            c.executemany("INSERT INTO fh_agg VALUES (?,?,?,?,?,?)", rows)
            c.commit()
            done += 1
            if done % 25 == 0:
                el = time.time() - t0
                print(f"  {done}/{len(by)}  경과 {el/60:.1f}분 · 남은 {el/done*(len(by)-done)/60:.1f}분", flush=True)
    c.close()


SEC_OFFS = (-5, -1, 0, 1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90)


def seconds():
    """초 단위 경로 — 받는 쪽 기준, T−10초 마지막 체결가 대비 T+s 초 마지막 체결가(그 시각까지의 마지막 체결)."""
    c = conn()
    tr = {}
    for s_, ft, t, p in c.execute("SELECT symbol, ft, t, price FROM fh_agg ORDER BY symbol, ft, t"):
        tr.setdefault((s_, ft), ([], []))
        tr[(s_, ft)][0].append(t); tr[(s_, ft)][1].append(p)
    c.close()
    ev = [(s_, ft, r) for s_, ft, r in events() if abs(r) >= AGG_TH and (s_, ft) in tr]
    print(f"FH 초 단위 · |펀딩| ≥ {AGG_TH*100:.1f}% 정산 {len(ev)}건 · 진입 = T−10초 마지막 체결가 · 받는 쪽 기준 가격 손익(%)")
    rows, delay, rate_pre, rate_post = [], [], [], []
    for s_, ft, r in ev:
        t = np.array(tr[(s_, ft)][0]); p = np.array(tr[(s_, ft)][1])
        def px(off):
            i = np.searchsorted(t, ft + off * 1000, side="right") - 1
            return p[i] if i >= 0 else np.nan
        p0 = px(-10)
        side = -np.sign(r)
        rows.append((abs(r), [side * (px(o) / p0 - 1) for o in SEC_OFFS]))
        after = t[t > ft]
        if len(after):
            delay.append((after[0] - ft) / 1000)
        rate_pre.append(((t >= ft - 30_000) & (t < ft)).sum() / 30)
        rate_post.append(((t > ft) & (t <= ft + 10_000)).sum() / 10)
    print(f"   정산 후 첫 체결까지: 중앙 {np.median(delay):.2f}초 · 90% {np.percentile(delay, 90):.1f}초 | "
          f"체결 건수/초 중앙: 직전 30초 {np.median(rate_pre):.1f} → 직후 10초 {np.median(rate_post):.1f}")
    head = " ".join(f"{('T' + format(o, '+d') + 's'):>7}" for o in SEC_OFFS)
    for lo, hi in ((0.002, 0.005), (0.005, 1.0)):
        sel = [(f, v) for f, v in rows if lo <= f < hi]
        if not sel:
            continue
        F = np.array([f for f, _ in sel]) * 100
        V = np.array([v for _, v in sel]) * 100
        print(f"\n■ |펀딩| {lo*100:.1f}~{min(hi, 9.99)*100:.1f}% · {len(sel)}건 · 평균 펀딩 {F.mean():+.3f}%")
        print(f"   {'':12} {head}")
        print(f"   {'가격 평균':10} " + " ".join(f"{x:+7.3f}" for x in np.nanmean(V, 0)))
        print(f"   {'가격 중앙':10} " + " ".join(f"{x:+7.3f}" for x in np.nanmedian(V, 0)))
        net = F[:, None] + V - 0.10
        print(f"   {'순(테이커)':9} " + " ".join(f"{x:+7.3f}" for x in np.nanmean(net, 0)))
        print(f"   {'순>0 비율':9} " + " ".join(f"{x:7.0%}" for x in np.nanmean(net > 0, 0)))


def main():
    if "--collect" in sys.argv:
        return collect()
    if "--collect-agg" in sys.argv:
        return collect_agg()
    if "--seconds" in sys.argv:
        return seconds()
    c = conn()
    win = {}
    for s, ft, t, o, cl in c.execute("SELECT symbol, ft, t, open, close FROM fh_1m"):
        win.setdefault((s, ft), {})[(t - ft) // M] = o
    c.close()
    ev = [e for e in events() if (e[0], e[1]) in win]
    print(f"FH · 펀딩 수확 · 2024-01~ · |펀딩| ≥ {TH*100:.2f}% 정산 {len(ev):,}건 (M 유니버스 56심볼)")
    bins = ((0.0005, 0.001), (0.001, 0.002), (0.002, 0.005), (0.005, 1.0))
    for a in ENTRIES:
        for b in EXITS:
            print(f"\n■ 진입 T{a:+d}분 · 청산 T{'+' if b >= 0 else ''}{b}분 (받는 쪽 기준, 건당 평균 %)")
            print(f"   {'|펀딩| 구간':14} {'건수':>5} {'펀딩':>7} {'가격':>8} {'가격 중앙':>8} | " +
                  " | ".join(f"순({n})" for n, _ in COSTS))
            for lo, hi in bins:
                f_, p_ = [], []
                for s, ft, r in ev:
                    w = win[(s, ft)]
                    if a in w and b in w and lo <= abs(r) < hi:
                        side = -np.sign(r)
                        p_.append(side * (w[b] / w[a] - 1)); f_.append(abs(r))
                if not f_:
                    continue
                f_, p_ = np.array(f_), np.array(p_)
                nets = [f"{(f_ + p_ - cost).mean()*100:+7.3f}%" for _, cost in COSTS]
                print(f"   {lo*100:.2f}~{min(hi,9.99)*100:.2f}%   {len(f_):5d} {f_.mean()*100:+6.3f}% {p_.mean()*100:+7.3f}% "
                      f"{np.median(p_)*100:+7.3f}% | " + " | ".join(nets))


if __name__ == "__main__":
    main()
