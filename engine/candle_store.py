"""로컬 캔들 캐시 (SQLite). 백테스트 반복 시 바이낸스 재수집 방지.

- 스키마는 candle-collector의 MySQL `coin.candle`과 동일 개념
  (open_time, symbol, open/high/low/close/volume). 나중에 실 DB 어댑터와 재사용.
- 증분 캐시: 요청 범위 중 '없는 구간만' 바이낸스에서 받아 저장.
  (연속 수집 가정 — head/tail만 확장하면 커버리지가 이어짐)
- 펀딩비율 히스토리도 같은 DB에 캐시.

CLI:
    python3 -m engine.candle_store BTCUSDT 60      # 60일치 미리 채우기
    python3 -m engine.candle_store --info          # 캐시 현황
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone

import numpy as np

from .candles import Candles, MINUTE_MS
from . import binance_data

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "candles.db")


def _conn(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE IF NOT EXISTS candle(
        symbol TEXT, open_time INTEGER,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY(symbol, open_time))""")
    c.execute("""CREATE TABLE IF NOT EXISTS funding(
        symbol TEXT, funding_time INTEGER, rate REAL,
        PRIMARY KEY(symbol, funding_time))""")
    # 마이그레이션: 구버전 DB엔 taker_buy 컬럼이 없음 → 추가(기존 행은 NULL).
    cols = [r[1] for r in c.execute("PRAGMA table_info(candle)")]
    if "taker_buy" not in cols:
        c.execute("ALTER TABLE candle ADD COLUMN taker_buy REAL")
    return c


def _coverage(conn, symbol):
    r = conn.execute("SELECT MIN(open_time), MAX(open_time), COUNT(*) FROM candle WHERE symbol=?",
                     (symbol,)).fetchone()
    return r  # (min, max, count) — count 0이면 (None, None, 0)


def ensure(symbol: str, start_ms: int, end_ms: int, db_path=DB_PATH, verbose=False) -> Candles:
    """[start_ms, end_ms] 1분봉을 보장 — 없는 head/tail만 수집 후 반환."""
    conn = _conn(db_path)
    mn, mx, cnt = _coverage(conn, symbol)

    to_fetch = []
    if cnt == 0:
        to_fetch.append((start_ms, end_ms))
    else:
        if start_ms < mn:
            to_fetch.append((start_ms, mn - MINUTE_MS))     # 과거쪽 확장
        if end_ms > mx:
            to_fetch.append((mx + MINUTE_MS, end_ms))        # 최신쪽 확장

    fetched = 0
    for s, e in to_fetch:
        if s > e:
            continue
        rows = binance_data.fetch_range_rows(symbol, "1m", s, e, verbose=verbose)
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO candle(symbol,open_time,open,high,low,close,volume,taker_buy) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(symbol, *r) for r in rows])
            conn.commit()
            fetched += len(rows)
    if verbose and fetched:
        print(f"  [캐시] {symbol} 신규 {fetched}개 저장")

    cur = conn.execute(
        "SELECT open_time,open,high,low,close,volume,taker_buy FROM candle "
        "WHERE symbol=? AND open_time BETWEEN ? AND ? ORDER BY open_time",
        (symbol, start_ms, end_ms))
    data = cur.fetchall()
    conn.close()
    if not data:
        raise ValueError(f"{symbol} 캔들 없음 ({start_ms}~{end_ms})")
    arr = np.array(data, dtype=float)   # NULL taker_buy는 numpy가 nan으로 변환
    return Candles(arr[:, 0].astype(np.int64), arr[:, 1], arr[:, 2], arr[:, 3],
                   arr[:, 4], arr[:, 5], timeframe_min=1, taker_buy=arr[:, 6])


def ensure_days(symbol: str, days: float, end_ms: int = None, db_path=DB_PATH, verbose=False) -> Candles:
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(days * 24 * 60) * MINUTE_MS
    return ensure(symbol, start_ms, end_ms, db_path=db_path, verbose=verbose)


