"""XV — 같은 코인, 다른 계약/거래소의 펀딩 히스토리 수집(공개 API, 키 불필요).

venue:
  bn_usdt  바이낸스 USDT 마진   fapi  {C}USDT
  bn_usdc  바이낸스 USDC 마진   fapi  {C}USDC      (2024-01~)
  bn_coin  바이낸스 코인 마진   dapi  {C}USD_PERP  (인버스 — 증거금·손익이 코인)
  bybit    바이빗 USDT 무기한   v5    {C}USDT
  okx      OKX USDT 무기한      v5    {C}-USDT-SWAP (API 는 최근 ~3개월만 준다)

저장: data/xvenue.db 의 funding(venue, symbol, t, rate) — 엔진 캐시(candles.db)와 섞지 않는다.
이미 있으면 덮어쓴다(INSERT OR REPLACE), 다시 돌려도 안전.

    python3 -u -m research.xv_data              # 기본 코인 10개, 2021-01-01~
    python3 -u -m research.xv_data BTC ETH
    python3 -u -m research.xv_data --m-universe --venues bn_usdt,bybit   # M 의 56심볼, 두 곳만
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
from datetime import datetime, timezone

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "xvenue.db")
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "LINK", "LTC", "AVAX", "ADA")
SINCE = "2021-01-01"
UA = {"User-Agent": "research-client/0.1"}


def _get(url, params, retries=4):
    q = urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{url}?{q}", headers=UA), timeout=30) as r:
                return json.load(r)
        except (urllib.error.HTTPError, urllib.error.URLError):
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def _binance(base, symbol, start_ms, end_ms):
    out, cur = [], start_ms
    while cur < end_ms:
        rows = _get(f"{base}/fundingRate", {"symbol": symbol, "startTime": cur, "endTime": end_ms, "limit": 1000})
        if not isinstance(rows, list) or not rows:
            break
        out += [(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows]
        nxt = int(rows[-1]["fundingTime"]) + 1
        if nxt <= cur or len(rows) < 1000:
            break
        cur = nxt
        time.sleep(0.3)
    return out


def _bybit(symbol, start_ms, end_ms):
    out, end = [], end_ms
    while end > start_ms:
        r = _get("https://api.bybit.com/v5/market/funding/history",
                 {"category": "linear", "symbol": symbol, "endTime": end, "limit": 200})
        rows = (r.get("result") or {}).get("list") or []
        if not rows:
            break
        out += [(int(x["fundingRateTimestamp"]), float(x["fundingRate"])) for x in rows]
        nxt = int(rows[-1]["fundingRateTimestamp"]) - 1           # 최신→과거
        if nxt >= end or len(rows) < 200:
            break
        end = nxt
        time.sleep(0.15)
    return [x for x in out if x[0] >= start_ms]


def _okx(inst, start_ms):
    out, after = [], None
    while True:
        p = {"instId": inst, "limit": 100}
        if after:
            p["after"] = after
        rows = (_get("https://www.okx.com/api/v5/public/funding-rate-history", p) or {}).get("data") or []
        if not rows:
            break
        out += [(int(x["fundingTime"]), float(x["realizedRate"] or x["fundingRate"])) for x in rows]
        after = rows[-1]["fundingTime"]
        if int(after) < start_ms:
            break
        time.sleep(0.15)
    return [x for x in out if x[0] >= start_ms]


def fetch(venue, coin, start_ms, end_ms):
    if venue == "bn_usdt":
        return f"{coin}USDT", _binance("https://fapi.binance.com/fapi/v1", f"{coin}USDT", start_ms, end_ms)
    if venue == "bn_usdc":
        return f"{coin}USDC", _binance("https://fapi.binance.com/fapi/v1", f"{coin}USDC", start_ms, end_ms)
    if venue == "bn_coin":
        return f"{coin}USD_PERP", _binance("https://dapi.binance.com/dapi/v1", f"{coin}USD_PERP", start_ms, end_ms)
    if venue == "bybit":
        return f"{coin}USDT", _bybit(f"{coin}USDT", start_ms, end_ms)
    if venue == "okx":
        return f"{coin}-USDT-SWAP", _okx(f"{coin}-USDT-SWAP", start_ms)
    raise ValueError(venue)


VENUES = ("bn_usdt", "bn_usdc", "bn_coin", "bybit", "okx")


def conn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS funding (venue TEXT, coin TEXT, symbol TEXT, t INTEGER, rate REAL, "
              "PRIMARY KEY (venue, coin, t))")
    return c


def main():
    args = sys.argv[1:]
    venues = VENUES
    if "--venues" in args:                               # 예: --venues bn_usdt,bybit
        i = args.index("--venues"); venues = tuple(args[i + 1].split(",")); args = args[:i] + args[i + 2:]
    if "--m-universe" in args:                           # M 의 56심볼(2021 이전 상장) — candles.db 의 _1H 목록
        from engine import candle_store as cs
        cc = sqlite3.connect(cs.DB_PATH)
        args = sorted(r[0][:-len("USDT_1H")] for r in cc.execute("SELECT DISTINCT symbol FROM candle")
                      if r[0].endswith("USDT_1H"))
        cc.close()
    coins = [a.upper() for a in args] or list(COINS)
    start = int(datetime.fromisoformat(SINCE).replace(tzinfo=timezone.utc).timestamp() * 1000)
    end = int(time.time() * 1000)
    c = conn()
    for coin in coins:
        for v in venues:
            try:
                sym, rows = fetch(v, coin, start, end)
            except Exception as e:                       # 상장 안 된 계약 등
                print(f"  {coin:5} {v:8} 실패: {e}", flush=True)
                continue
            c.executemany("INSERT OR REPLACE INTO funding VALUES (?,?,?,?,?)",
                          [(v, coin, sym, t, r) for t, r in rows])
            c.commit()
            span = ""
            if rows:
                f = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")
                span = f"{f(min(t for t, _ in rows))} ~ {f(max(t for t, _ in rows))}"
            print(f"  {coin:5} {v:8} {len(rows):6,}건  {span}", flush=True)
    c.close()


if __name__ == "__main__":
    main()
