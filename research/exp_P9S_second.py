"""P9S — 9시(00:00 UTC) 펌핑을 '초 단위'로 감지해 따라타기 (사용자 지적 "초단위로 봐야 하는데?", 2026-11-19).

P9(1분봉)는 감지가 최소 1분 늦다 → 처음 몇 초의 급등을 못 탄다. 여기선 체결(aggTrades) 단위로 본다.

━━ 사전 고정 (2026-11-19 작성 · 결과 보기 전) ━━
후보    USDT 무기한(xs_sym 654, 폐지 포함) · 2025-07 ~ 2026-10 · 매일 00:00 UTC.
        1분봉 고가로 고른다(종가 아님 — 몇 초 올랐다 꺼진 '속는 신호'까지 포함해야 편향이 없다):
        첫 3분(00:00~00:02) 고가 최대 / 기준가 − 1 ≥ +2%  그리고  00:00 1분 거래대금 ≥ $5만.
        기준가 = 23:59 1분봉 종가(00:00 직전 마지막 가격).
감지    00:00:00 부터 S초 안에 처음으로 체결가 ≥ 기준가 × (1+θ) 인 순간.
진입    감지 + 0.5초(주문 지연) 뒤 첫 체결가.   청산  진입 + M 시점 직전 마지막 체결가.
비용    왕복 0.20%(수수료 0.05×2 + 급등 중 슬리피지 0.05×2).
격자    θ ∈ {+2%, +4%} · S ∈ {5초, 20초, 60초} · M ∈ {30초, 2분, 10분} · 롱(따라타기)/숏(되돌림) = 36.
판정    t 는 날짜 묶음. ① 36개 중 최고 t ≥ 3.0  ② 그 조합 25H2 · 26H1 · 26H2(7~10월) 모두 > 0
        ③ 최다 심볼 빼도 · 최다 날짜 빼도 > 0 · 심볼별 평균의 중앙 > 0.
참고(판정 아님): 지연 0초·2초 · 감지 순간 체결가로 진입(지연 0, 슬리피지 무시 상한).

━━ 정정 v2 (2026-11-19, 위 규칙의 결과를 본 뒤 — 판정 규칙의 누설·낙관 두 가지를 고침, 원 결과도 BACKLOG 에 남김) ━━
  누설  후보 조건 '00:00 1분 거래대금 ≥ $5만'은 진입(수 초) 뒤 거래까지 포함 → 계속 오른 날이 더 잘 뽑힌다.
        → 후보는 거래대금 조건 없이(첫 3분 고가 +2%↑만), 유동성은 진입 전 정보로: 직전 1시간(23:00~23:59) 거래대금 ≥ $50만.
  낙관  아무 체결가로 진입·청산 → 시장가 매수는 매도호가, 매도는 매수호가에 체결된다(급등 중 호가 차 큼).
        → 진입 = 지연 뒤 첫 '매수 주도' 체결가(is_buyer_maker=false), 청산 = 그 시점 직전 마지막 '매도 주도' 체결가. 비용 0.20% 는 그대로 추가.
  격자·판정 ①~③ 은 원래대로. 참고: 유동성 조건 없음 · 지연 0/0.5/2초 · 상위 1%/5% 거래 빼기.

    python3 -u -m research.exp_P9S_second --cand2    # v2 후보(거래대금 조건 없음, 직전 1시간 거래대금 기록)
    python3 -u -m research.exp_P9S_second --agg2     # v2 체결(가격·수량·매수/매도 주도)
    python3 -u -m research.exp_P9S_second --v2       # v2 판정
    python3 -u -m research.exp_P9S_second --cand     # 1분봉 다시 훑어 후보(고가 기준)
    python3 -u -m research.exp_P9S_second --agg      # 후보 날만 aggTrades 앞 12분
    python3 -u -m research.exp_P9S_second            # 판정
"""
from __future__ import annotations

import io
import sqlite3
import sys
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np

from research.exp_P9_hour_pump import HALVES, MONTHS, _ms
from research.xv_data import DB

MIN, D = 60_000, 86_400_000
WIN = 12 * MIN
THS, SS, MS = (0.02, 0.04), (5_000, 20_000, 60_000), (30_000, 120_000, 600_000)
COST, LAT = 0.0020, 500