def ensure_funding(symbol: str, days: float, end_ms: int = None, db_path=DB_PATH):
    """펀딩비율 히스토리 캐시 → [(time_ms, rate), ...] 반환."""
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(days * 24 * 60) * MINUTE_MS
    conn = _conn(db_path)
    mx = conn.execute("SELECT MAX(funding_time) FROM funding WHERE symbol=?", (symbol,)).fetchone()[0]
    # 최신쪽만 갱신 (없으면 전체)
    if mx is None or mx < end_ms - 8 * 3600 * 1000:
        try:
            fr = binance_data.fetch_funding(symbol, days=days, end_ms=end_ms)
            if fr:
                conn.executemany("INSERT OR IGNORE INTO funding VALUES(?,?,?)",
                                 [(symbol, t, r) for t, r in fr])
                conn.commit()
        except Exception:
            pass
    rows = conn.execute(
        "SELECT funding_time, rate FROM funding WHERE symbol=? AND funding_time BETWEEN ? AND ? "
        "ORDER BY funding_time", (symbol, start_ms, end_ms)).fetchall()
    conn.close()
    return [(int(t), float(r)) for t, r in rows]


def fill_range(symbol: str, start_ms: int, end_ms: int, db_path=DB_PATH, verbose=False) -> int:
    """[start_ms, end_ms] 구간의 '빠진 부분만' 수집 (겹치는 구간 스킵).

    ensure()와 달리 구간 내부의 구멍(hole)까지 감지해 채운다. 명시적 기간 수집용.
    반환: 신규 저장된 캔들 수.
    """
    conn = _conn(db_path)
    existing = [r[0] for r in conn.execute(
        "SELECT open_time FROM candle WHERE symbol=? AND open_time BETWEEN ? AND ? ORDER BY open_time",
        (symbol, start_ms, end_ms))]

    gaps = []
    if not existing:
        gaps.append((start_ms, end_ms))
    else:
        if existing[0] > start_ms:
            gaps.append((start_ms, existing[0] - MINUTE_MS))
        for a, b in zip(existing, existing[1:]):
            if b - a > MINUTE_MS:                       # 내부 구멍
                gaps.append((a + MINUTE_MS, b - MINUTE_MS))
        if existing[-1] < end_ms:
            gaps.append((existing[-1] + MINUTE_MS, end_ms))

    fetched = 0
    for s, e in gaps:
        if s > e:
            continue
        rows = binance_data.fetch_range_rows(symbol, "1m", s, e, verbose=verbose)
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO candle(symbol,open_time,open,high,low,close,volume,taker_buy) "
                "VALUES(?,?,?,?,?,?,?,?)", [(symbol, *r) for r in rows])
            conn.commit()
            fetched += len(rows)
    conn.close()
    return fetched


def backfill_taker(symbol: str, db_path=DB_PATH, verbose=False) -> int:
    """기존 캐시 행의 taker_buy(NULL)를 klines 재수집으로 채운다.

    구버전 캐시(OHLCV만)엔 taker_buy가 NULL이라 델타/CVD 지표가 NaN. 이 함수로
    NULL 구간을 재수집해 UPDATE. 반환: 재수집한 행 수.
    """
    conn = _conn(db_path)
    mn, mx, cnt = conn.execute(
        "SELECT MIN(open_time), MAX(open_time), COUNT(*) FROM candle "
        "WHERE symbol=? AND taker_buy IS NULL", (symbol,)).fetchone()
    if not cnt:
        conn.close()
        if verbose:
            print(f"  {symbol}: 채울 taker_buy 없음 (이미 최신)")
        return 0
    if verbose:
        print(f"  {symbol}: taker_buy 미보유 {cnt:,}개 재수집 중...")
    rows = binance_data.fetch_range_rows(symbol, "1m", mn, mx, verbose=verbose)
    if rows:
        conn.executemany("UPDATE candle SET taker_buy=? WHERE symbol=? AND open_time=?",
                         [(r[6], symbol, r[0]) for r in rows])
        conn.commit()
    conn.close()
    return len(rows)


