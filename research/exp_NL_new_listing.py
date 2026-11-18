"""NL — 신규 상장 코인 숏: 바이낸스 USDT 무기한에 새로 상장된 코인은 상장 후 몇 주간 내리는 경향이 있나.

배경: 초기 유통량이 적고 평가액이 높은 토큰(에어드롭·런치풀)이 상장 후 매도 물량에 밀린다는 통념.
지표가 아니라 '상장'이라는 사건 기반. 숏이 내는/받는 펀딩까지 넣는다(신규 코인은 펀딩 음수가 흔해 숏이 낸다).

━━ 사전 고정(2026-11-18, 결과 보기 전에 작성 — 바꾸지 않는다) ━━
  표본   바이낸스 USDT 무기한(contractType PERPETUAL · 기초자산 COIN · 거래 중 + 폐지 SETTLING)
         onboardDate 2023-01-01 이후 · 상장 후 15일 이상 지난 것
  주 규칙 상장 + 24시간 뒤 첫 1h 종가에 숏 · 14일 보유 후 1h 종가 청산 · 손절 없음
         순손익 = −(청산/진입 − 1) + 보유 중 펀딩 합(숏 기준) − 비용 0.10%
  합격   ① 순평균 − 2×표준오차 > 0   ② 중앙값 > 0   ③ 상장일 2025-07 전·후 두 반쪽 모두 순평균 > 0
         셋 다 통과해야 ✅.
  참고(판정 아님 · 다중 비교라 걸러 볼 것): 진입 1·3·7일 × 보유 7·14·30일 × 손절 없음/+50%/+100%,
         꼬리(손실 +50% 초과 비율 · 최악 5건), 동시 보유 포트폴리오.

    python3 -u -m research.exp_NL_new_listing --collect     # 상장 목록 · 1h 봉(상장~+61일) · 펀딩(같은 구간)
    python3 -u -m research.exp_NL_new_listing
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np

from research.xv_data import DB

H = 3_600_000
DAY = 24 * H
SINCE = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
SPLIT = int(datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
FAPI = "https://fapi.binance.com/fapi/v1"
WINDOW = 61 * DAY
COST = 0.0010


def _get(path, params, retries=5):
    q = urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            req = urllib.request.Request(f"{FAPI}/{path}?{q}", headers={"User-Agent": "research-client/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
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
    c.execute("CREATE TABLE IF NOT EXISTS nl_listing (symbol TEXT PRIMARY KEY, onboard INTEGER, status TEXT, done INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS nl_1h (symbol TEXT, t INTEGER, open REAL, high REAL, low REAL, close REAL, "
              "qvol REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS nl_funding (symbol TEXT, t INTEGER, rate REAL, PRIMARY KEY (symbol, t))")
    return c


def collect():
    c = conn()
    info = _get("exchangeInfo", {})
    now = int(time.time() * 1000)
    for s in info["symbols"]:
        if (s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("underlyingType") == "COIN"
                and s.get("status") in ("TRADING", "SETTLING") and int(s.get("onboardDate", 0)) >= SINCE):
            c.execute("INSERT OR IGNORE INTO nl_listing VALUES (?,?,?,0)", (s["symbol"], int(s["onboardDate"]), s["status"]))
    c.commit()
    todo = [r for r in c.execute("SELECT symbol, onboard FROM nl_listing WHERE done=0 AND onboard <= ?", (now - 15 * DAY,))]
    print(f"상장 {c.execute('SELECT COUNT(*) FROM nl_listing').fetchone()[0]}개 · 받을 것 {len(todo)}개", flush=True)
    t0 = time.time()
    for n, (s, ob) in enumerate(todo, 1):
        end = min(ob + WINDOW, now)
        cur = ob - H
        while cur < end:
            rows = _get("klines", {"symbol": s, "interval": "1h", "startTime": cur, "endTime": end, "limit": 1500})
            if not rows:
                break
            c.executemany("INSERT OR REPLACE INTO nl_1h VALUES (?,?,?,?,?,?,?)",
                          [(s, int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7])) for k in rows])
            cur = int(rows[-1][0]) + H
            if len(rows) < 1500:
                break
            time.sleep(0.1)
        cur = ob - H
        while cur < end:
            rows = _get("fundingRate", {"symbol": s, "startTime": cur, "endTime": end, "limit": 1000})
            time.sleep(0.65)                                         # fundingRate 5분 500회 한도
            if not rows:
                break
            c.executemany("INSERT OR REPLACE INTO nl_funding VALUES (?,?,?)",
                          [(s, int(r["fundingTime"]), float(r["fundingRate"])) for r in rows])
            cur = int(rows[-1]["fundingTime"]) + 1
            if len(rows) < 1000:
                break
        c.execute("UPDATE nl_listing SET done=1 WHERE symbol=?", (s,))
        c.commit()
        if n % 25 == 0:
            el = time.time() - t0
            print(f"  {n}/{len(todo)}  경과 {el/60:.1f}분 · 남은 {el/n*(len(todo)-n)/60:.0f}분", flush=True)
    c.close()


def load():
    c = conn()
    lst = c.execute("SELECT symbol, onboard FROM nl_listing WHERE done=1 ORDER BY onboard").fetchall()
    data = {}
    for s, ob in lst:
        k = np.array(c.execute("SELECT t, open, high, close FROM nl_1h WHERE symbol=? ORDER BY t", (s,)).fetchall())
        f = np.array(c.execute("SELECT t, rate FROM nl_funding WHERE symbol=? ORDER BY t", (s,)).fetchall())
        if len(k) < 24:
            continue
        data[s] = (ob, k, f if len(f) else np.zeros((0, 2)))
    c.close()
    return data


def trade(ob, k, f, D, N, stop=None):
    """→ (진입 시각, 순손익) 또는 None. k: [t, open, high, close] (t = 봉 시작)."""
    close_t = k[:, 0] + H
    i = np.searchsorted(close_t, ob + D * DAY, side="left")
    if i >= len(k):
        return None
    e_t, e_p = close_t[i], k[i, 3]
    j = np.searchsorted(close_t, e_t + N * DAY, side="left")
    if j >= len(k):
        return None                                                    # 보유 기간을 다 못 채움(폐지·데이터 끝)
    x_t, x_p = close_t[j], k[j, 3]
    if stop is not None:
        lvl = e_p * (1 + stop)
        hit = np.where(k[i + 1:j + 1, 2] >= lvl)[0]
        if len(hit):
            b = i + 1 + hit[0]
            x_t, x_p = close_t[b], max(lvl, k[b, 1])                     # 갭이면 시가에 체결
    fund = f[(f[:, 0] > e_t) & (f[:, 0] <= x_t), 1].sum() if len(f) else 0.0
    return e_t, -(x_p / e_p - 1) + fund - COST, -(x_p / e_p - 1), fund


def main():
    if "--collect" in sys.argv:
        return collect()
    data = load()
    print(f"NL 신규 상장 숏 · 상장 {len(data)}개(2023-01~, 폐지 포함) · 비용 {COST*100:.2f}% · 값은 % · ±2se")

    def run(D, N, stop=None):
        out = []
        for s, (ob, k, f) in data.items():
            r = trade(ob, k, f, D, N, stop)
            if r:
                out.append((s, ob) + r)
        return out

    tr = run(1, 14)
    net = np.array([x[3] for x in tr]) * 100
    m, se = net.mean(), 2 * net.std() / np.sqrt(len(net))
    h0 = np.array([x[3] for x in tr if x[1] < SPLIT]) * 100
    h1 = np.array([x[3] for x in tr if x[1] >= SPLIT]) * 100
    ok1, ok2, ok3 = m - se > 0, np.median(net) > 0, h0.mean() > 0 and h1.mean() > 0
    print(f"\n━━ 주 규칙: 상장+24h 숏 · 14일 보유 · 손절 없음 · {len(net)}건 ━━")
    print(f"   가격 {np.mean([x[4] for x in tr])*100:+.2f} · 펀딩 {np.mean([x[5] for x in tr])*100:+.2f} → 순 {m:+.2f} ±{se:.2f}")
    print(f"① 순평균 − 2se = {m - se:+.2f}  → {'통과' if ok1 else '실패'}")
    print(f"② 중앙값 {np.median(net):+.2f} (이익 {np.mean(net > 0):.0%})  → {'통과' if ok2 else '실패'}")
    print(f"③ 전반 {h0.mean():+.2f} ({len(h0)}건) · 후반 {h1.mean():+.2f} ({len(h1)}건)  → {'통과' if ok3 else '실패'}")
    print(f"판정: {'✅ 통과' if ok1 and ok2 and ok3 else '❌ 실패'}")
    worst = sorted(tr, key=lambda x: x[3])[:5]
    print(f"   꼬리: 손실 50% 초과 {np.mean(net < -50):.1%} · 100% 초과 {np.mean(net < -100):.1%} · 최악 "
          + ", ".join(f"{x[0]} {x[3]*100:+.0f}%" for x in worst))

    print("\n— 참고(판정 아님 · 다중 비교) — 진입일 × 보유일 × 손절: 순평균 ±2se · 중앙 · [전반 | 후반]")
    for D in (1, 3, 7):
        for N in (7, 14, 30):
            for stop in (None, 0.5, 1.0):
                t = run(D, N, stop)
                if len(t) < 20:
                    continue
                v = np.array([x[3] for x in t]) * 100
                a0 = np.array([x[3] for x in t if x[1] < SPLIT]) * 100
                a1 = np.array([x[3] for x in t if x[1] >= SPLIT]) * 100
                print(f"   진입 {D}일 보유 {N:2d}일 손절 {('없음' if stop is None else f'+{stop:.0%}'):>5} | {len(v):4d}건 "
                      f"{v.mean():+6.2f} ±{2*v.std()/np.sqrt(len(v)):5.2f} · 중앙 {np.median(v):+6.2f} | "
                      f"{a0.mean():+6.2f} | {a1.mean():+6.2f}")


if __name__ == "__main__":
    main()
