"""SG — 모든 시각의 '꾸준한 급등' 감지 후 따라타기, 1분봉 (사용자 관찰 "오늘 급등들은 상승이 앞쪽에 몰려 있지 않더라", 2026-11-20).

배경: 최근 24시간 확인(감지 56건) — 중앙은 음수(열에 여섯은 손해)인데 60분 평균은 +0.85%(비용 뒤). 상승률 상위 10 코인의 감지 13건은
30분 뒤 +5.6%, 나머지 43건은 −1.3% → 크게 가는 몇 건이 평균을 만드는 '오른쪽 꼬리'. 하루치라 결론 불가 → 16개월로 본다.
9시·정시 급등(P9·P9S)과 달리 느린 급등은 1분 단위로도 탈 수 있다는 가정.

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
데이터  USDT 무기한(xs_sym 654, 폐지 포함) 1분봉 2025-07 ~ 2026-10 (data.binance.vision 월별 zip, 메모리에서 처리).
감지    매 1분봉 종가 i: 5분 수익 C[i]/C[i−5] − 1 ≥ θ  그리고  5분 거래대금 ≥ 5 × (직전 [i−65, i−5) 1분 거래대금 중앙 × 5)
        그리고  5분 거래대금 ≥ $10만 · 같은 코인 60분 간격(첫 감지만). (최근 24시간 확인과 같은 정의, θ 만 격자 — θ 마다 따로 감지·쿨다운: θ 를 처음 넘은 순간)
진입    다음 1분봉 시가 O[i+1](감지 → 주문 1분 안).   비용 왕복 0.20% · 펀딩 무시(≤4시간).
청산    (a) 시간: M분 뒤 종가  (b) 추격: 1분 종가가 진입 뒤 최고 종가보다 −3% 아래로 내려온 첫 종가, 늦어도 M분 뒤 종가.
격자    θ ∈ {+3%, +5%, +10%} × M ∈ {15, 60, 240분} × 청산 (a)(b) = 18개 · 롱.
판정    t 는 날짜 묶음(같은 날 여러 코인이 같이 움직임). 18개 중 최고 t 조합이
  ① t ≥ 2.9(18개 다중 비교)  ② 반기 2025-H2 · 2026-H1 · 2026-H2(7~10월) 모두 > 0
  ③ 상위 5% 거래 빼도 평균 > 0 (꼬리 의존 점검 — 이 전략의 핵심 위험)  ④ 최다 심볼 빼도 · 최고 날 빼도 > 0.
참고(판정 아님) 숏(되돌림) 같은 격자 · 감지 크기·거래대금 크기별 · 이익 비율·중앙 · 손익비.
한계    월 경계(각 월 마지막 ~4시간 감지는 경로가 잘려 빠짐) · 1분봉이라 분 안 경로 모름 · 진입 시가 체결(슬리피지는 비용으로 근사).

    python3 -u -m research.exp_SG_surge_follow --collect
    python3 -u -m research.exp_SG_surge_follow
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
PATH = 241                                     # 진입 시가 다음 분부터 240분 종가(+진입 분 종가)
THS, MS = (0.03, 0.05, 0.10), (15, 60, 240)
COST, TRAIL = 0.0020, 0.03


def conn():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("CREATE TABLE IF NOT EXISTS sg_ev (symbol TEXT, th REAL, t INTEGER, r5 REAL, q5 REAL, base REAL, o1 REAL, path BLOB,"
              " PRIMARY KEY (symbol, th, t))")
    c.execute("CREATE TABLE IF NOT EXISTS sg_done (symbol TEXT, ym TEXT, status INTEGER, PRIMARY KEY (symbol, ym))")
    return c


def _k1m(sym, y, m):
    url = f"https://data.binance.vision/data/futures/um/monthly/klines/{sym}/1m/{sym}-1m-{y}-{m:02d}.zip"
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research/0.1"}), timeout=120).read()
    except urllib.error.HTTPError as e:
        return None, e.code
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        txt = z.read(z.namelist()[0])
    if txt[:1].isalpha():
        txt = txt.split(b"\n", 1)[1] if b"\n" in txt else b""
    if not txt.strip():
        return None, 204
    return np.loadtxt(io.BytesIO(txt), delimiter=",", usecols=(0, 1, 4, 7), ndmin=2), 200   # t, open, close, qvol


def events(a):
    t0 = int(a[0, 0])
    n = (int(a[-1, 0]) - t0) // MIN + 1
    O = np.full(n, np.nan); C = np.full(n, np.nan); Q = np.zeros(n)
    ix = ((a[:, 0].astype(np.int64) - t0) // MIN).astype(np.int64)
    O[ix], C[ix], Q[ix] = a[:, 1], a[:, 2], a[:, 3]
    r5 = C / np.r_[np.full(5, np.nan), C[:-5]] - 1
    q5 = np.convolve(Q, np.ones(5), "full")[:n]
    out = []
    for th in THS:                                     # θ 마다 따로 감지(쿨다운도 따로) — θ 를 처음 넘은 순간이 감지
        cand = np.where((r5 >= th) & (q5 >= 1e5))[0]
        last = -10_000
        for i in cand:
            if i < 65 or i + PATH >= n or i - last < 60:
                continue
            base = np.median(Q[i - 65:i - 5])
            if q5[i] < 25 * base:                      # 5 × (중앙 × 5)
                continue
            o1 = O[i + 1]
            p = C[i + 1:i + 1 + PATH]
            if not (o1 > 0) or np.isnan(p).any():
                continue
            last = i
            out.append((th, int(t0 + i * MIN), float(r5[i]), float(q5[i]), float(base), float(o1), p.astype(np.float32).tobytes()))
    return out


def collect():
    c = conn()
    syms = [r[0] for r in c.execute("SELECT symbol FROM xs_sym") if r[0].isascii()]
    done = {(s, ym) for s, ym in c.execute("SELECT symbol, ym FROM sg_done")}
    jobs = [(s, y, m) for s in syms for y, m in MONTHS if (s, f"{y}-{m:02d}") not in done]
    print(f"1분봉 {len(jobs)}개", flush=True)

    def work(j):
        try:
            a, st = _k1m(*j)
        except Exception as e:      # noqa: BLE001
            return j, None, -1, str(e)
        return j, (events(a) if a is not None else []), st, None
    n = 0
    with ThreadPoolExecutor(8) as ex:
        for f in as_completed([ex.submit(work, j) for j in jobs]):
            (s, y, m), ev, st, err = f.result()
            n += 1
            if st == -1:
                print(f"  ⚠ {s} {y}-{m:02d}: {err}", flush=True)
                continue
            c.executemany("INSERT OR REPLACE INTO sg_ev VALUES (?,?,?,?,?,?,?,?)", [(s, *e) for e in ev])
            c.execute("INSERT OR REPLACE INTO sg_done VALUES (?,?,?)", (s, f"{y}-{m:02d}", st))
            if n % 500 == 0:
                c.commit()
                print(f"  {n}/{len(jobs)} · 사건 {c.execute('SELECT COUNT(*) FROM sg_ev').fetchone()[0]}", flush=True)
    c.commit()
    c.close()


def load():
    c = conn()
    rows = c.execute("SELECT symbol, th, t, r5, q5, base, o1, path FROM sg_ev").fetchall()
    c.close()
    sym = np.array([r[0] for r in rows]); th = np.array([r[1] for r in rows]); t = np.array([int.from_bytes(r[2], 'little') if isinstance(r[2], bytes) else r[2] for r in rows], dtype=np.int64)   # 처음 수집분은 numpy int 가 BLOB 으로 저장됨
    r5 = np.array([r[3] for r in rows]); q5 = np.array([r[4] for r in rows]); o1 = np.array([r[6] for r in rows])
    P = np.stack([np.frombuffer(r[7], dtype=np.float32).astype(float) for r in rows])
    return sym, th, t, r5, q5, o1, P


def trade(o1, P, M, trail, side=1):
    """P[:, k] = 진입 분(k=0)부터 k분째 종가. 반환 순수익."""
    seg = P[:, :M]                                   # 진입 분 종가 ~ M분째 종가
    if not trail:
        ex = seg[:, -1]
    else:
        ref = np.maximum.accumulate(seg, axis=1) if side == 1 else np.minimum.accumulate(seg, axis=1)
        ref = np.maximum(ref, o1[:, None]) if side == 1 else np.minimum(ref, o1[:, None])
        hit = seg <= ref * (1 - TRAIL) if side == 1 else seg >= ref * (1 + TRAIL)
        first = np.where(hit.any(1), hit.argmax(1), M - 1)
        ex = seg[np.arange(len(seg)), first]
    return side * (ex / o1 - 1) - COST


def day_t(tt, rr):
    days = tt // D
    u, inv = np.unique(days, return_inverse=True)
    dm = np.bincount(inv, rr) / np.bincount(inv)
    return dm, dm.mean() / (dm.std(ddof=1) / np.sqrt(len(dm)))


def main():
    if "--collect" in sys.argv:
        return collect()
    sym, thv, t, r5, q5, o1, P = load()
    print("감지 사건: " + " · ".join(f"+{x*100:.0f}% {np.sum(np.isclose(thv, x)):,}" for x in THS) + f" · 심볼 {len(set(sym))} · 날 {len(set(t // D))}")
    res = []
    for th in THS:
        mk = np.isclose(thv, th)
        for M in MS:
            for tr in (False, True):
                rr = trade(o1[mk], P[mk], M, tr)
                _, tt = day_t(t[mk], rr)
                res.append(dict(th=th, M=M, tr=tr, mk=mk, rr=rr, t=tt))
    print("\n θ · 보유 · 청산 |  건수 | 평균 | 중앙 | 이익 | t(날짜) | 상위5% 뺌")
    for x in res:
        rr = x["rr"]; srt = np.sort(rr)
        print(f"  +{x['th']*100:2.0f}% · {x['M']:3d}분 · {'추격' if x['tr'] else '시간'} | {len(rr):6d} | {rr.mean()*100:+6.2f}% | {np.median(rr)*100:+6.2f} | "
              f"{np.mean(rr>0):.0%} | {x['t']:+5.2f} | {srt[:len(rr)-max(1,len(rr)//20)].mean()*100:+6.2f}")
    b = max(res, key=lambda x: x["t"])
    mk, rr = b["mk"], b["rr"]
    tt, ss = t[mk], sym[mk]
    hv = []
    for i, (nm, (y, mo)) in enumerate(HALVES):
        a = _ms(y, mo)
        e = _ms(*HALVES[i + 1][1]) if i + 1 < len(HALVES) else 1 << 62
        k = (tt >= a) & (tt < e)
        hv.append((nm, rr[k].mean() if k.sum() else np.nan, int(k.sum())))
    srt = np.sort(rr)
    cut = srt[:len(rr) - max(1, len(rr) // 20)].mean()
    top_s = max(set(ss), key=lambda s: rr[ss == s].sum())
    days = tt // D
    dm = {d: rr[days == d].mean() for d in set(days)}
    top_d = max(dm, key=dm.get)
    ex_s, ex_d = rr[ss != top_s].mean(), rr[days != top_d].mean()
    c1, c2, c3, c4 = b["t"] >= 2.9, all(np.isfinite(v) and v > 0 for _, v, _ in hv), cut > 0, ex_s > 0 and ex_d > 0
    w, l = rr[rr > 0], rr[rr <= 0]
    print(f"\n■ 최고: +{b['th']*100:.0f}% · {b['M']}분 · {'추격' if b['tr'] else '시간'} — {rr.mean()*100:+.2f}%/건 · 중앙 {np.median(rr)*100:+.2f} · t {b['t']:+.2f} · "
          f"이익 {np.mean(rr>0):.0%} · 평균 이익 {w.mean()*100:+.2f} / 평균 손실 {l.mean()*100:+.2f} · 최악 {rr.min()*100:+.1f} · 최고 {rr.max()*100:+.1f}")
    print("   반기: " + " · ".join(f"{nm} {v*100:+.2f}% ({k})" for nm, v, k in hv))
    print(f"   상위 5% 빼면 {cut*100:+.2f}% · 최고 수익 심볼 {top_s} 빼면 {ex_s*100:+.2f}% · 최고 날 빼면 {ex_d*100:+.2f}%")
    print(f"   ① t≥2.9 {'✅' if c1 else '❌'} · ② 반기 {'✅' if c2 else '❌'} · ③ 상위5% 빼도 {'✅' if c3 else '❌'} · ④ 심볼·날 {'✅' if c4 else '❌'}"
          f" → {'✅ 합격' if c1 and c2 and c3 and c4 else '❌ 불합격'}")
    print("\n━━ 참고(판정 아님) ━━")
    for th in THS:
        mk = np.isclose(thv, th)
        line = []
        for M in MS:
            for tr in (False, True):
                rr = trade(o1[mk], P[mk], M, tr, side=-1)
                line.append(f"{M}분{'추격' if tr else ''} {rr.mean()*100:+.2f}")
        print(f"  숏(되돌림) +{th*100:.0f}%: " + " · ".join(line))
    k3 = np.isclose(thv, 0.03)
    rr = trade(o1[k3], P[k3], 60, False)
    for lab, cuts, x in (("5분 수익", (0.03, 0.05, 0.10, 0.20, 9), r5[k3]), ("5분 거래대금($)", (1e5, 1e6, 1e7, 1e8, 1e12), q5[k3])):
        line = []
        for a, b_ in zip(cuts[:-1], cuts[1:]):
            k = (x >= a) & (x < b_)
            if k.sum():
                line.append(f"[{a:g}~{b_:g}) {rr[k].mean()*100:+.2f}%({k.sum()})")
        print(f"  {lab}별 60분 시간 청산 롱: " + " · ".join(line))


if __name__ == "__main__":
    main()
