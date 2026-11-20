"""UN — 업비트 공지(신규 원화 상장 등) 직후 바이낸스 선물로 따라타기 (사용자 요청 "업비트 공지 펌핑 체크", 2026-11-20).

업비트가 원화 마켓 신규 상장을 공지하면 해외 거래소에서 그 코인이 급등한다는 현상. 공지 → 감지(지연 L) → 바이낸스 USDT 무기한 롱 → M 뒤 청산.
반대 공지(유의 종목 지정·유의 촉구·거래지원 종료)는 급락 → 숏, 유의 해제는 롱 — 참고로만.

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
공지    업비트 거래 공지 전체(api-manager announcements, category=trade, 817건 2017-10~2026-11-19) · 시각 = first_listed_at(초 단위, 처음 게시).
분류    상장 = 제목에 (KRW 또는 원화) 와 (신규 거래지원 · 디지털 자산 추가 · 신규 상장 · 마켓 추가 · 코인 추가) — 종료·해제 포함 제목은 제외.
        코인마다 처음 공지 하나(같은 코인의 후속 안내 제외). 티커 = 제목의 대문자 2~10자(KRW·BTC·USDT 제외).
대상    그 공지 1일 전부터 바이낸스 USDT 무기한이 거래 중인 코인(xs_sym, base = 티커 또는 1000+티커). 2020-01 이후.
매매    공지 t0 + 지연 L 뒤 첫 '매수 주도' 체결가 롱 → t0 + M 직전 마지막 '매도 주도' 체결가 청산 · 왕복 0.20%.
격자    L ∈ {2초, 10초, 60초} × M ∈ {1분, 5분, 30분, 4시간} = 12.
판정    단위 = 공지(한 공지에 코인 여럿이면 평균). ① 12개 중 최고 t ≥ 2.6  ② 2024 · 2025 · 2026 각 > 0
        ③ 상위 10% 공지 빼도 > 0  ④ 중앙 > 0.
참고(판정 아님) 반응 곡선(t0−10분 ~ +4시간 평균 경로) · 급등 시작 시각 분포(공지 시각이 정확한지 점검 — 급등이 t0 전에 시작하면
        시각이 늦게 찍혔거나 정보가 샌 것, 한참 뒤면 시각이 이르게 찍힌 것 = 진입이 실제보다 이르게 계산돼 낙관) ·
        BTC·USDT 마켓만 상장 · 유의 지정/촉구·종료(숏) · 유의 해제(롱).
데이터  data.binance.vision 일별 aggTrades(공지 날짜, 4시간이 날을 넘기면 다음 날도) — t0−10분 ~ t0+30분은 체결 전부,
        30분 ~ 4시간은 1분마다 마지막 매수·매도 주도 체결가.

    python3 -u -m research.exp_UN_upbit_notice --collect
    python3 -u -m research.exp_UN_upbit_notice
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np

from research.xv_data import DB

S, MIN, H, D = 1000, 60_000, 3_600_000, 86_400_000
PRE, FULL, END = 10 * MIN, 30 * MIN, 4 * H
LS, MS = (2 * S, 10 * S, 60 * S), (MIN, 5 * MIN, 30 * MIN, 4 * H)
COST = 0.0020
Y2020 = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
SKIP = {"KRW", "BTC", "USDT", "USDC", "API", "NFT", "DAO", "ETF"}


def conn():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("CREATE TABLE IF NOT EXISTS un_notice (id INTEGER PRIMARY KEY, t INTEGER, kind TEXT, title TEXT, tickers TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS un_tr (nid INTEGER, symbol TEXT, ms BLOB, px BLOB, buy BLOB, mb BLOB, ms_ BLOB, status INTEGER,"
              " PRIMARY KEY (nid, symbol))")
    return c


def kind(title):
    t = title
    if ("종료" in t and "디지털 자산 추가" not in t and "신규 거래지원" not in t) or "해제" in t:
        if "해제" in t:
            return "유의해제"
        return "거래지원종료"
    if ("KRW" in t or "원화" in t) and any(k in t for k in ("신규 거래지원", "디지털 자산 추가", "신규 상장", "마켓 추가", "코인 추가")):
        return "상장KRW"
    if any(k in t for k in ("신규 거래지원", "디지털 자산 추가", "마켓 코인 추가", "마켓 추가")):
        return "상장기타"
    if "유의 종목 지정" in t and "연장" not in t:
        return "유의지정"
    if "유의 촉구" in t:
        return "유의촉구"
    return "기타"


def tickers(title):
    return [x for x in dict.fromkeys(re.findall(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9})(?![A-Za-z0-9])", title)) if x not in SKIP]


def fetch_notices():
    out, p = [], 1
    while True:
        req = urllib.request.Request(f"https://api-manager.upbit.com/api/v1/announcements?os=web&page={p}&per_page=20&category=trade",
                                     headers={"User-Agent": "Mozilla/5.0"})
        d = json.load(urllib.request.urlopen(req, timeout=30))["data"]
        out += d["notices"]
        if p >= d["total_pages"]:
            break
        p += 1
        time.sleep(0.3)
    return out


def _stream(sym, day, a, b):
    """그날 aggTrades 중 [a, b) — (ms, px, buy) 리스트."""
    ds = datetime.fromtimestamp(day / 1000, timezone.utc).strftime("%Y-%m-%d")
    url = f"https://data.binance.vision/data/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{ds}.zip"
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research/0.1"}), timeout=300).read()
    except urllib.error.HTTPError as e:
        return None, e.code
    rows = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z, z.open(z.namelist()[0]) as f:
        for line in io.TextIOWrapper(f):
            if line[:1].isalpha():
                continue
            p = line.split(",")
            tt = int(p[5])
            if tt < a:
                continue
            if tt >= b:
                break
            rows.append((tt, float(p[1]), p[6].strip().lower() == "false"))
    return rows, 200


def grab(nid, t0, sym):
    a, b = t0 - PRE, t0 + END
    rows, st = [], 200
    for day in sorted({a // D * D, (b - 1) // D * D}):
        r, st = _stream(sym, day, a, b)
        if r is None:
            return None, st
        rows += r
    if not rows:
        return None, 204
    ms = np.array([x[0] - t0 for x in rows], dtype=np.int64)
    px = np.array([x[1] for x in rows])
    buy = np.array([x[2] for x in rows], dtype=np.bool_)
    full = ms < FULL
    # 30분 이후: 1분마다 마지막 매수·매도 주도 체결가 (k = 30..239)
    mb = np.full((210, 2), np.nan)
    late = ~full
    if late.any():
        k = (ms[late] // MIN - 30).astype(int)
        for side, col in ((True, 0), (False, 1)):
            sel = buy[late] == side
            for kk, pp in zip(k[sel], px[late][sel]):
                if 0 <= kk < 210:
                    mb[kk, col] = pp
    return (ms[full].astype(np.int32), px[full], buy[full], mb), 200


def collect():
    c = conn()
    ns = fetch_notices()
    for n in ns:
        t0 = int(datetime.fromisoformat(n["first_listed_at"]).timestamp() * 1000)
        c.execute("INSERT OR REPLACE INTO un_notice VALUES (?,?,?,?,?)", (n["id"], t0, kind(n["title"]), n["title"], ",".join(tickers(n["title"]))))
    c.commit()
    sym = {}
    for s, b, ob in c.execute("SELECT symbol, base, onboard FROM xs_sym"):
        sym.setdefault(b, []).append((s, ob))
    seen, jobs = set(), []
    for nid, t0, k, title, tk in c.execute("SELECT id, t, kind, title, tickers FROM un_notice WHERE t >= ? ORDER BY t", (Y2020,)):
        if k == "기타" or not tk:
            continue
        for x in tk.split(","):
            if k.startswith("상장"):
                if (k, x) in seen:                         # 같은 코인의 후속 상장 안내는 제외
                    continue
                seen.add((k, x))
            for base in (x, "1000" + x):
                for s, ob in sym.get(base, []):
                    if s.endswith("USDT") and ob and ob <= t0 - D:
                        jobs.append((nid, t0, s))
    have = {(a, b) for a, b in c.execute("SELECT nid, symbol FROM un_tr")}
    jobs = [j for j in jobs if (j[0], j[2]) not in have]
    print(f"공지 {len(ns)} · 바이낸스 선물 매칭 받을 것 {len(jobs)}", flush=True)

    def work(j):
        try:
            return j, *grab(*j)
        except Exception as e:      # noqa: BLE001
            print(f"  ⚠ {j}: {e}", flush=True)
            return j, None, -1
    n = 0
    with ThreadPoolExecutor(6) as ex:
        for f in as_completed([ex.submit(work, j) for j in jobs]):
            (nid, t0, s), arr, st = f.result()
            n += 1
            if st == -1:
                continue
            blobs = [x.tobytes() for x in arr] if arr is not None else [b""] * 4
            c.execute("INSERT OR REPLACE INTO un_tr VALUES (?,?,?,?,?,?,?,?)", (nid, s, blobs[0], blobs[1], blobs[2], blobs[3], b"", st))
            c.commit()
            if n % 20 == 0:
                print(f"  {n}/{len(jobs)}", flush=True)
    c.close()


def load():
    c = conn()
    out = []
    for nid, t0, k, title, s, m, p, b, mb in c.execute(
            "SELECT n.id, n.t, n.kind, n.title, r.symbol, r.ms, r.px, r.buy, r.mb FROM un_notice n JOIN un_tr r ON r.nid = n.id WHERE r.status=200"):
        if not m:
            continue
        out.append(dict(nid=nid, t0=t0, kind=k, title=title, sym=s, ms=np.frombuffer(m, dtype=np.int32), px=np.frombuffer(p),
                        buy=np.frombuffer(b, dtype=np.bool_), mb=np.frombuffer(mb).reshape(210, 2)))
    c.close()
    return out


def price_at(e, tm, side_buy, first_after=False):
    """tm(ms, t0 기준) 직전 마지막(또는 직후 첫) side 체결가."""
    if tm > FULL or (tm == FULL and first_after):          # 30분 정각 '직전'은 체결 전부 구간에서 찾는다
        k = tm // MIN - 30 - (0 if first_after else 1)
        col = 0 if side_buy else 1
        while 0 <= k < 210:
            v = e["mb"][k, col]
            if v == v:
                return v
            k += 1 if first_after else -1
        return None
    ms, px, buy = e["ms"], e["px"], e["buy"]
    sel = buy == side_buy
    if first_after:
        i = np.where(sel & (ms >= tm))[0]
        return px[i[0]] if len(i) else None
    i = np.where(sel & (ms <= tm))[0]
    return px[i[-1]] if len(i) else None


def trade(e, L, M, side=1):
    ent = price_at(e, L, side == 1, first_after=True)
    ex = price_at(e, M, side != 1)
    if ent is None or ex is None:
        return None
    return side * (ex / ent - 1) - COST


def by_notice(evs, L, M, side=1):
    g = {}
    for e in evs:
        r = trade(e, L, M, side)
        if r is not None:
            g.setdefault(e["nid"], (e["t0"], []))[1].append(r)
    return {k: (t0, float(np.mean(v))) for k, (t0, v) in g.items()}


def tstat(v):
    v = np.asarray(v)
    return v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 2 and v.std() > 0 else float("nan")


def yr(t):
    return datetime.fromtimestamp(t / 1000, timezone.utc).year


def main():
    if "--collect" in sys.argv:
        return collect()
    evs = load()
    krw = [e for e in evs if e["kind"] == "상장KRW"]
    print(f"매칭된 공지·심볼: {len(evs)} · 원화 상장 {len(krw)}(공지 {len({e['nid'] for e in krw})}) · 연도 "
          + " ".join(f"{y}:{sum(1 for e in krw if yr(e['t0'])==y)}" for y in range(2020, 2027)))
    res = []
    for L in LS:
        for M in MS:
            g = by_notice(krw, L, M)
            v = np.array([x[1] for x in g.values()]); ys = np.array([yr(x[0]) for x in g.values()])
            res.append(dict(L=L, M=M, g=g, v=v, ys=ys, t=tstat(v)))
    print("\n원화 상장 공지 → 바이낸스 선물 롱 (공지 단위, 비용 0.20%)")
    print(" 지연 · 보유 | 공지 | 평균 | 중앙 | 이익 | t | 2024 · 2025 · 2026")
    for x in res:
        v, ys = x["v"], x["ys"]
        yy = " · ".join(f"{v[ys==y].mean()*100:+.2f}({(ys==y).sum()})" if (ys == y).any() else "-" for y in (2024, 2025, 2026))
        print(f"  {x['L']//1000:2d}초 · {x['M']//60000:3d}분 | {len(v):3d} | {v.mean()*100:+6.2f}% | {np.median(v)*100:+6.2f} | {np.mean(v>0):.0%} | {x['t']:+5.2f} | {yy}")
    b = max(res, key=lambda x: x["t"])
    v, ys = b["v"], b["ys"]
    srt = np.sort(v)
    c1 = b["t"] >= 2.6
    c2 = all((ys == y).any() and v[ys == y].mean() > 0 for y in (2024, 2025, 2026))
    c3 = srt[: len(v) - max(1, len(v) // 10)].mean() > 0
    c4 = np.median(v) > 0
    print(f"\n■ 최고: 지연 {b['L']//1000}초 · 보유 {b['M']//60000}분 — {v.mean()*100:+.2f}%/공지 · t {b['t']:+.2f} · 최악 {v.min()*100:+.1f}% · 최고 {v.max()*100:+.1f}%")
    print(f"   상위 10% 빼면 {srt[:len(v)-max(1,len(v)//10)].mean()*100:+.2f}% · 중앙 {np.median(v)*100:+.2f}%")
    print(f"   ① t≥2.6 {'✅' if c1 else '❌'} · ② 2024·25·26 {'✅' if c2 else '❌'} · ③ 상위 10% 빼도 {'✅' if c3 else '❌'} · ④ 중앙 {'✅' if c4 else '❌'}"
          f" → {'✅ 합격' if c1 and c2 and c3 and c4 else '❌ 불합격'}")
    # 참고: 반응 곡선 · 급등 시작
    print("\n━━ 참고(판정 아님) ━━")
    marks = [-10 * MIN, -MIN, -10 * S, 0, 1 * S, 2 * S, 5 * S, 10 * S, 30 * S, MIN, 5 * MIN, 30 * MIN, H, 4 * H]
    for ys_, lab in ((None, "전체"), (2024, "2024"), (2025, "2025"), (2026, "2026")):
        sub = [e for e in krw if ys_ is None or yr(e["t0"]) == ys_]
        if not sub:
            continue
        cur = []
        for mk in marks:
            rr = []
            for e in sub:
                p0 = price_at(e, -10 * S, False) or price_at(e, -10 * S, True)
                p1 = price_at(e, mk, False) or price_at(e, mk, True)
                if p0 and p1:
                    rr.append(p1 / p0 - 1)
            cur.append(np.median(rr) if rr else np.nan)
        print(f"  반응 곡선 중앙(t0−10초 = 0) {lab:4}: " + " · ".join(
            f"{('%+d' % (mk//1000))+'s' if abs(mk) < MIN else ('%+dm' % (mk//MIN))} {x*100:+.1f}" for mk, x in zip(marks, cur)))
    onset = []
    for e in krw:
        p0 = price_at(e, -10 * S, False) or price_at(e, -10 * S, True)
        if not p0:
            continue
        hit = np.where((e["px"] >= p0 * 1.02) & (e["ms"] >= -10 * S))[0]
        onset.append(e["ms"][hit[0]] / 1000 if len(hit) else np.nan)
    on = np.array(onset)
    ok = on[np.isfinite(on)]
    print(f"  급등(+2%) 시작 시각(초, t0 기준): {len(ok)}/{len(on)}건 · 분위 10/25/50/75/90% = "
          + " / ".join(f"{np.percentile(ok, q):+.1f}" for q in (10, 25, 50, 75, 90)) + f" · t0 전에 시작 {np.mean(ok < 0):.0%}")
    print("  지연 민감도(원화 상장 · 롱 · 공지 평균, 비용 0.20%):")
    for M in (MIN, 5 * MIN):
        for ys_ in (None, 2024, 2025, 2026):
            sub = [e for e in krw if ys_ is None or yr(e["t0"]) == ys_]
            line = []
            for L in (500, 1000, 1500, 2000, 3000, 5000, 10000):
                vv = np.array([x[1] for x in by_notice(sub, L, M).values()])
                line.append(f"{L/1000:g}초 {vv.mean()*100:+.1f}")
            print(f"    보유 {M//60000}분 · {ys_ or '전체'}: " + " · ".join(line))
    for k, side, lab in (("상장기타", 1, "BTC·USDT 마켓만 상장 · 롱"), ("유의지정", -1, "유의 종목 지정 · 숏"), ("유의촉구", -1, "유의 촉구 · 숏"),
                         ("거래지원종료", -1, "거래지원 종료 · 숏"), ("유의해제", 1, "유의 해제 · 롱")):
        sub = [e for e in evs if e["kind"] == k]
        if not sub:
            continue
        line = []
        for M in (5 * MIN, 30 * MIN, 4 * H):
            g = by_notice(sub, 10 * S, M, side)
            vv = np.array([x[1] for x in g.values()])
            line.append(f"{M//60000}분 {vv.mean()*100:+.2f}%(t {tstat(vv):+.2f}, {len(vv)})" if len(vv) else f"{M//60000}분 -")
        print(f"  {lab}: 지연 10초 · " + " · ".join(line))


if __name__ == "__main__":
    main()