def load_range(symbol: str, start_ms: int, end_ms: int, db_path=DB_PATH) -> Candles:
    """캐시에서 [start,end] 구간을 읽기만 함 (네트워크 수집 없음)."""
    conn = _conn(db_path)
    data = conn.execute(
        "SELECT open_time,open,high,low,close,volume,taker_buy FROM candle "
        "WHERE symbol=? AND open_time BETWEEN ? AND ? ORDER BY open_time",
        (symbol, start_ms, end_ms)).fetchall()
    conn.close()
    if not data:
        raise ValueError(f"{symbol} 캐시에 해당 구간 데이터 없음")
    arr = np.array(data, dtype=float)   # NULL taker_buy는 numpy가 nan으로 변환
    return Candles(arr[:, 0].astype(np.int64), arr[:, 1], arr[:, 2], arr[:, 3],
                   arr[:, 4], arr[:, 5], timeframe_min=1, taker_buy=arr[:, 6])


def load_recent(symbol: str, days: float, db_path=DB_PATH) -> Candles:
    """캐시된 최신 데이터에서 마지막 days일치를 읽음 (수집 안 함).

    기준은 '지금'이 아니라 '캐시된 최신 캔들' — 수집기가 안 돌아도 동작.
    """
    st = stats(symbol, db_path)
    if not st["count"]:
        raise ValueError(f"{symbol} 캐시 없음 — 먼저 '📥 데이터 수집' 탭에서 수집해줘")
    end = st["max"]
    start = end - int(days * 24 * 60) * MINUTE_MS
    return load_range(symbol, start, end, db_path)


def load_funding_cached(symbol: str, start_ms: int, end_ms: int, db_path=DB_PATH):
    """캐시된 펀딩비율만 읽음 (없으면 빈 리스트)."""
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT funding_time, rate FROM funding WHERE symbol=? AND funding_time BETWEEN ? AND ? "
        "ORDER BY funding_time", (symbol, start_ms, end_ms)).fetchall()
    conn.close()
    return [(int(t), float(r)) for t, r in rows]


def count_range(symbol: str, start_ms: int, end_ms: int, db_path=DB_PATH) -> int:
    conn = _conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM candle WHERE symbol=? AND open_time BETWEEN ? AND ?",
                     (symbol, start_ms, end_ms)).fetchone()[0]
    conn.close()
    return n


def list_stats(db_path=DB_PATH) -> list:
    """모든 심볼 캐시 현황 리스트."""
    if not os.path.exists(db_path):
        return []
    conn = _conn(db_path)
    rows = conn.execute(
        "SELECT symbol, COUNT(*), MIN(open_time), MAX(open_time) FROM candle GROUP BY symbol "
        "ORDER BY symbol").fetchall()
    conn.close()
    return [{"symbol": s, "count": c, "min": mn, "max": mx} for s, c, mn, mx in rows]


def stats(symbol: str, db_path=DB_PATH) -> dict:
    """심볼 캐시 현황: {count, min, max} (min/max는 ms epoch 또는 None)."""
    conn = _conn(db_path)
    mn, mx, cnt = _coverage(conn, symbol)
    conn.close()
    return {"count": cnt, "min": mn, "max": mx}


def info(db_path=DB_PATH):
    if not os.path.exists(db_path):
        print("캐시 없음:", db_path)
        return
    conn = _conn(db_path)
    print(f"캐시 DB: {db_path}  ({os.path.getsize(db_path)/1e6:.1f} MB)")
    rows = conn.execute(
        "SELECT symbol, COUNT(*), MIN(open_time), MAX(open_time) FROM candle GROUP BY symbol").fetchall()
    if not rows:
        print("  (비어있음)")
    for sym, cnt, mn, mx in rows:
        f = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"  {sym:12s} {cnt:>8,}개  {f(mn)} ~ {f(mx)} UTC")
    conn.close()


def main():
    import sys
    args = sys.argv[1:]
    if not args or args[0] == "--info":
        info()
        return
    if args[0] == "--backfill-taker":       # taker_buy 백필: 인자 심볼 또는 전체
        syms = [args[1].upper()] if len(args) > 1 else [s["symbol"] for s in list_stats()]
        for sym in syms:
            n = backfill_taker(sym, verbose=True)
            print(f"{sym}: taker_buy {n:,}개 채움")
        return
    symbol = args[0].upper()
    days = float(args[1]) if len(args) > 1 else 30
    print(f"{symbol} {days}일치 캐시 채우는 중...")
    c = ensure_days(symbol, days, verbose=True)
    print(f"완료: {len(c)}개 캔들 캐시됨")
    info()


if __name__ == "__main__":
    main()
