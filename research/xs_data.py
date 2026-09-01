"""횡단면(cross-sectional) 연구용 다심볼 1시간봉 수집.

왜 별도 스크립트인가: 횡단면 전략은 '여러 심볼의 순위'를 쓰므로 현재 프리셋 스키마(단일 심볼)로
표현할 수 없다. 백테스트 엔진을 안 타므로 1분봉도 필요 없다 — 리밸런싱이 시간~일 단위다.
그래서 엔진을 건드리지 않고 `<SYMBOL>_1H` 이름으로 캐시에 넣는다(spot_data.py 와 같은 방식).
부정적 결론이 나오면 엔진을 고친 게 낭비가 되므로.

★ 유니버스는 **상장 시점**으로 고정한다. '오늘 거래량 상위 N개'를 고르면 살아남아 커진 종목만
   보게 되고(생존 편향), 그러면 가짜 엣지가 반드시 나온다 — 이 저장소가 여섯 번 피해온 함정이다.
   기준일 이전에 이미 상장돼 있던 심볼만 쓰고, 목록은 사후에 바꾸지 않는다.

   남은 편향(정직하게): exchangeInfo 는 **현재 거래중인 것만** 준다. 그때 상장됐다가 폐지된
   페어는 빠진다. 다만 오래된 코호트일수록 폐지율이 낮아(2020년 상장은 대부분 당시 메이저)
   최근 코호트보다 편향이 작다 — 오래된 코호트를 쓰는 것 자체가 완화책이다.

    python3 -m research.xs_data --list                 # 유니버스만 출력
    python3 -m research.xs_data                        # 수집(기본: 2021 이전 상장, 2021-01-01~)
    python3 -m research.xs_data --since 2022-01-01
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from engine import candle_store as cs

FAPI = "https://fapi.binance.com/fapi/v1"
LIMIT = 1500                  # 요청당 최대 캔들
PAGE_SLEEP = 0.3              # 레이트리밋 여유
HOUR_MS = 3_600_000


def xs_symbol(symbol: str) -> str:
    """캐시에 넣을 이름 — 기존 1분봉 캐시와 섞이지 않게."""
    return f"{symbol}_1H"


def _get(path: str, params: dict, retries: int = 4):
    q = urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            req = urllib.request.Request(f"{FAPI}/{path}?{q}",
                                         headers={"User-Agent": "auto-trading-research/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except (urllib.error.HTTPError, urllib.error.URLError):
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))
    return []


def universe(before: datetime) -> list:
    """`before` 이전에 상장된 USDT 무기한 심볼 — (심볼, 상장일) 오름차순."""
    info = _get("exchangeInfo", {})
    out = []
    for s in info["symbols"]:
        if (s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL"
                and s.get("status") == "TRADING" and s.get("onboardDate")):
            on = datetime.fromtimestamp(s["onboardDate"] / 1000, timezone.utc)
            if on < before:
                out.append((s["symbol"], on))
    return sorted(out, key=lambda x: x[1])


def collect(symbol: str, start_ms: int, end_ms: int) -> int:
    """1h 캔들을 받아 <SYMBOL>_1H 로 저장. 이미 있으면 덮어쓴다(INSERT OR REPLACE)."""
    sym = xs_symbol(symbol)
    conn = sqlite3.connect(cs.DB_PATH)
    cursor, total = start_ms, 0
    while cursor < end_ms:
        rows = _get("klines", {"symbol": symbol, "interval": "1h",
                               "startTime": cursor, "endTime": end_ms, "limit": LIMIT})
        if not rows:
            break
        vals = [(sym, int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
                for k in rows if int(k[0]) < end_ms]
        if vals:
            conn.executemany("INSERT OR REPLACE INTO candle"
                             "(symbol, open_time, open, high, low, close, volume) "
                             "VALUES (?,?,?,?,?,?,?)", vals)
            conn.commit()
            total += len(vals)
        nxt = int(rows[-1][0]) + HOUR_MS
        if nxt <= cursor or len(rows) < LIMIT:
            break
        cursor = nxt
        time.sleep(PAGE_SLEEP)
    conn.close()
    return total


def main():
    ap = argparse.ArgumentParser(description="횡단면 연구용 다심볼 1h 수집")
    ap.add_argument("--before", default="2021-01-01", help="이 날짜 이전 상장분만 (유니버스 고정)")
    ap.add_argument("--since", default=None, help="수집 시작일 (기본: --before 와 같음)")
    ap.add_argument("--list", action="store_true", help="유니버스만 출력하고 끝")
    a = ap.parse_args()

    before = datetime.fromisoformat(a.before).replace(tzinfo=timezone.utc)
    since = datetime.fromisoformat(a.since or a.before).replace(tzinfo=timezone.utc)
    uni = universe(before)
    print(f"유니버스: {a.before} 이전 상장 {len(uni)}개 · 수집 시작 {since:%Y-%m-%d}", flush=True)
    if a.list:
        for s, on in uni:
            print(f"  {on:%Y-%m-%d}  {s}")
        return

    start_ms, end_ms = int(since.timestamp() * 1000), int(time.time() * 1000)
    t0, grand = time.time(), 0
    for i, (s, on) in enumerate(uni, 1):
        try:
            n = collect(s, start_ms, end_ms)
        except Exception as e:
            print(f"  [{i}/{len(uni)}] {s:<14} 실패: {e}", flush=True)
            continue
        grand += n
        el = time.time() - t0
        eta = el / i * (len(uni) - i)
        print(f"  [{i}/{len(uni)}] {s:<14} {n:6,}봉  누적 {grand:,}  "
              f"경과 {el/60:.1f}분  남은예상 {eta/60:.1f}분", flush=True)
    print(f"\n완료 — {len(uni)}심볼 {grand:,}봉, {(time.time()-t0)/60:.1f}분", flush=True)


if __name__ == "__main__":
    main()
