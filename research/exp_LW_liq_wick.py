"""LW — 대량 청산 꼬리 되사기(사용자 선택 B4, 2026-11-18). 짧은 시간 급락(거래량 폭증 동반) 직후 롱 → 1시간 뒤 청산.

가설: 연쇄 강제 청산은 '팔고 싶은 사람'이 아니라 '팔려 나간 사람'의 물량이라 가격을 과하게 누른다 → 청산이 끝나면 되돌림.
반론: 진짜 악재(해킹·폐지·언락)면 계속 떨어진다. 과거 청산 기록은 공개 안 됨 → 분봉 급락 + 거래량 폭증으로 대신 판별.

━━ 사전 고정 (2026-11-18 작성 · 결과 보기 전) ━━
데이터  data.binance.vision 선물 월별 5분봉 · USDT 무기한 COIN 전체(M 포함, 폐지 SETTLING 포함 — 생존 편향 방지) · 2024-01~2026-10.
사건    5분봉 i 종가 기준 **급락 = 종가 / 최근 3봉(i−2..i) 최고가 − 1 ≤ −10%** (15분 안에 −10%)
        그리고 거래량 폭증 = 최근 3봉 거래대금 합 ≥ 5 × (직전 24h 5분봉 거래대금 중앙 × 3)
        그리고 유동성 = 직전 24h 5분봉 거래대금 중앙 ≥ $20,000.  같은 심볼 사건 뒤 4시간은 새 사건 안 봄.
거래    다음 봉(i+1) 시가 롱 → **i+12 봉 종가(1시간 보유)** 청산. 1배. 비용 왕복 0.20%(수수료 0.10 + 급변 중 슬리피지 0.10).
합격(넷 다)  ① 평균 − 2·se > 0  ② 중앙값 > 0
             ③ 최다 심볼 빼도 · 사건 최다 날짜(시장 전체 폭락일) 빼도 평균 > 0, 그리고 심볼별 평균의 중앙값 > 0
             ④ 반기별 2025-H2 · 2026-H1 · 2026-H2 평균이 모두 > 0
참고(판정 아님)  보유 15분·4h·24h · 문턱 −5%·−20% · 손절 −5% · 급등(+10%) 숏 대칭 · BTC 같이 급락한 날 vs 단독 급락.

    python3 -u -m research.exp_LW_liq_wick --collect   # 5분봉 받아 사건·창만 저장(원본은 버림)
    python3 -u -m research.exp_LW_liq_wick             # 결과
"""
from __future__ import annotations

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

from research.exp_FH5_post_trend import _get
from research.xv_data import DB

M5 = 300_000
DAY_BARS = 288
START = (2024, 1)
END = (2026, 10)
COST = 0.0020
PRE, POST = 3, 288                      # 저장 창: 사건 전 3봉 ~ 후 288봉(24h)
LOOSE = 0.05                             # 저장은 ±5% 사건까지(변형용), 판정은 −10%
VOLX, MINQ, COOL = 5.0, 20_000.0, 48
HALVES = [("2024-H1", 2024, 1), ("2024-H2", 2024, 7), ("2025-H1", 2025, 1), ("2025-H2", 2025, 7), ("2026-H1", 2026, 1), ("2026-H2", 2026, 7)]


def _ms(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def conn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS lw_event (symbol TEXT, t INTEGER, move REAL, volx REAL, medq REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS lw_5m (symbol TEXT, ev INTEGER, k INTEGER, open REAL, high REAL, low REAL, close REAL, "
              "PRIMARY KEY (symbol, ev, k))")
    c.execute("CREATE TABLE IF NOT EXISTS lw_done (symbol TEXT PRIMARY KEY, months INTEGER, bars INTEGER)")
    return c


def _months(onboard):
    y, m = START
    oy, om = (datetime.fromtimestamp(onboard / 1000, timezone.utc).year, datetime.fromtimestamp(onboard / 1000, timezone.utc).month)
    if (oy, om) > (y, m):
        y, m = oy, om
    out = []
    while (y, m) <= END:
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _zip(sym, ym):
    u = f"https://data.binance.vision/data/futures/um/monthly/klines/{sym}/5m/{sym}-5m-{ym}.zip"
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "research-client/0.1"}), timeout=120) as r:
                raw = r.read()
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                return z.read(z.namelist()[0])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == 3:
                raise
        except Exception:
            if i == 3:
                raise
        time.sleep(2 * (i + 1))


