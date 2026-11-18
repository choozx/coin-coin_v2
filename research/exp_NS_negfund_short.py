"""NS — 펀딩 음수 알트 숏(펀딩을 내면서 하락 추세를 탄다).

사용자 관찰(2026-11-18, fh_proc_watch 5시간): 펀딩 음수 알트 롱 4개 중 SOL 빼고 전부 가격 손실이 펀딩 수취보다 컸다.
질문: 그럼 반대로 '펀딩 음수 코인 숏'(펀딩은 낸다)이 이기나? M 의 롱 다리 가격은 2021-24 +184·+156%p, 2025-26 −58%p 로
구간마다 뒤집혔다 → 최근 구조에서만 성립하는지, 꼬리(숏 스퀴즈)는 얼마나 되는지 본다.

사전 고정(결과 보기 전 작성):
  유니버스  바이낸스 USDT 무기한 전부(폐지 포함은 API 한계로 현재 상장분만 — 생존 편향: 폐지 코인은 대개 하락 → 숏에 불리하게 빠짐)
  기간      2025-01-01 ~ 2026-11-17 · 전반 2025-01~2025-12 / 후반 2026-01~
  신호      정산 T 에 펀딩률 ≤ −0.10%(그 회차 값, 주기 무관)
  진입      T 다음 1h 봉 시가 숏(정시 정산이면 T+1h — 그 회차 펀딩은 내지 않고, 정산 직후 1분 하락도 안 먹는다 = 보수적)
  보유      8h · 24h · 72h (각각 따로)
  청산      진입 + 보유시간의 1h 봉 시가
  손익      −(청산가/진입가 − 1) − Σ 보유 중 펀딩률(숏은 음수 펀딩을 낸다 = −rate 가 아니라 +rate 를 더함) − 0.10%
  중복      같은 심볼은 보유 중 새 신호 무시
  합격      ① 평균 − 2·se > 0 ② 중앙 > 0 ③ 전반·후반 모두 평균 > 0 ④ 최다 기여 심볼 빼도 평균 > 0
  꼬리      손실 > 20% · > 50% 비율, 최악 5건을 함께 보고

    .venv/bin/python -u research/exp_NS_negfund_short.py --collect   # 펀딩·1h 봉 수집(data/ns.db, 다시 돌리면 빠진 것만)
    .venv/bin/python -u research/exp_NS_negfund_short.py             # 판정
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "ns.db")
FAPI = "https://fapi.binance.com"
START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
END = int(datetime(2026, 11, 17, tzinfo=timezone.utc).timestamp() * 1000)
SPLIT = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
H = 3_600_000
TH = -0.0010
HOLDS = (8, 24, 72)
FEE = 0.0010


def get(path, params, retries=6):
    q = urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            req = urllib.request.Request(f"{FAPI}{path}?{q}", headers={"User-Agent": "research-client/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
                time.sleep(30 * (i + 1))
                continue
            if e.code == 400:
                return []
            if i == retries - 1:
                raise
        except urllib.error.URLError:
            if i == retries - 1:
                raise
        time.sleep(2 * (i + 1))


def conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS fr (symbol TEXT, t INTEGER, rate REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS k1h (symbol TEXT, t INTEGER, open REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS done (symbol TEXT, kind TEXT, PRIMARY KEY (symbol, kind))")
    return c


def collect():
    c = conn()
    syms = [s["symbol"] for s in get("/fapi/v1/exchangeInfo", {})["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"]
    done = {(s, k) for s, k in c.execute("SELECT symbol, kind FROM done")}
    print(f"심볼 {len(syms)}개", flush=True)
    for i, s in enumerate(syms):
        if (s, "fr") not in done:                         # fundingRate: 500회/5분 제한 → 0.7초 간격
            st, rows = START, []
            while st < END:
                r = get("/fapi/v1/fundingRate", dict(symbol=s, startTime=st, endTime=END, limit=1000))
                time.sleep(0.7)
                if not r:
                    break
                rows += [(s, int(x["fundingTime"]), float(x["fundingRate"])) for x in r]
                if len(r) < 1000:
                    break
                st = int(r[-1]["fundingTime"]) + 1
            c.executemany("INSERT OR IGNORE INTO fr VALUES (?,?,?)", rows)
            c.execute("INSERT OR IGNORE INTO done VALUES (?, 'fr')", (s,))
            c.commit()
            done.add((s, "fr"))
        has_sig = c.execute("SELECT 1 FROM fr WHERE symbol=? AND rate<=? LIMIT 1", (s, TH)).fetchone()
        if has_sig and (s, "k") not in done:              # 신호가 있는 심볼만 1h 봉
            st, rows = START, []
            while st < END + 80 * H:
                r = get("/fapi/v1/klines", dict(symbol=s, interval="1h", startTime=st, limit=1500))
                time.sleep(0.25)
                if not r:
                    break
                rows += [(s, int(x[0]), float(x[1])) for x in r]
                if len(r) < 1500:
                    break
                st = int(r[-1][0]) + H
            c.executemany("INSERT OR IGNORE INTO k1h VALUES (?,?,?)", rows)
            c.execute("INSERT OR IGNORE INTO done VALUES (?, 'k')", (s,))
            c.commit()
        if i % 50 == 0:
            print(f"  {i}/{len(syms)} {s}", flush=True)
    print("수집 끝", flush=True)


def trades(c, hold):
    fr = defaultdict(list)
    for s, t, r in c.execute("SELECT symbol, t, rate FROM fr ORDER BY symbol, t"):
        fr[s].append((t, r))
    out = []
    for s, rows in fr.items():
        k = dict(c.execute("SELECT t, open FROM k1h WHERE symbol=?", (s,)).fetchall())
        if not k:
            continue
        ts = np.array([t for t, _ in rows])
        rs = np.array([r for _, r in rows])
        busy_until = 0
        for t, r in rows:
            if r > TH or t < START or t >= END or t < busy_until:
                continue
            e = (t // H + 1) * H                          # 다음 1h 봉 시가 진입
            x = e + hold * H
            if e not in k or x not in k or k[e] <= 0:
                continue
            price = -(k[x] / k[e] - 1)                    # 숏 가격 손익
            m = (ts > e) & (ts <= x)
            fund = float(rs[m].sum())                     # 숏: rate 가 음수면 낸다 → 손익에 rate 그대로 더함
            out.append(dict(symbol=s, t=e, price=price, fund=fund, net=price + fund - FEE, sig=r))
            busy_until = x
    return out


def stats(v):
    v = np.asarray(v)
    if len(v) == 0:
        return "0건"
    se = v.std(ddof=1) / np.sqrt(len(v)) if len(v) > 1 else float("nan")
    return f"{len(v):5d}건 평균 {v.mean()*100:+.2f}% ±{se*100:.2f} · 중앙 {np.median(v)*100:+.2f}% · 이익 {np.mean(v > 0):.0%}"


def judge():
    c = conn()
    n_sym = c.execute("SELECT COUNT(DISTINCT symbol) FROM fr").fetchone()[0]
    print(f"데이터: 펀딩 {n_sym}심볼 · 신호 기준 펀딩 ≤ {TH*100:.2f}% · 비용 {FEE*100:.2f}%\n")
    for hold in HOLDS:
        T = trades(c, hold)
        if not T:
            print(f"{hold}h: 거래 없음")
            continue
        net = np.array([x["net"] for x in T])
        se = net.std(ddof=1) / np.sqrt(len(net))
        a = [x["net"] for x in T if x["t"] < SPLIT]
        b = [x["net"] for x in T if x["t"] >= SPLIT]
        by = defaultdict(float)
        for x in T:
            by[x["symbol"]] += x["net"]
        top = max(by, key=lambda s: by[s])
        wo = [x["net"] for x in T if x["symbol"] != top]
        ok = [net.mean() - 2 * se > 0, np.median(net) > 0, bool(a) and np.mean(a) > 0 and bool(b) and np.mean(b) > 0,
              np.mean(wo) > 0]
        print(f"■ 보유 {hold}h — {'✅ 합격' if all(ok) else '❌'} (①{'○' if ok[0] else '×'} ②{'○' if ok[1] else '×'} "
              f"③{'○' if ok[2] else '×'} ④{'○' if ok[3] else '×'})")
        print(f"   순      {stats(net)}")
        print(f"   가격    {stats([x['price'] for x in T])}")
        print(f"   펀딩    {stats([x['fund'] for x in T])}")
        print(f"   전반    {stats(a)}")
        print(f"   후반    {stats(b)}")
        print(f"   최다 기여 {top} {by[top]*100:+.0f}%p 빼면 {stats(wo)}")
        print(f"   꼬리    손실>20% {np.mean(net < -0.2):.1%} · >50% {np.mean(net < -0.5):.1%} · 최악 "
              + ", ".join(f"{x['symbol']} {x['net']*100:+.0f}%" for x in sorted(T, key=lambda x: x["net"])[:5]))
        for lo, hi in ((-0.003, -0.001), (-0.01, -0.003), (-1, -0.01)):
            v = [x["net"] for x in T if lo < x["sig"] <= hi]
            print(f"   신호 {hi*100:+.2f}~{lo*100:+.2f}%  {stats(v)}")
        print()


if __name__ == "__main__":
    if "--collect" in sys.argv:
        collect()
    else:
        judge()
