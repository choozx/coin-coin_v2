"""FH3 — 펀딩 수확 + 현물 반대 헤지: 정산 하락이 선물에만 있나, 현물도 같이 빠지나.

FH 실측: 펀딩은 처리 순간(T+~260~390ms)에 들고 있어야 붙고 가격이 바로 그 순간 펀딩만큼 반대로 간다.
남은 변형: 받는 쪽 선물 + **같은 코인 현물 반대 방향**(펀딩 − → 선물 롱 + 현물 숏(마진 차입),
펀딩 + → 선물 숏 + 현물 롱). 가격 방향 위험은 지워지고 남는 건

    순손익 = |펀딩| + 받는 쪽 기준 (선물 수익률 − 현물 수익률) − 비용      (= 펀딩 + 베이시스 변화)

현물도 같이 빠지면(모두가 같이 팔기) 헤지가 하락을 지워 펀딩이 남는다. 선물만 빠지면(정산 직후 선물 가격이
'다음 펀딩 기대'만큼 재조정) 베이시스가 펀딩만큼 움직여 0이 된다. 사전 확률: 후자 — 선물 가격엔 다가올 펀딩이
이미 녹아 있어서(펀딩 − 면 선물이 현물보다 싸다) 정산이 지나면 그만큼 선물만 되돌아갈 것.

비용(왕복): 선물 테이커 0.05%×2 + 현물 테이커 0.10%×2 = 0.30% (BNB 할인 현물 0.075% · 메이커 혼합으로 낮춰도 ~0.15%).
현물 숏은 마진 차입이 필요하다(몇 분이면 이자는 무시, 대신 차입 가능 수량이 문제).

데이터
  분 단위: FH 의 fh_1m(선물, M 유니버스 56심볼 · |펀딩| ≥ 0.05% · 2024-01~ · 7,087건) + 같은 창 현물 1분봉(공개 API).
  초 단위: FH 의 fh_agg(선물 aggTrades, |펀딩| ≥ 0.2%) + 같은 창 현물 aggTrades(data.binance.vision 일별 zip).
           현물 아카이브는 2025-01 부터 시각이 마이크로초 → 밀리초로 맞춘다.

    python3 -u -m research.exp_FH3_spot_hedge --collect       # 현물 1분봉(다시 돌리면 빠진 것만)
    python3 -u -m research.exp_FH3_spot_hedge --collect-agg   # 현물 aggTrades 창
    python3 -u -m research.exp_FH3_spot_hedge                 # 판정
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np

from research.exp_FH_funding_harvest import AGG_POST, AGG_PRE, conn, events
from research.exp_FH2_traits import SPLIT

M = 60_000
SAPI = "https://api.binance.com/api/v3"
COSTS = (("0.15% (메이커 혼합·BNB 할인)", 0.0015), ("0.30% (양쪽 테이커)", 0.0030))


def _sget(path, params, retries=4):
    q = urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            req = urllib.request.Request(f"{SAPI}/{path}?{q}", headers={"User-Agent": "research-client/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return None                                   # 현물 없음(상장 전·폐지)
            if i == retries - 1:
                raise
        except urllib.error.URLError:
            if i == retries - 1:
                raise
        time.sleep(1.5 * (i + 1))


def sconn():
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS fh_spot_1m (symbol TEXT, ft INTEGER, t INTEGER, open REAL, "
              "PRIMARY KEY (symbol, ft, t))")
    c.execute("CREATE TABLE IF NOT EXISTS fh_spot_done (symbol TEXT, ft INTEGER, kind TEXT, n INTEGER, "
              "PRIMARY KEY (symbol, ft, kind))")
    c.execute("CREATE TABLE IF NOT EXISTS fh_spot_agg (symbol TEXT, ft INTEGER, t INTEGER, price REAL)")
    c.execute("CREATE INDEX IF NOT EXISTS fh_spot_agg_i ON fh_spot_agg(symbol, ft)")
    return c


def collect():
    c = sconn()
    done = {(s, ft) for s, ft in c.execute("SELECT symbol, ft FROM fh_spot_done WHERE kind='1m'")}
    ev = [(s, ft) for s, ft in c.execute("SELECT DISTINCT symbol, ft FROM fh_1m") if (s, ft) not in done]
    print(f"받을 정산 {len(ev):,}건", flush=True)
    t0 = time.time()
    for n, (s, ft) in enumerate(ev, 1):
        rows = _sget("klines", {"symbol": s, "interval": "1m", "startTime": ft - 10 * M,
                                "endTime": ft + 10 * M, "limit": 21}) or []
        c.executemany("INSERT OR REPLACE INTO fh_spot_1m VALUES (?,?,?,?)",
                      [(s, ft, int(k[0]), float(k[1])) for k in rows])
        c.execute("INSERT OR REPLACE INTO fh_spot_done VALUES (?,?,?,?)", (s, ft, "1m", len(rows)))
        if n % 300 == 0:
            c.commit()
            el = time.time() - t0
            print(f"  {n:,}/{len(ev):,}  경과 {el/60:.1f}분 · 남은 {el/n*(len(ev)-n)/60:.1f}분", flush=True)
        time.sleep(0.05)
    c.commit()
    c.close()


def _spot_agg_file(sym, day, fts):
    import csv
    import io
    import zipfile
    u = f"https://data.binance.vision/data/spot/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip"
    raw = None
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "research-client/0.1"}),
                                        timeout=180) as r:
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
        time.sleep(2 * (i + 1))
    out = []
    wins = [(ft, ft - AGG_PRE, ft + AGG_POST) for ft in fts]
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        with z.open(z.namelist()[0]) as f:
            for row in csv.reader(io.TextIOWrapper(f)):
                if not row or not row[0].isdigit():
                    continue
                t = int(row[5])
                if t > 10 ** 14:                                  # 2025-01~ 마이크로초
                    t //= 1000
                for ft, a, b in wins:
                    if a <= t <= b:
                        out.append((sym, ft, t, float(row[1])))
    return out


def collect_agg():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    c = sconn()
    done = {(s, ft) for s, ft in c.execute("SELECT symbol, ft FROM fh_spot_done WHERE kind='agg'")}
    by = {}
    for s, ft in c.execute("SELECT DISTINCT symbol, ft FROM fh_agg"):
        if (s, ft) not in done:
            day = datetime.fromtimestamp(ft / 1000, timezone.utc).strftime("%Y-%m-%d")
            by.setdefault((s, day), []).append(ft)
    print(f"받을 파일 {len(by)}개", flush=True)
    t0, n = time.time(), 0
    with ThreadPoolExecutor(4) as ex:
        futs = {ex.submit(_spot_agg_file, s, d, fts): (s, d, fts) for (s, d), fts in by.items()}
        for f in as_completed(futs):
            s, d, fts = futs[f]
            try:
                rows = f.result()
            except Exception as e:
                print(f"  {s} {d} 실패 {e}", flush=True)
                continue
            if rows:
                c.executemany("INSERT INTO fh_spot_agg VALUES (?,?,?,?)", rows)
            for ft in fts:
                cnt = sum(1 for r in rows if r[1] == ft) if rows else -1      # −1 = 현물 파일 없음
                c.execute("INSERT OR REPLACE INTO fh_spot_done VALUES (?,?,?,?)", (s, ft, "agg", cnt))
            c.commit()
            n += 1
            if n % 25 == 0:
                el = time.time() - t0
                print(f"  {n}/{len(by)}  경과 {el/60:.1f}분 · 남은 {el/n*(len(by)-n)/60:.1f}분", flush=True)
    c.close()


def _fund():
    return {(s, ft): r for s, ft, r in events()}


def minutes():
    c = sconn()
    fut, spot = {}, {}
    for s, ft, t, o in c.execute("SELECT symbol, ft, t, open FROM fh_1m"):
        fut.setdefault((s, ft), {})[(t - ft) // M] = o
    for s, ft, t, o in c.execute("SELECT symbol, ft, t, open FROM fh_spot_1m"):
        spot.setdefault((s, ft), {})[(t - ft) // M] = o
    c.close()
    fund = _fund()
    pairs = ((-1, 0), (-1, 1), (-1, 5), (-5, 5), (-1, 10))
    rows = []
    for key, r in fund.items():
        f, sp = fut.get(key), spot.get(key)
        if not f or not sp:
            continue
        d = -np.sign(r)
        rec = dict(ft=key[1], f=abs(r), sign=int(np.sign(r)))
        for a, b in pairs:
            if a in f and b in f and a in sp and b in sp:
                fr, sr = f[b] / f[a] - 1, sp[b] / sp[a] - 1
                rec[(a, b)] = (d * fr, d * sr)                   # 받는 쪽 기준 선물·현물 수익률
        rows.append(rec)
    n_sp = len({k for k in spot})
    print(f"\n━━ 분 단위 (1분봉 시가) · 선물 {len(fut):,}건 중 현물 있는 {n_sp:,}건 ━━")
    print("   받는 쪽 기준 % · 선물 = 맨몸 가격 손익 · 현물 = 반대 다리 전 현물 수익률 · 헤지 = 펀딩 + 선물 − 현물 (비용 전)")
    bins = [(0.0005, 0.001, "0.05~0.1%"), (0.001, 0.002, "0.1~0.2%"), (0.002, 0.005, "0.2~0.5%"),
            (0.005, 0.0199, "0.5~2%"), (0.0199, 1, "상한 2%")]
    for a, b in pairs:
        print(f"\n■ 진입 T{a:+d}분 → 청산 T{b:+d}분   [전반 2024-01~2025-06 | 후반 2025-07~]")
        print(f"   {'|펀딩|':10} {'반쪽':4} {'건수':>5} {'펀딩':>7} {'선물':>8} {'현물':>8} {'헤지 합':>8} "
              f"{'±2se':>6} {'후 0.15%':>9} {'후 0.30%':>9}")
        for lo, hi, nm in bins:
            for half in (0, 1):
                sel = [x for x in rows if (a, b) in x and lo <= x["f"] < hi and (x["ft"] >= SPLIT) == bool(half)]
                if len(sel) < 10:
                    continue
                F = np.array([x["f"] for x in sel]) * 100
                FR = np.array([x[(a, b)][0] for x in sel]) * 100
                SR = np.array([x[(a, b)][1] for x in sel]) * 100
                H = F + FR - SR
                se = H.std() / np.sqrt(len(H))
                print(f"   {nm:10} {'전반' if half == 0 else '후반':4} {len(sel):5d} {F.mean():+7.3f} {FR.mean():+8.3f} "
                      f"{SR.mean():+8.3f} {H.mean():+8.3f} {2*se:6.3f} {H.mean()-0.15:+9.3f} {H.mean()-0.30:+9.3f}")
    return rows


SEC_OFFS = (-10, -1, 0, 0.3, 0.5, 1, 2, 5, 10, 30, 60, 90)


def _last(ts, ps, t):
    i = np.searchsorted(ts, t, side="right") - 1
    return ps[i] if i >= 0 else np.nan


def seconds():
    c = sconn()
    fut, spot = {}, {}
    for s, ft, t, p in c.execute("SELECT symbol, ft, t, price FROM fh_agg ORDER BY symbol, ft, t"):
        fut.setdefault((s, ft), ([], []));  fut[(s, ft)][0].append(t);  fut[(s, ft)][1].append(p)
    for s, ft, t, p in c.execute("SELECT symbol, ft, t, price FROM fh_spot_agg ORDER BY symbol, ft, t"):
        spot.setdefault((s, ft), ([], []));  spot[(s, ft)][0].append(t);  spot[(s, ft)][1].append(p)
    missing = c.execute("SELECT COUNT(*) FROM fh_spot_done WHERE kind='agg' AND n<=0").fetchone()[0]
    c.close()
    fund = _fund()
    base = -25.0                                                   # 진입 기준 T−25초(창 시작 T−30초, 체결 여유)
    res = {o: [] for o in SEC_OFFS}
    for key, (ft_, fp) in fut.items():
        if key not in spot or key not in fund:
            continue
        r = fund[key]; d = -np.sign(r); ft = key[1]
        ft_a, fp_a = np.array(ft_), np.array(fp)
        st_a, sp_a = np.array(spot[key][0]), np.array(spot[key][1])
        f0, s0 = _last(ft_a, fp_a, ft + base * 1000), _last(st_a, sp_a, ft + base * 1000)
        if not (f0 > 0 and s0 > 0):
            continue
        for o in SEC_OFFS:
            f1, s1 = _last(ft_a, fp_a, ft + o * 1000), _last(st_a, sp_a, ft + o * 1000)
            if f1 > 0 and s1 > 0:
                res[o].append((ft, abs(r), d * (f1 / f0 - 1), d * (s1 / s0 - 1)))
    n = len(res[90])
    print(f"\n━━ 초 단위 (aggTrades 마지막 체결가) · |펀딩| ≥ 0.2% · 선물·현물 둘 다 있는 {n}건 (현물 없음 {missing}건) ━━")
    print("   진입 T−25초 기준 · 받는 쪽 기준 % · 펀딩은 T+0.3초 이후부터 붙는 것으로(실측 T+~260~390ms)")
    print(f"   {'시각':>7} {'건수':>5} {'선물':>8} {'현물':>8} {'선물−현물':>9} {'헤지 합':>8} {'±2se':>6} "
          f"{'후 0.30%':>9} {'이익비율':>7} | {'전반 헤지':>9} {'후반 헤지':>9}")
    for o in SEC_OFFS:
        v = res[o]
        if not v:
            continue
        t_ = np.array([x[0] for x in v]); F = np.array([x[1] for x in v]) * 100
        FR = np.array([x[2] for x in v]) * 100; SR = np.array([x[3] for x in v]) * 100
        H = (F if o >= 0.3 else 0) + FR - SR
        se = H.std() / np.sqrt(len(H))
        h0, h1 = H[t_ < SPLIT], H[t_ >= SPLIT]
        print(f"   T{o:+6g}s {len(v):5d} {FR.mean():+8.3f} {SR.mean():+8.3f} {(FR-SR).mean():+9.3f} {H.mean():+8.3f} "
              f"{2*se:6.3f} {H.mean()-0.30:+9.3f} {np.mean(H - 0.30 > 0):7.0%} | "
              f"{(h0.mean() if len(h0) else np.nan):+9.3f} {(h1.mean() if len(h1) else np.nan):+9.3f}")
    # 펀딩 크기별 T+60초
    v = res[60]
    print("\n■ T+60초 청산 · |펀딩| 크기별 (헤지 합 = 펀딩 + 선물 − 현물, 비용 전)")
    for lo, hi, nm in ((0.002, 0.003, "0.2~0.3%"), (0.003, 0.005, "0.3~0.5%"), (0.005, 0.01, "0.5~1%"),
                       (0.01, 0.0199, "1~2%"), (0.0199, 1, "상한 2%")):
        sel = [x for x in v if lo <= x[1] < hi]
        if len(sel) < 5:
            continue
        F = np.array([x[1] for x in sel]) * 100; B = np.array([x[2] - x[3] for x in sel]) * 100
        H = F + B
        print(f"   {nm:9} {len(sel):4d}건  펀딩 {F.mean():+6.3f}  선물−현물 {B.mean():+7.3f}  헤지 합 {H.mean():+7.3f}"
              f" ±{2*H.std()/np.sqrt(len(H)):.3f}  후 0.30% {H.mean()-0.30:+7.3f}")


def main():
    if "--collect" in sys.argv:
        collect()
    elif "--collect-agg" in sys.argv:
        collect_agg()
    else:
        minutes()
        seconds()


if __name__ == "__main__":
    main()
