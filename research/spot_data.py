"""현물(spot) 캔들 수집 — 베이시스 측정 전용. 연구용이라 엔진을 건드리지 않는다.

engine.binance_data 는 선물 전용(fapi.binance.com)이다. 베이시스(선물-현물 괴리)를 재려면
현물(api.binance.com/api/v3)이 필요한데, 이게 실제 봇에 쓰일지는 H 검증 결과에 달렸다.
그래서 엔진을 확장하는 대신 여기서 받아 candles.db 에 `<SYMBOL>_SPOT` 심볼로 넣는다
(부정적 결론이 나오면 엔진을 건드린 게 낭비가 되므로).

1분봉이 아니라 **1시간봉**을 받는다 — 캐리 진입·청산은 드물고 우리가 볼 것은 괴리의
수준과 분포이지 초단기 변동이 아니다. 6.9년 = 6만봉이라 몇 분이면 끝난다.

    python3 -m research.spot_data BTCUSDT 1h
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from engine import candle_store as cs

SPOT = "https://api.binance.com/api/v3/klines"
LIMIT = 1000
MIN_MS = 60_000


def spot_symbol(symbol: str) -> str:
    """캐시에 넣을 이름 — 선물과 섞이지 않게 접미사를 붙인다."""
    return f"{symbol}_SPOT"


def _get(symbol: str, interval: str, start_ms: int, retries: int = 4):
    q = urllib.parse.urlencode({"symbol": symbol, "interval": interval,
                                "startTime": start_ms, "limit": LIMIT})
    for i in range(retries):
        try:
            req = urllib.request.Request(SPOT + "?" + q, headers={"User-Agent": "auto-trading/0.1"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))            # 레이트리밋/일시 오류 → 백오프
    return []


def collect(symbol: str, interval: str, start_ms: int, end_ms: int, verbose: bool = True) -> int:
    """현물 캔들을 받아 candles.db 의 <SYMBOL>_SPOT 로 저장. 반환: 저장 건수."""
    step = cs.MINUTE_MS if interval == "1m" else {"1h": 60, "4h": 240, "1d": 1440}[interval] * MIN_MS
    sym = spot_symbol(symbol)
    conn = sqlite3.connect(cs.DB_PATH)      # 테이블은 이미 있다(선물 캐시) — 컬럼을 명시해 넣는다
    cursor, total = start_ms, 0
    while cursor < end_ms:
        rows = _get(symbol, interval, cursor)
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
        nxt = int(rows[-1][0]) + step
        if nxt <= cursor:
            break
        cursor = nxt
        if verbose and total % 10000 < LIMIT:
            print(f"  ... {total:,}봉", flush=True)
        if len(rows) < LIMIT:
            break
    conn.close()
    return total


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    interval = sys.argv[2] if len(sys.argv) > 2 else "1h"
    # 선물 캐시가 덮는 구간과 같은 범위를 받는다(그래야 짝지어 비교할 수 있다).
    st = cs.stats(symbol)
    if not st["count"]:
        raise SystemExit(f"선물 캐시에 {symbol} 이 없다 — 먼저 수집할 것")
    print(f"[현물수집] {symbol} {interval} · 선물 캐시 구간에 맞춤")
    n = collect(symbol, interval, int(st["min"]), int(st["max"]))
    print(f"[현물수집] 완료 — {spot_symbol(symbol)} {n:,}봉")


if __name__ == "__main__":
    main()
