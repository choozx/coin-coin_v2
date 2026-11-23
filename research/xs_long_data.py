"""XSL 데이터 — USDT 무기한 '지금까지 있었던 전부'(폐지 포함) 일봉·펀딩 2019-09 ~ 2026-11-16 (사용자: "기간 넓혀서 다시 해봐", 2026-11-23).

기존 XS 데이터(xvenue.db xs_1d)는 지금 exchangeInfo 에 남은 심볼만이라 오래전 폐지 코인(LUNA·FTT 등)이 빠졌다.
여기선 data.binance.vision 의 klines 폴더 목록(폐지 포함 전체 심볼)을 심볼 목록으로 쓰고, 값은 REST(폐지 심볼도 과거가 나온다)로 받는다.
저장: data/xs_long.db — sym(symbol, first) · k1d(symbol, t, close, qvol) · fund(symbol, t, rate) (펀딩은 (심볼, 시각) 하나만 — 중복 없음).

    .venv/bin/python -u research/xs_long_data.py
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.xs_mom import STABLE  # noqa: E402
from research.exp_FH5_post_trend import _get as _get0  # noqa: E402

D = 86_400_000
FROM = int(datetime(2019, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
END = int(datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp() * 1000)
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "xs_long.db")
INDEX = {"BTCDOMUSDT", "DEFIUSDT", "BLUEBIRDUSDT", "FOOTBALLUSDT"}   # 지수 계약(코인 아님)
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/futures/um/monthly/klines/"


def _get(path, params):
    for i in range(5):                                          # 읽기 시간 초과(socket.timeout)는 _get0 이 안 잡는다
        try:
            return _get0(path, params)
        except OSError:
            time.sleep(10 * (i + 1))
    return _get0(path, params)


def conn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS sym (symbol TEXT PRIMARY KEY, first INTEGER, done_k INTEGER DEFAULT 0, done_f INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS k1d (symbol TEXT, t INTEGER, close REAL, qvol REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS fund (symbol TEXT, t INTEGER, rate REAL, PRIMARY KEY (symbol, t))")
    return c


def all_symbols():
    out, marker = [], ""
    while True:
        with urllib.request.urlopen(S3 + (f"&marker={marker}" if marker else ""), timeout=30) as r:
            x = r.read().decode()
        out += re.findall(r"<Prefix>data/futures/um/monthly/klines/([^/<]+)/</Prefix>", x)
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", x)
        if "<IsTruncated>true</IsTruncated>" not in x or not m:
            break
        marker = m.group(1)
    info = _get("exchangeInfo", {})
    non_coin = {s["symbol"] for s in info["symbols"] if s.get("underlyingType") not in (None, "COIN")}
    keep = []
    for s in out:
        if not s.endswith("USDT") or "_" in s or s in INDEX or s in non_coin:
            continue
        base = re.sub(r"^1000+", "", s[:-4])
        if s[:-4] in STABLE or base in STABLE:
            continue
        keep.append(s)
    return sorted(set(keep))


def main():
    c = conn()
    syms = all_symbols()
    for s in syms:
        c.execute("INSERT OR IGNORE INTO sym (symbol) VALUES (?)", (s,))
    c.commit()
    print(f"심볼 {len(syms)}개", flush=True)
    todo = [r[0] for r in c.execute("SELECT symbol FROM sym WHERE done_k=0")]
    for n, s in enumerate(todo, 1):
        cur, first = FROM, None
        while cur < END + D:
            rows = _get("klines", {"symbol": s, "interval": "1d", "startTime": cur, "endTime": END + D, "limit": 499})
            time.sleep(0.1)
            if not rows:
                break
            first = first or int(rows[0][0])
            c.executemany("INSERT OR REPLACE INTO k1d VALUES (?,?,?,?)", [(s, int(k[0]), float(k[4]), float(k[7])) for k in rows])
            cur = int(rows[-1][0]) + D
            if len(rows) < 499:
                break
        c.execute("UPDATE sym SET done_k=1, first=? WHERE symbol=?", (first, s))
        c.commit()
        if n % 100 == 0:
            print(f"  일봉 {n}/{len(todo)}", flush=True)
    todo = [r for r in c.execute("SELECT symbol, first FROM sym WHERE done_f=0 AND first IS NOT NULL")]
    for n, (s, first) in enumerate(todo, 1):
        cur = max(FROM, first - D)
        while cur < END + D:
            rows = _get("fundingRate", {"symbol": s, "startTime": cur, "endTime": END + D, "limit": 1000})
            time.sleep(0.65)                                        # fundingRate 한도 500회/5분
            if not rows:
                break
            c.executemany("INSERT OR REPLACE INTO fund VALUES (?,?,?)",
                          [(s, int(r["fundingTime"]), float(r["fundingRate"])) for r in rows if r.get("fundingRate") not in (None, "")])
            cur = int(rows[-1]["fundingTime"]) + 1
            if len(rows) < 1000:
                break
        c.execute("UPDATE sym SET done_f=1 WHERE symbol=?", (s,))
        c.commit()
        if n % 100 == 0:
            print(f"  펀딩 {n}/{len(todo)}", flush=True)
    print("끝:", c.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM k1d").fetchone(),
          c.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM fund").fetchone(), flush=True)


if __name__ == "__main__":
    main()