def conn():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("CREATE TABLE IF NOT EXISTS p9s_cand (symbol TEXT, t INTEGER, ref REAL, hi3 REAL, q0 REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS p9s_cdone (symbol TEXT, ym TEXT, status INTEGER, PRIMARY KEY (symbol, ym))")
    c.execute("CREATE TABLE IF NOT EXISTS p9s_tr (symbol TEXT, t INTEGER, ms BLOB, px BLOB, status INTEGER, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS p9s2_cand (symbol TEXT, t INTEGER, ref REAL, hi3 REAL, pq60 REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS p9s2_cdone (symbol TEXT, ym TEXT, status INTEGER, PRIMARY KEY (symbol, ym))")
    c.execute("CREATE TABLE IF NOT EXISTS p9s2_tr (symbol TEXT, t INTEGER, ms BLOB, px BLOB, qv BLOB, buy BLOB, status INTEGER,"
              " PRIMARY KEY (symbol, t))")
    return c


def _get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research/0.1"}), timeout=120) as r:
            return r.read(), 200
    except urllib.error.HTTPError as e:
        return None, e.code


def _k1m(sym, y, m):
    raw, st = _get(f"https://data.binance.vision/data/futures/um/monthly/klines/{sym}/1m/{sym}-1m-{y}-{m:02d}.zip")
    if raw is None:
        return None, st
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        txt = z.read(z.namelist()[0])
    if txt[:1].isalpha():
        txt = txt.split(b"\n", 1)[1] if b"\n" in txt else b""
    if not txt.strip():
        return None, 204
    return np.loadtxt(io.BytesIO(txt), delimiter=",", usecols=(0, 2, 4, 7), ndmin=2), 200   # t, high, close, qvol


def cands(a):
    t = a[:, 0].astype(np.int64)
    pos = {int(x): i for i, x in enumerate(t)}
    out = []
    for d in range(int(t[0]) // D * D + D, int(t[-1]) + 1, D):
        i = pos.get(d)
        ip = pos.get(d - MIN)
        if i is None or ip is None:
            continue
        ref = a[ip, 2]
        hs = [a[pos[d + k * MIN], 1] for k in range(3) if d + k * MIN in pos]
        if ref > 0 and hs and max(hs) / ref - 1 >= 0.02 and a[i, 3] >= 5e4:
            out.append((d, float(ref), float(max(hs)), float(a[i, 3])))
    return out


def collect_cands():
    c = conn()
    syms = [r[0] for r in c.execute("SELECT symbol FROM xs_sym") if r[0].isascii()]
    done = {(s, ym) for s, ym in c.execute("SELECT symbol, ym FROM p9s_cdone")}
    jobs = [(s, y, m) for s in syms for y, m in MONTHS if (s, f"{y}-{m:02d}") not in done]
    print(f"1분봉 다시 훑기 {len(jobs)}개", flush=True)

    def work(j):
        try:
            a, st = _k1m(*j)
        except Exception as e:      # noqa: BLE001
            return j, None, -1, str(e)
        return j, (cands(a) if a is not None else []), st, None
    n = 0
    with ThreadPoolExecutor(8) as ex:
        for f in as_completed([ex.submit(work, j) for j in jobs]):
            (s, y, m), ev, st, err = f.result()
            n += 1
            if st == -1:
                print(f"  ⚠ {s} {y}-{m:02d}: {err}", flush=True)
                continue
            c.executemany("INSERT OR REPLACE INTO p9s_cand VALUES (?,?,?,?,?)", [(s, *e) for e in ev])
            c.execute("INSERT OR REPLACE INTO p9s_cdone VALUES (?,?,?)", (s, f"{y}-{m:02d}", st))
            if n % 500 == 0:
                c.commit()
                print(f"  {n}/{len(jobs)} · 후보 {c.execute('SELECT COUNT(*) FROM p9s_cand').fetchone()[0]}", flush=True)
    c.commit()
    print(f"후보 {c.execute('SELECT COUNT(*) FROM p9s_cand').fetchone()[0]}", flush=True)
    c.close()


def _agg(sym, d):
    day = datetime.fromtimestamp(d / 1000, timezone.utc).strftime("%Y-%m-%d")
    url = f"https://data.binance.vision/data/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip"
    req = urllib.request.Request(url, headers={"User-Agent": "research/0.1"})
    try:
        r = urllib.request.urlopen(req, timeout=180)
    except urllib.error.HTTPError as e:
        return None, None, e.code
    raw = r.read()
    ms, px = [], []
    with zipfile.ZipFile(io.BytesIO(raw)) as z, z.open(z.namelist()[0]) as f:
        for line in io.TextIOWrapper(f):
            if line[:1].isalpha():
                continue
            p = line.split(",")
            tt = int(p[5]) - d
            if tt >= WIN:                                  # 시간순 — 앞 12분만
                break
            if tt >= 0:
                ms.append(tt)
                px.append(float(p[1]))
    return np.array(ms, dtype=np.int32), np.array(px, dtype=np.float64), 200


def collect_agg():
    c = conn()
    have = {(s, t) for s, t in c.execute("SELECT symbol, t FROM p9s_tr")}
    jobs = [(s, t) for s, t in c.execute("SELECT symbol, t FROM p9s_cand") if (s, t) not in have]
    print(f"aggTrades {len(jobs)}개", flush=True)

    def work(j):
        try:
            return j, *_agg(*j)
        except Exception as e:      # noqa: BLE001
            return j, None, None, -1
    n = 0
    with ThreadPoolExecutor(8) as ex:
        for f in as_completed([ex.submit(work, j) for j in jobs]):
            (s, t), ms, px, st = f.result()
            n += 1
            if st == -1:
                continue
            c.execute("INSERT OR REPLACE INTO p9s_tr VALUES (?,?,?,?,?)",
                      (s, t, ms.tobytes() if ms is not None else b"", px.tobytes() if px is not None else b"", st))
            if n % 100 == 0:
                c.commit()
                print(f"  {n}/{len(jobs)}", flush=True)
    c.commit()
    c.close()


def load():
    c = conn()
    rows = c.execute("SELECT c.symbol, c.t, c.ref, r.ms, r.px FROM p9s_cand c JOIN p9s_tr r USING (symbol, t) "
                     "WHERE r.status=200").fetchall()
    c.close()
    return [(s, t, ref, np.frombuffer(m, dtype=np.int32), np.frombuffer(p, dtype=np.float64)) for s, t, ref, m, p in rows if m]


def trade(ev, th, S, M, side, lat=LAT, at_detect=False):
    s, t, ref, ms, px = ev
    hit = np.where((px >= ref * (1 + th)) & (ms <= S))[0]
    if not len(hit):
        return None
    td = ms[hit[0]]
    if at_detect:
        ie, te = hit[0], td
    else:
        k = np.searchsorted(ms, td + lat)
        if k >= len(ms):
            return None
        ie, te = k, ms[k]
    j = np.searchsorted(ms, te + M, side="right") - 1
    if j < ie or te + M > WIN:
        return None
    return side * (px[j] / px[ie] - 1) - COST


def day_t(tt, rr):
    days = np.asarray(tt) // D
    u, inv = np.unique(days, return_inverse=True)
    dm = np.bincount(inv, rr) / np.bincount(inv)
    return dm, (dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm))) if len(dm) > 2 else np.nan)


