"""P9 — 한국 시간 9시(00:00 UTC) 알트 펌핑 따라타기 (사용자 아이디어, 2026-11-19).

사용자 관찰: "알트 중에 한국 9시가 되면 갑자기 펌핑하는 코인들이 있다 → 알아채고 들어가서 조금만 먹고 나온다."
00:00 UTC 는 업비트·바이낸스 일봉 리셋(그날 상승률 순위가 새로 시작) · 8h 펀딩 정산 시각이 겹친다.

━━ 사전 고정 (2026-11-19 작성 · 결과 보기 전) ━━
데이터  USDT 무기한(xs_sym 654개, 폐지 포함) 1분봉, data.binance.vision 월별 zip, 2025-07 ~ 2026-10 (최근 16개월).
사건    매 정시 H:00(UTC). r_N = (H:00 + N분 종가) / (H:00 시가) − 1.
        감지 = r_N ≥ θ  그리고  첫 N분 거래대금 ≥ 5 × (직전 60분 1분 거래대금 중앙 × N)  그리고  첫 N분 거래대금 ≥ $5만.
매매    감지 시점(H:00+N분 종가)에 진입 → M분 뒤 종가에 청산. 손절·익절 없음(참고로만).
        방향 두 가지: 따라타기(롱) · 되돌림(숏).
비용    왕복 0.20%(수수료 0.05×2 + 펌핑 중 얇은 호가 슬리피지 0.05×2).
격자    N ∈ {1, 3, 5} · θ ∈ {+2%, +4%} · M ∈ {5, 15, 30} · 방향 2 = 36개.
판정    주 가설은 H = 0(한국 9시). t 는 날짜별 평균으로 묶어서(같은 날 여러 코인이 같이 펌핑 → 독립 아님).
  ① 36개 중 하나라도 H=0 에서 t ≥ 3.0 (36개 다중 비교 문턱)
  ② 그 조합이 반기 2025-H2 · 2026-H1 · 2026-H2(7~10월) 모두 > 0
  ③ 최다 심볼 빼도 · 최다 날짜 빼도 평균 > 0 · 심볼별 평균의 중앙 > 0
  ④ 9시가 특별한가: 같은 조합을 24개 정시에 똑같이 적용했을 때 H=0 이 평균 수익 상위 3위 안
     (④만 실패면 '9시 효과'는 아니고 '정시 펌핑 일반'일 뿐 — 그래도 ①~③ 통과면 전략으론 유효)
참고(판정 아님): 익절 +2%/손절 −2% · 업비트 원화 상장 여부 · 이벤트 빈도.

    python3 -u -m research.exp_P9_hour_pump --collect     # 받기(zip 은 메모리에서 처리, 후보 사건만 저장)
    python3 -u -m research.exp_P9_hour_pump               # 판정
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

from research.xv_data import DB

MONTHS = [(2025, m) for m in range(7, 13)] + [(2026, m) for m in range(1, 11)]
NS, THS, MS = (1, 3, 5), (0.02, 0.04), (5, 15, 30)
PATH = 66                                  # 0시 시가 다음 분 종가부터 65분
COST = 0.0020
MIN = 60_000
H_MS = 3_600_000
HALVES = [("2025-H2", (2025, 7)), ("2026-H1", (2026, 1)), ("2026-H2", (2026, 7))]


def conn():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("CREATE TABLE IF NOT EXISTS p9_ev (symbol TEXT, t INTEGER, o0 REAL, path BLOB, q1 REAL, q3 REAL, q5 REAL,"
              " base REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS p9_done (symbol TEXT, ym TEXT, status INTEGER, hours INTEGER, PRIMARY KEY (symbol, ym))")
    return c


def _fetch(sym, y, m):
    url = f"https://data.binance.vision/data/futures/um/monthly/klines/{sym}/1m/{sym}-1m-{y}-{m:02d}.zip"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research/0.1"}), timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        return None, e.code
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        txt = z.read(z.namelist()[0])
    lines = txt.split(b"\n", 1)
    if lines[0][:1].isalpha():
        txt = lines[1] if len(lines) > 1 else b""
    if not txt.strip():
        return None, 204
    a = np.loadtxt(io.BytesIO(txt), delimiter=",", usecols=(0, 1, 4, 7), ndmin=2)
    return a, 200


def events(a):
    """1분봉 [t, open, close, qvol] → 후보 사건(첫 1·3·5분 중 하나라도 +2% 이상)."""
    t0 = int(a[0, 0]) // H_MS * H_MS
    n = (int(a[-1, 0]) - t0) // MIN + 1 + PATH + 1
    O = np.full(n, np.nan); Cl = np.full(n, np.nan); Q = np.full(n, np.nan)
    ix = ((a[:, 0].astype(np.int64) - t0) // MIN).astype(np.int64)
    O[ix], Cl[ix], Q[ix] = a[:, 1], a[:, 2], a[:, 3]
    out, hours = [], 0
    for h in range(60, n - PATH - 1, 60):                   # h = 정시 분 인덱스(직전 60분 필요)
        o0 = O[h]
        if not o0 > 0:
            continue
        hours += 1
        c = Cl[h:h + PATH]
        r = c[[0, 2, 4]] / o0 - 1
        if not np.nanmax(r) >= 0.02:
            continue
        q = Q[h:h + 5]
        base = np.nanmedian(Q[h - 60:h])
        out.append((t0 + h * MIN, float(o0), c.astype(np.float32).tobytes(),
                    float(np.nansum(q[:1])), float(np.nansum(q[:3])), float(np.nansum(q[:5])),
                    float(base) if base == base else 0.0))
    return out, hours


def collect():
    c = conn()
    syms = [r[0] for r in c.execute("SELECT symbol FROM xs_sym")]
    done = {(s, ym) for s, ym in c.execute("SELECT symbol, ym FROM p9_done")}
    jobs = [(s, y, m) for s in syms for y, m in MONTHS if (s, f"{y}-{m:02d}") not in done and s.isascii()]
    print(f"심볼 {len(syms)} · 월 {len(MONTHS)} · 받을 것 {len(jobs)}", flush=True)

    def work(j):
        s, y, m = j
        try:
            a, st = _fetch(s, y, m)
        except Exception as e:      # noqa: BLE001
            return j, None, -1, 0, str(e)
        if a is None:
            return j, [], st, 0, None
        ev, hrs = events(a)
        return j, ev, 200, hrs, None
    n = 0
    with ThreadPoolExecutor(8) as ex:
        for f in as_completed([ex.submit(work, j) for j in jobs]):
            (s, y, m), ev, st, hrs, err = f.result()
            n += 1
            if st == -1:
                print(f"  ⚠ {s} {y}-{m:02d}: {err}", flush=True)
                continue
            c.executemany("INSERT OR REPLACE INTO p9_ev VALUES (?,?,?,?,?,?,?,?)", [(s, *e) for e in ev])
            c.execute("INSERT OR REPLACE INTO p9_done VALUES (?,?,?,?)", (s, f"{y}-{m:02d}", st, hrs))
            if n % 200 == 0:
                c.commit()
                print(f"  {n}/{len(jobs)} · 사건 누적 {c.execute('SELECT COUNT(*) FROM p9_ev').fetchone()[0]}", flush=True)
    c.commit()
    c.close()


def _ms(y, m):
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)


def load():
    c = conn()
    rows = c.execute("SELECT symbol, t, o0, path, q1, q3, q5, base FROM p9_ev").fetchall()
    hours = c.execute("SELECT SUM(hours) FROM p9_done WHERE status=200").fetchone()[0]
    c.close()
    sym = np.array([r[0] for r in rows])
    t = np.array([r[1] for r in rows], dtype=np.int64)
    o0 = np.array([r[2] for r in rows])
    P = np.stack([np.frombuffer(r[3], dtype=np.float32).astype(float) for r in rows])
    Qn = {1: np.array([r[4] for r in rows]), 3: np.array([r[5] for r in rows]), 5: np.array([r[6] for r in rows])}
    base = np.array([r[7] for r in rows])
    return sym, t, o0, P, Qn, base, hours


def trades(sym, t, o0, P, Qn, base, N, th, M, side, tp=None):
    r = P[:, N - 1] / o0 - 1
    ok = (r >= th) & (Qn[N] >= 5 * base * N) & (Qn[N] >= 5e4)
    ent = P[:, N - 1]
    ex = P[:, N - 1 + M]
    if tp is not None:                                     # 참고: 분 종가 기준 ±tp 먼저 닿은 쪽
        seg = P[:, N:N + M] / ent[:, None] - 1
        ex = ex.copy()
        for i in np.where(ok)[0]:
            hit = np.where(np.abs(seg[i]) >= tp)[0]
            if len(hit):
                ex[i] = P[i, N + hit[0]]
    ret = side * (ex / ent - 1) - COST
    ok &= np.isfinite(ret)
    return ok, ret


def day_t(tt, rr):
    days = tt // 86_400_000
    u, inv = np.unique(days, return_inverse=True)
    dm = np.bincount(inv, rr) / np.bincount(inv)
    if len(dm) < 3:
        return dm, np.nan
    return dm, dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm)))


def main():
    if "--collect" in sys.argv:
        collect()
        return
    sym, t, o0, P, Qn, base, hours = load()
    hod = (t // H_MS) % 24
    print(f"후보 사건(+2% 이상) {len(t):,} · 관측 정시 {hours:,} 심볼·시간")
    res = []
    for N in NS:
        for th in THS:
            for M in MS:
                for side, nm in ((1, "롱"), (-1, "숏")):
                    ok, ret = trades(sym, t, o0, P, Qn, base, N, th, M, side)
                    by_h = []
                    for H in range(24):
                        mk = ok & (hod == H)
                        by_h.append(ret[mk].mean() if mk.sum() else np.nan)
                    m0 = ok & (hod == 0)
                    dm, tt = day_t(t[m0], ret[m0])
                    rank = int(np.sum(np.array(by_h) > by_h[0])) + 1 if np.isfinite(by_h[0]) else 99
                    res.append(dict(N=N, th=th, M=M, side=side, nm=nm, n=int(m0.sum()), days=len(dm),
                                    mean=ret[m0].mean() if m0.sum() else np.nan, t=tt, rank=rank,
                                    other=np.nanmean(by_h[1:]), mask=m0, ret=ret))
    print("\n H=0(한국 9시) 결과 — 36개 조합 (평균은 비용 0.20% 뺀 거래당 %, t 는 날짜 묶음)")
    print(" N분 · 문턱 · 보유 · 방향 |  건수 · 날수 |  평균  |   t   | 24개 정시 중 순위 · 다른 정시 평균")
    for x in sorted(res, key=lambda x: -(x["t"] if np.isfinite(x["t"]) else -9)):
        print(f"  {x['N']}분 · +{x['th']*100:.0f}% · {x['M']:2d}분 · {x['nm']} | {x['n']:5d} · {x['days']:4d} | "
              f"{x['mean']*100:+6.2f} | {x['t']:+5.2f} | {x['rank']:2d}위 · {x['other']*100:+6.2f}")
    best = max(res, key=lambda x: x["t"] if np.isfinite(x["t"]) else -9)
    m0, ret = best["mask"], best["ret"]
    rr, ss, tt = ret[m0], sym[m0], t[m0]
    print(f"\n■ 최고 조합: {best['N']}분 · +{best['th']*100:.0f}% · {best['M']}분 · {best['nm']} — 거래당 {best['mean']*100:+.2f}% · t {best['t']:+.2f}")
    c1 = best["t"] >= 3.0
    hv = []
    for i, (nm, (y, mo)) in enumerate(HALVES):
        a = _ms(y, mo)
        b = _ms(*HALVES[i + 1][1]) if i + 1 < len(HALVES) else 1 << 62
        k = (tt >= a) & (tt < b)
        hv.append((nm, rr[k].mean() if k.sum() else np.nan, int(k.sum())))
    c2 = all(np.isfinite(v) and v > 0 for _, v, _ in hv)
    top_sym = max(set(ss), key=lambda s: np.sum(ss == s))
    days = tt // 86_400_000
    top_day = max(set(days), key=lambda d: np.sum(days == d))
    ex_s, ex_d = rr[ss != top_sym].mean(), rr[days != top_day].mean()
    med_sym = np.median([rr[ss == s].mean() for s in set(ss)])
    c3 = ex_s > 0 and ex_d > 0 and med_sym > 0
    c4 = best["rank"] <= 3
    print("   반기: " + " · ".join(f"{nm} {v*100:+.2f}% ({k}건)" for nm, v, k in hv))
    print(f"   최다 심볼 {top_sym}({np.sum(ss==top_sym)}건) 빼면 {ex_s*100:+.2f}% · 최다 날짜 빼면 {ex_d*100:+.2f}% · 심볼별 중앙 {med_sym*100:+.2f}%")
    print(f"   이익 {np.mean(rr>0):.0%} · 중앙 {np.median(rr)*100:+.2f}% · 최악 {rr.min()*100:+.1f}% · 최고 {rr.max()*100:+.1f}%")
    print(f"   ① t≥3.0 {'✅' if c1 else '❌'} · ② 반기 {'✅' if c2 else '❌'} · ③ 제외·중앙 {'✅' if c3 else '❌'} · ④ 9시 상위 3위 {'✅' if c4 else '❌'}"
          f" ({best['rank']}위)")
    print(f"   판정: {'✅ 합격' if c1 and c2 and c3 else '❌ 불합격'}" + ("" if c4 or not (c1 and c2 and c3) else " (단 9시 특유 효과는 아님)"))
    # 참고
    print("\n━━ 참고(판정 아님) ━━")
    for N in NS:
        k = (P[:, N - 1] / o0 - 1 >= 0.02)
        print(f"  첫 {N}분 +2%↑ 사건 정시별 빈도: 0시 {np.sum(k & (hod==0))} · 다른 정시 평균 {np.mean([np.sum(k & (hod==H)) for H in range(1,24)]):.0f}")
    for side, nm in ((1, "롱"), (-1, "숏")):
        ok, ret = trades(sym, t, o0, P, Qn, base, best["N"], best["th"], 30, side, tp=0.02)
        mk = ok & (hod == 0)
        dm, tt2 = day_t(t[mk], ret[mk])
        print(f"  최고 조합 N·θ · 30분 안 ±2% 먼저 닿으면 청산 · {nm}: {mk.sum()}건 · 평균 {ret[mk].mean()*100:+.2f}% · t {tt2:+.2f}")


if __name__ == "__main__":
    main()