def _symbol(sym, onboard):
    rows = []
    n_m = 0
    for ym in _months(onboard):
        data = _zip(sym, ym)
        if data is None:
            continue
        n_m += 1
        for ln in data.split(b"\n"):
            if not ln[:1].isdigit():
                continue
            f = ln.split(b",", 8)
            rows.append((int(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[4]), float(f[7])))
    if not rows:
        return sym, n_m, 0, [], []
    a = np.array(sorted(set(rows)))
    t, o, h, l, c, q = a.T
    n = len(t)
    ev, win = [], []
    if n > DAY_BARS + 3:
        q3 = np.convolve(q, np.ones(3), "full")[:n]
        hmax = np.maximum(np.maximum(h, np.r_[h[0], h[:-1]]), np.r_[h[:2], h[:-2]])
        lmin = np.minimum(np.minimum(l, np.r_[l[0], l[:-1]]), np.r_[l[:2], l[:-2]])
        down = c / hmax - 1
        up = c / lmin - 1
        cand = np.where(((down <= -LOOSE) | (up >= LOOSE)) & (np.arange(n) >= DAY_BARS))[0]
        for i in cand:
            medq = float(np.median(q[i - DAY_BARS:i]))
            if medq <= 0:
                continue
            vx = q3[i] / (3 * medq)
            if vx < VOLX:
                continue
            mv = down[i] if down[i] <= -LOOSE else up[i]
            ev.append((sym, int(t[i]), float(mv), float(vx), medq))
            for k in range(-PRE, POST + 1):
                j = i + k
                if 0 <= j < n and t[j] - t[i] == k * M5:
                    win.append((sym, int(t[i]), k, float(o[j]), float(h[j]), float(l[j]), float(c[j])))
    return sym, n_m, n, ev, win


def collect():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    c = conn()
    info = _get("exchangeInfo", {})
    syms = [(s["symbol"], int(s.get("onboardDate", 0))) for s in info["symbols"]
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("underlyingType") == "COIN"
            and s.get("status") in ("TRADING", "SETTLING") and int(s.get("onboardDate", 0)) < _ms(2026, 11)]
    done = {r[0] for r in c.execute("SELECT symbol FROM lw_done")}
    todo = [x for x in syms if x[0] not in done]
    print(f"심볼 {len(syms)}개 · 받을 것 {len(todo)}개", flush=True)
    t0, k = time.time(), 0
    with ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(_symbol, s, ob): s for s, ob in todo}
        for fu in as_completed(futs):
            s = futs[fu]
            try:
                sym, n_m, n, ev, win = fu.result()
            except Exception as e:
                print(f"  {s} 실패 {e}", flush=True)
                continue
            c.executemany("INSERT OR REPLACE INTO lw_event VALUES (?,?,?,?,?)", ev)
            c.executemany("INSERT OR REPLACE INTO lw_5m VALUES (?,?,?,?,?,?,?)", win)
            c.execute("INSERT OR REPLACE INTO lw_done VALUES (?,?,?)", (sym, n_m, n))
            c.commit()
            k += 1
            if k % 25 == 0:
                el = time.time() - t0
                print(f"  {k}/{len(todo)}  경과 {el/60:.1f}분 · 남은 {el/k*(len(todo)-k)/60:.0f}분", flush=True)
    c.close()


def load():
    c = conn()
    E = list(c.execute("SELECT symbol, t, move, volx, medq FROM lw_event ORDER BY symbol, t"))
    W = defaultdict(dict)
    for s, ev, k, o, h, l, cl in c.execute("SELECT symbol, ev, k, open, high, low, close FROM lw_5m"):
        W[(s, ev)][k] = (o, h, l, cl)
    c.close()
    return E, W