# ── v2: 누설·낙관 정정 ──────────────────────────────────────────────────────
PQ60 = 5e5


def cands2(a):
    t = a[:, 0].astype(np.int64)
    pos = {int(x): i for i, x in enumerate(t)}
    out = []
    for d in range(int(t[0]) // D * D + D, int(t[-1]) + 1, D):
        ip = pos.get(d - MIN)
        if pos.get(d) is None or ip is None:
            continue
        ref = a[ip, 2]
        hs = [a[pos[d + k * MIN], 1] for k in range(3) if d + k * MIN in pos]
        pq = sum(a[pos[d - k * MIN], 3] for k in range(1, 61) if d - k * MIN in pos)
        if ref > 0 and hs and max(hs) / ref - 1 >= 0.02:
            out.append((d, float(ref), float(max(hs)), float(pq)))
    return out


def collect_cands2():
    c = conn()
    syms = [r[0] for r in c.execute("SELECT symbol FROM xs_sym") if r[0].isascii()]
    done = {(s, ym) for s, ym in c.execute("SELECT symbol, ym FROM p9s2_cdone")}
    jobs = [(s, y, m) for s in syms for y, m in MONTHS if (s, f"{y}-{m:02d}") not in done]
    print(f"v2 1분봉 훑기 {len(jobs)}개", flush=True)

    def work(j):
        try:
            a, st = _k1m(*j)
        except Exception as e:      # noqa: BLE001
            return j, None, -1, str(e)
        return j, (cands2(a) if a is not None else []), st, None
    n = 0
    with ThreadPoolExecutor(8) as ex:
        for f in as_completed([ex.submit(work, j) for j in jobs]):
            (s, y, m), ev, st, err = f.result()
            n += 1
            if st == -1:
                print(f"  ⚠ {s} {y}-{m:02d}: {err}", flush=True)
                continue
            c.executemany("INSERT OR REPLACE INTO p9s2_cand VALUES (?,?,?,?,?)", [(s, *e) for e in ev])
            c.execute("INSERT OR REPLACE INTO p9s2_cdone VALUES (?,?,?)", (s, f"{y}-{m:02d}", st))
            if n % 500 == 0:
                c.commit()
                print(f"  {n}/{len(jobs)} · 후보 {c.execute('SELECT COUNT(*) FROM p9s2_cand').fetchone()[0]}", flush=True)
    c.commit()
    print(f"v2 후보 {c.execute('SELECT COUNT(*) FROM p9s2_cand').fetchone()[0]}", flush=True)
    c.close()


def _agg2(sym, d):
    day = datetime.fromtimestamp(d / 1000, timezone.utc).strftime("%Y-%m-%d")
    url = f"https://data.binance.vision/data/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip"
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research/0.1"}), timeout=180)
    except urllib.error.HTTPError as e:
        return None, e.code
    raw = r.read()
    ms, px, qv, buy = [], [], [], []
    with zipfile.ZipFile(io.BytesIO(raw)) as z, z.open(z.namelist()[0]) as f:
        for line in io.TextIOWrapper(f):
            if line[:1].isalpha():
                continue
            p = line.split(",")
            tt = int(p[5]) - d
            if tt >= WIN:
                break
            if tt >= 0:
                pr = float(p[1])
                ms.append(tt); px.append(pr); qv.append(pr * float(p[2]))
                buy.append(p[6].strip().lower() == "false")       # 매수 주도(테이커 매수)
    return (np.array(ms, dtype=np.int32), np.array(px), np.array(qv, dtype=np.float32), np.array(buy, dtype=np.bool_)), 200


def collect_agg2():
    c = conn()
    have = {(s, t) for s, t in c.execute("SELECT symbol, t FROM p9s2_tr")}
    jobs = [(s, t) for s, t in c.execute("SELECT symbol, t FROM p9s2_cand") if (s, t) not in have]
    print(f"v2 aggTrades {len(jobs)}개", flush=True)

    def work(j):
        try:
            return j, *_agg2(*j)
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


def load2():
    c = conn()
    rows = c.execute("SELECT c.symbol, c.t, c.ref, c.pq60, r.ms, r.px, r.qv, r.buy FROM p9s2_cand c JOIN p9s2_tr r USING (symbol, t) "
                     "WHERE r.status=200").fetchall()
    c.close()
    out = []
    for s, t, ref, pq, m, p, q, b in rows:
        if m:
            out.append((s, t, ref, pq, np.frombuffer(m, dtype=np.int32), np.frombuffer(p), np.frombuffer(q, dtype=np.float32),
                        np.frombuffer(b, dtype=np.bool_)))
    return out


def trade2(ev, th, S, M, side, lat=LAT, liq=True):
    s, t, ref, pq, ms, px, qv, buy = ev
    if liq and pq < PQ60:
        return None
    hit = np.where((px >= ref * (1 + th)) & (ms <= S))[0]
    if not len(hit):
        return None
    td = ms[hit[0]]
    k = np.searchsorted(ms, td + lat)
    ent_side = buy if side == 1 else ~buy                  # 롱 진입 = 매수 주도 체결가 · 숏 진입 = 매도 주도
    cand = np.where(ent_side[k:])[0]
    if not len(cand):
        return None
    ie = k + cand[0]
    te = ms[ie]
    if te + M > WIN:
        return None
    j = np.searchsorted(ms, te + M, side="right") - 1
    ex_side = ~buy if side == 1 else buy                   # 롱 청산 = 매도 주도 · 숏 청산 = 매수 주도
    prev = np.where(ex_side[ie + 1:j + 1])[0]
    if not len(prev):
        return None
    jx = ie + 1 + prev[-1]
    return side * (px[jx] / px[ie] - 1) - COST


def main_v2():
    evs = load2()
    liq = [e for e in evs if e[3] >= PQ60]
    print(f"v2 후보 날(첫 3분 고가 +2%↑, 거래대금 조건 없음) {len(evs)} · 그중 직전 1시간 ≥ $50만 {len(liq)} · 심볼 {len({e[0] for e in liq})}")
    res = []
    for th in THS:
        for S in SS:
            for M in MS:
                for side, nm in ((1, "롱"), (-1, "숏")):
                    out = [(e[0], e[1], r) for e in liq if (r := trade2(e, th, S, M, side)) is not None]
                    if len(out) < 10:
                        continue
                    rr = np.array([x[2] for x in out])
                    _, tt = day_t([x[1] for x in out], rr)
                    res.append(dict(th=th, S=S, M=M, side=side, nm=nm, out=out, n=len(rr), mean=rr.mean(), med=np.median(rr), t=tt))
    print("\n θ · 감지 S · 보유 M · 방향 |  건수 |  평균  |  중앙  |   t")
    for x in sorted(res, key=lambda x: -x["t"])[:14]:
        print(f"  +{x['th']*100:.0f}% · {x['S']//1000:2d}초 · {x['M']//1000:3d}초 · {x['nm']} | {x['n']:5d} | {x['mean']*100:+6.2f} | {x['med']*100:+6.2f} | {x['t']:+5.2f}")
    print("  …(나머지 생략)")
    b = max(res, key=lambda x: x["t"])
    ss = np.array([x[0] for x in b["out"]]); tt = np.array([x[1] for x in b["out"]]); rr = np.array([x[2] for x in b["out"]])
    hv = []
    for i, (nm, (y, mo)) in enumerate(HALVES):
        a = _ms(y, mo)
        e = _ms(*HALVES[i + 1][1]) if i + 1 < len(HALVES) else 1 << 62
        k = (tt >= a) & (tt < e)
        hv.append((nm, rr[k].mean() if k.sum() else np.nan, int(k.sum())))
    top_s = max(set(ss), key=lambda s: np.sum(ss == s))
    days = tt // D
    top_d = max(set(days), key=lambda d: np.sum(days == d))
    ex_s, ex_d = rr[ss != top_s].mean(), rr[days != top_d].mean()
    med_s = np.median([rr[ss == s].mean() for s in set(ss)])
    c1, c2, c3 = b["t"] >= 3.0, all(np.isfinite(v) and v > 0 for _, v, _ in hv), ex_s > 0 and ex_d > 0 and med_s > 0
    srt = np.sort(rr)
    print(f"\n■ v2 최고: +{b['th']*100:.0f}% · {b['S']//1000}초 · {b['M']//1000}초 · {b['nm']} — {b['mean']*100:+.2f}%/건 · t {b['t']:+.2f} · "
          f"이익 {np.mean(rr>0):.0%} · 중앙 {np.median(rr)*100:+.2f} · 최악 {rr.min()*100:+.1f}% · 최고 {rr.max()*100:+.1f}%")
    print(f"   상위 1% 빼면 {srt[:-max(1, len(rr)//100)].mean()*100:+.2f}% · 상위 5% 빼면 {srt[:-max(1, len(rr)//20)].mean()*100:+.2f}%")
    print("   반기: " + " · ".join(f"{nm} {v*100:+.2f}% ({k})" for nm, v, k in hv))
    print(f"   최다 심볼 {top_s} 빼면 {ex_s*100:+.2f}% · 최다 날짜 빼면 {ex_d*100:+.2f}% · 심볼 중앙 {med_s*100:+.2f}%")
    print(f"   ① {'✅' if c1 else '❌'} · ② {'✅' if c2 else '❌'} · ③ {'✅' if c3 else '❌'} → {'✅ 합격' if c1 and c2 and c3 else '❌ 불합격'}")
    print("\n━━ 참고(판정 아님) ━━")
    for th, S, M in ((0.02, 5_000, 30_000), (0.04, 5_000, 30_000), (0.02, 20_000, 30_000), (b["th"], b["S"], b["M"])):
        line = []
        for lab, kw in (("지연 0", dict(lat=0)), ("0.5초", {}), ("1초", dict(lat=1000)), ("2초", dict(lat=2000)),
                        ("유동성 조건 없음", dict(liq=False))):
            src = evs if kw.get("liq") is False else liq
            r2 = np.array([r for e in src if (r := trade2(e, th, S, M, 1, **kw)) is not None])
            line.append(f"{lab} {r2.mean()*100:+.2f}%({len(r2)})" if len(r2) else f"{lab} -")
        print(f"  +{th*100:.0f}% · {S//1000}초 · {M//1000}초 롱: " + " · ".join(line))
    # 분기별(최고 조합)
    q = {}
    for x in b["out"]:
        dt = datetime.fromtimestamp(x[1] / 1000, timezone.utc)
        key = f"{dt.year}Q{(dt.month - 1)//3 + 1}"
        q.setdefault(key, []).append(x[2])
    print("  최고 조합 분기별: " + " · ".join(f"{k} {np.mean(v)*100:+.2f}%({len(v)})" for k, v in sorted(q.items())))


def main():
    if "--cand" in sys.argv:
        return collect_cands()
    if "--agg" in sys.argv:
        return collect_agg()
    if "--cand2" in sys.argv:
        return collect_cands2()
    if "--agg2" in sys.argv:
        return collect_agg2()
    if "--v2" in sys.argv:
        return main_v2()
    evs = load()
    print(f"후보 날(첫 3분 고가 +2%↑) {len(evs)}개 · 심볼 {len({e[0] for e in evs})}")
    res = []
    for th in THS:
        for S in SS:
            for M in MS:
                for side, nm in ((1, "롱"), (-1, "숏")):
                    out = [(e[0], e[1], r) for e in evs if (r := trade(e, th, S, M, side)) is not None]
                    if len(out) < 10:
                        continue
                    rr = np.array([x[2] for x in out])
                    _, tt = day_t([x[1] for x in out], rr)
                    res.append(dict(th=th, S=S, M=M, side=side, nm=nm, out=out, n=len(rr), mean=rr.mean(), med=np.median(rr), t=tt))
    print("\n θ · 감지 S · 보유 M · 방향 |  건수 |  평균  |  중앙  |   t")
    for x in sorted(res, key=lambda x: -x["t"]):
        print(f"  +{x['th']*100:.0f}% · {x['S']//1000:2d}초 · {x['M']//1000:3d}초 · {x['nm']} | {x['n']:5d} | {x['mean']*100:+6.2f} | {x['med']*100:+6.2f} | {x['t']:+5.2f}")
    b = max(res, key=lambda x: x["t"])
    ss = np.array([x[0] for x in b["out"]]); tt = np.array([x[1] for x in b["out"]]); rr = np.array([x[2] for x in b["out"]])
    hv = []
    for i, (nm, (y, mo)) in enumerate(HALVES):
        a = _ms(y, mo)
        e = _ms(*HALVES[i + 1][1]) if i + 1 < len(HALVES) else 1 << 62
        k = (tt >= a) & (tt < e)
        hv.append((nm, rr[k].mean() if k.sum() else np.nan, int(k.sum())))
    top_s = max(set(ss), key=lambda s: np.sum(ss == s))
    days = tt // D
    top_d = max(set(days), key=lambda d: np.sum(days == d))
    ex_s, ex_d = rr[ss != top_s].mean(), rr[days != top_d].mean()
    med_s = np.median([rr[ss == s].mean() for s in set(ss)])
    c1, c2, c3 = b["t"] >= 3.0, all(np.isfinite(v) and v > 0 for _, v, _ in hv), ex_s > 0 and ex_d > 0 and med_s > 0
    print(f"\n■ 최고: +{b['th']*100:.0f}% · {b['S']//1000}초 · {b['M']//1000}초 · {b['nm']} — {b['mean']*100:+.2f}%/건 · t {b['t']:+.2f} · 이익 {np.mean(rr>0):.0%} · 최악 {rr.min()*100:+.1f}%")
    print("   반기: " + " · ".join(f"{nm} {v*100:+.2f}% ({k})" for nm, v, k in hv))
    print(f"   최다 심볼 {top_s} 빼면 {ex_s*100:+.2f}% · 최다 날짜 빼면 {ex_d*100:+.2f}% · 심볼 중앙 {med_s*100:+.2f}%")
    print(f"   ① {'✅' if c1 else '❌'} · ② {'✅' if c2 else '❌'} · ③ {'✅' if c3 else '❌'} → {'✅ 합격' if c1 and c2 and c3 else '❌ 불합격'}")
    print("\n━━ 참고(판정 아님): 롱 따라타기, 지연별 ━━")
    for th in THS:
        for S in SS:
            for M in MS:
                line = []
                for lab, kw in (("감지 즉시·슬리피지 무시", dict(at_detect=True)), ("0.5초", {}), ("2초", dict(lat=2000))):
                    rr2 = np.array([r for e in evs if (r := trade(e, th, S, M, 1, **kw)) is not None])
                    line.append(f"{lab} {rr2.mean()*100:+.2f}%" if len(rr2) else f"{lab} -")
                print(f"  +{th*100:.0f}% · {S//1000:2d}초 · {M//1000:3d}초 롱: " + " · ".join(line))


if __name__ == "__main__":
    main()
