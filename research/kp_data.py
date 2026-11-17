"""KP — 김프 연구용 수집: 업비트 원화 1시간봉 + 원·달러 기준환율(일별). 공개 API, 키 불필요.

  업비트   https://api.upbit.com/v1/candles/minutes/60   (to·count=200, 최신→과거)
  환율     https://api.frankfurter.app  (ECB 기준환율, 영업일만 · 주말·휴일은 직전 값으로 채워 쓴다)
  환율 1h  query1.finance.yahoo.com KRW=X (외환시장 시세 1시간봉, 최근 ~730일 — 2024-01~)
바이낸스 쪽 1시간봉은 candles.db 의 <SYM>USDT_1H(xs_data), 펀딩은 xvenue.db(xv_data)를 그대로 쓴다.

저장: data/xvenue.db 의 upbit_1h(coin, t, close) · fx(day, usdkrw). 다시 돌려도 안전(INSERT OR REPLACE).

    python3 -u -m research.kp_data
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from research.xv_data import DB, UA

COINS = ("BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "LINK", "AVAX")
SINCE = datetime(2021, 1, 1, tzinfo=timezone.utc)


def _get(url, retries=5):
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(1.0 * (i + 1))


def conn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS upbit_1h (coin TEXT, t INTEGER, close REAL, PRIMARY KEY (coin, t))")
    c.execute("CREATE TABLE IF NOT EXISTS fx (day TEXT PRIMARY KEY, usdkrw REAL)")
    return c


def upbit(c, coin):
    to, n = datetime.now(timezone.utc), 0
    while to > SINCE:
        rows = _get(f"https://api.upbit.com/v1/candles/minutes/60?market=KRW-{coin}"
                    f"&to={to.strftime('%Y-%m-%dT%H:%M:%SZ')}&count=200")
        if not rows:
            break
        vals = []
        for r in rows:
            t = datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
            vals.append((coin, int(t.timestamp() * 1000), float(r["trade_price"])))
        c.executemany("INSERT OR REPLACE INTO upbit_1h VALUES (?,?,?)", vals)
        c.commit()
        n += len(vals)
        oldest = datetime.fromisoformat(rows[-1]["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
        if oldest >= to or len(rows) < 200:
            break
        to = oldest
        time.sleep(0.12)                                   # 업비트 시세 API 초당 10회 제한
    return n


def fx(c):
    a, n = SINCE.date(), 0
    end = datetime.now(timezone.utc).date()
    while a <= end:
        b = min(a + timedelta(days=360), end)
        r = _get(f"https://api.frankfurter.app/{a}..{b}?from=USD&to=KRW")
        c.executemany("INSERT OR REPLACE INTO fx VALUES (?,?)", [(d, v["KRW"]) for d, v in r["rates"].items()])
        c.commit()
        n += len(r["rates"])
        a = b + timedelta(days=1)
    return n


def fx_hourly(c):
    """원·달러 1시간봉(외환시장 시세, 최근 ~730일만 제공) → fx_1h(t, usdkrw). t = 봉 시작(UTC 정시)."""
    c.execute("CREATE TABLE IF NOT EXISTS fx_1h (t INTEGER PRIMARY KEY, usdkrw REAL)")
    d = _get("https://query1.finance.yahoo.com/v8/finance/chart/KRW=X?interval=1h&range=730d")
    r = d["chart"]["result"][0]
    rows = [(int(t) // 3600 * 3_600_000, float(v)) for t, v in zip(r["timestamp"], r["indicators"]["quote"][0]["close"])
            if v is not None]
    c.executemany("INSERT OR REPLACE INTO fx_1h VALUES (?,?)", rows)
    c.commit()
    return len(rows)


def main():
    c = conn()
    print(f"환율 1시간 {fx_hourly(c):,}봉", flush=True)
    print(f"환율 {fx(c):,}영업일", flush=True)
    for coin in COINS:
        print(f"  업비트 KRW-{coin:5} {upbit(c, coin):7,}봉", flush=True)
    c.close()


if __name__ == "__main__":
    main()