def trades(E, W, th=-0.10, hold=12, stop=None, side=+1, volx=VOLX, minq=MINQ):
    out, last = [], {}
    for s, t, mv, vx, mq in E:
        if (side > 0 and mv > th) or (side < 0 and mv < -th) or vx < volx or mq < minq:
            continue
        if s in last and t - last[s] < COOL * M5:
            continue
        w = W.get((s, t))
        if not w or 1 not in w or hold not in w:
            continue
        last[s] = t
        ep = w[1][0]
        xp, why = w[hold][3], "시간"
        if stop is not None:
            for k in range(1, hold + 1):
                if k in w and side * (w[k][2 if side > 0 else 1] / ep - 1) <= stop:
                    xp, why = ep * (1 + side * stop), "손절"
                    break
        out.append(dict(s=s, t=t, mv=mv, r=side * (xp / ep - 1) - COST, why=why,
                        day=datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d")))
    return out


def judge(tr, title):
    if len(tr) < 3:
        print(f"\n■ {title}: {len(tr)}건")
        return False
    v = np.array([x["r"] for x in tr])
    se = v.std(ddof=1) / np.sqrt(len(v))
    per, byday = defaultdict(list), defaultdict(list)
    for x in tr:
        per[x["s"]].append(x["r"])
        byday[x["day"]].append(x["r"])
    top = max(per, key=lambda k: len(per[k]))
    tday = max(byday, key=lambda k: len(byday[k]))
    no_top = np.array([x["r"] for x in tr if x["s"] != top])
    no_day = np.array([x["r"] for x in tr if x["day"] != tday])
    sym_med = np.median([np.mean(a) for a in per.values()])
    halves = []
    for i, (nm, y, m) in enumerate(HALVES):
        a = _ms(y, m)
        b = _ms(*HALVES[i + 1][1:]) if i + 1 < len(HALVES) else 1 << 62
        halves.append((nm, np.array([x["r"] for x in tr if a <= x["t"] < b])))
    c1, c2 = v.mean() - 2 * se > 0, np.median(v) > 0
    c3 = no_top.mean() > 0 and no_day.mean() > 0 and sym_med > 0
    c4 = all(len(h) and h.mean() > 0 for _, h in halves[3:])
    print(f"\n■ {title} · {len(v):,}건 · {len(per)}심볼 · {len(byday)}일")
    print(f"   거래당 순 {v.mean()*100:+.2f} ±{se*100:.2f}% · 중앙 {np.median(v)*100:+.2f}% · 이익 {np.mean(v>0):.0%}")
    print(f"   ① {'✅' if c1 else '❌'} · ② {'✅' if c2 else '❌'} · ③ 최다 {top}({len(per[top])}) 빼면 {no_top.mean()*100:+.2f} · "
          f"최다 날 {tday}({len(byday[tday])}) 빼면 {no_day.mean()*100:+.2f} · 심볼 중앙 {sym_med*100:+.2f} {'✅' if c3 else '❌'} · "
          f"④ {'✅' if c4 else '❌'}")
    print("   반기: " + " · ".join(f"{nm} {h.mean()*100:+.2f}({len(h)})" if len(h) else f"{nm} -" for nm, h in halves))
    q = np.percentile(v, [1, 5, 95, 99]) * 100
    print(f"   분포 1% {q[0]:+.1f} · 5% {q[1]:+.1f} · 95% {q[2]:+.1f} · 99% {q[3]:+.1f} · 최악 {v.min()*100:+.1f} · 최고 {v.max()*100:+.1f}")
    return c1 and c2 and c3 and c4


def main():
    if "--collect" in sys.argv:
        collect()
        return
    E, W = load()
    ok = judge(trades(E, W), "사전 고정 · 15분 −10% 급락 + 거래량 5배 → 롱 1시간 · 비용 0.20%")
    print(f"\n판정: {'✅ 합격' if ok else '❌ 불합격'}")
    print("\n──── 참고(판정 아님) ────")
    for h, nm in ((3, "15분"), (48, "4시간"), (288, "24시간")):
        judge(trades(E, W, hold=h), f"보유 {nm}")
    for th in (-0.05, -0.20):
        judge(trades(E, W, th=th), f"문턱 {th*100:.0f}%")
    judge(trades(E, W, stop=-0.05), "손절 −5%")
    judge(trades(E, W, th=0.10, side=-1), "급등 +10% → 숏 1시간")
    judge(trades(E, W, th=0.10, side=-1, hold=48), "급등 +10% → 숏 4시간")
    # BTC 와 같이 빠진 날 vs 단독
    tr = trades(E, W)
    btc = {x["day"] for x in trades(E, W, th=-0.05, minq=0) if x["s"] == "BTCUSDT"}
    multi = defaultdict(int)
    for x in tr:
        multi[x["t"] // 3_600_000] += 1
    judge([x for x in tr if multi[x["t"] // 3_600_000] >= 5], "같은 1시간에 5심볼 이상 급락(시장 전체)")
    judge([x for x in tr if multi[x["t"] // 3_600_000] == 1], "단독 급락")


if __name__ == "__main__":
    main()
