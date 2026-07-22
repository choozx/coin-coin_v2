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

PAGE_MINUTES = binance_data.MAX_LIMIT   # 바이낸스 API 1회 요청으로 받는 최대 1분봉 수(=1500분)


def _conn(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    # 동시 쓰기 안전: trader와 collector가 같은 candles.db에 붙는다.
    #   WAL      → 읽기/쓰기 동시 가능(리더가 라이터를 막지 않음).
    #   busy_timeout → 락 걸리면 즉시 에러 대신 5초 대기 → 'database is locked' 회피.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
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


def tail_gap_minutes(symbol: str, db_path=DB_PATH):
    """마지막 캐시 봉이 지금으로부터 몇 분(분봉) 뒤처졌나. 캐시 없으면 None(신규 심볼)."""
    st = stats(symbol, db_path)
    if not st["count"] or st["max"] is None:
        return None
    return (int(time.time() * 1000) - st["max"]) / MINUTE_MS


def backfill_step(symbol: str, seed_days: float, db_path=DB_PATH) -> tuple:
    """뒤처진/새 심볼을 '한 페이지(≤PAGE_MINUTES)'만 최신 방향으로 전진 수집한다.
    대량 백필을 한 번에 하지 않고 잘게 나눠 폴링 사이클을 막지 않게 하기 위함.
    반환: (fetched, caught_up). caught_up=True면 tail이 한 페이지 안 → 폴링 그룹 합류 가능."""
    now = int(time.time() * 1000)
    conn = _conn(db_path)
    mn, mx, cnt = _coverage(conn, symbol)
    conn.close()
    start = (mx + MINUTE_MS) if cnt else (now - int(seed_days * 24 * 60) * MINUTE_MS)
    if start >= now:
        return 0, True
    end = min(now, start + (PAGE_MINUTES - 1) * MINUTE_MS)   # 딱 한 요청 분량(PAGE_MINUTES봉)
    fetched = fill_range(symbol, start, end, db_path=db_path)
    st = stats(symbol, db_path)
    caught = st["max"] is not None and (now - st["max"]) <= PAGE_MINUTES * MINUTE_MS
    return fetched, caught


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


def backfill_funding(symbol: str, start_ms: int, end_ms: int = None, db_path=DB_PATH, verbose=False) -> int:
    """펀딩 히스토리를 [start, end] 전체 백필(페이지네이션). 반환: 저장된(신규 포함) 레코드 수."""
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    rows = binance_data.fetch_funding_range(symbol, start_ms, end_ms)
    if rows:
        conn = _conn(db_path)
        conn.executemany("INSERT OR IGNORE INTO funding VALUES(?,?,?)",
                         [(symbol, t, r) for t, r in rows])
        conn.commit()
        conn.close()
    if verbose:
        print(f"[funding] {symbol}: {len(rows)}건 수집")
    return len(rows)


def funding_schedule(symbol: str, start_ms: int, end_ms: int, db_path=DB_PATH) -> dict:
    """백테스트용 실제 펀딩 스케줄 {funding_time_ms: rate}. BacktestConfig.funding_schedule에 넣는다."""
    return {int(t): float(r) for t, r in load_funding_cached(symbol, start_ms, end_ms, db_path)}


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


def coverage_report(db_path=DB_PATH) -> list:
    """심볼별 커버리지+신선도+구멍 개수 (대시보드 데이터탭용).
    반환: [{symbol, count, min, max, ageMin(마지막봉 몇분전), gaps}, ...]."""
    now = int(time.time() * 1000)
    out = []
    for st in list_stats(db_path):
        mx = st.get("max")
        out.append({
            "symbol": st["symbol"], "count": st["count"], "min": st.get("min"), "max": mx,
            "ageMin": round((now - mx) / 60000, 1) if mx else None,
            "gaps": len(find_gaps(st["symbol"], db_path)),
        })
    return out


def find_gaps(symbol: str, db_path=DB_PATH) -> list:
    """저장된 [min,max] 구간 '내부'의 1분봉 결측을 찾는다 (head/tail 아님).
    반환: [(gap_start_ms, gap_end_ms), ...] — 빠진 첫 분 ~ 마지막 분(양끝 포함)."""
    conn = _conn(db_path)
    ot = np.array([r[0] for r in conn.execute(
        "SELECT open_time FROM candle WHERE symbol=? ORDER BY open_time", (symbol,))],
        dtype=np.int64)
    conn.close()
    if len(ot) < 2:
        return []
    diffs = np.diff(ot)
    idx = np.where(diffs > MINUTE_MS)[0]                     # 1분보다 벌어진 지점 = 구멍
    return [(int(ot[i] + MINUTE_MS), int(ot[i + 1] - MINUTE_MS)) for i in idx]


def heal_gaps(symbol: str, db_path=DB_PATH, verbose=True) -> dict:
    """내부 결측 구간을 바이낸스에서 재수집해 메운다. (자가치유)
    반환: {'gaps', 'expected'(결측 분봉수), 'filled'(복구), 'source_missing'(바이낸스에도 없음)}.
    소스에도 없는 분봉(거래소 정지 등)은 물리적 결측이라 남는다."""
    gaps = find_gaps(symbol, db_path)
    if not gaps:
        if verbose:
            print(f"[heal] {symbol}: 구멍 없음 ✓")
        return {"gaps": 0, "expected": 0, "filled": 0, "source_missing": 0}
    conn = _conn(db_path)
    filled = expected = 0
    for start, end in gaps:
        expected += int((end - start) // MINUTE_MS) + 1
        # end+1분: fetch_range_rows는 while cursor<end_ms 라 1분 구멍(start==end)이면 빈손 → 확장 후 필터
        rows = binance_data.fetch_range_rows(symbol, "1m", start, end + MINUTE_MS)
        rows = [r for r in rows if start <= int(r[0]) <= end]  # 구멍 구간만 (경계 밖 배제)
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO candle(symbol,open_time,open,high,low,close,volume,taker_buy) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(symbol, *r) for r in rows])
            conn.commit()
            filled += len(rows)
    conn.close()
    src_missing = expected - filled
    if verbose:
        tail = f", 소스결측 {src_missing}분(거래소에도 없음)" if src_missing else ""
        print(f"[heal] {symbol}: 구멍 {len(gaps)}곳/{expected}분 → 복구 {filled}분{tail}")
    return {"gaps": len(gaps), "expected": expected, "filled": filled, "source_missing": src_missing}


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
    if args[0] == "--heal":                 # 내부 구멍 스캔·복구: 인자 심볼 또는 전체
        syms = [args[1].upper()] if len(args) > 1 else [s["symbol"] for s in list_stats()]
        for sym in syms:
            heal_gaps(sym, verbose=True)
        return
    symbol = args[0].upper()
    days = float(args[1]) if len(args) > 1 else 30
    print(f"{symbol} {days}일치 캐시 채우는 중...")
    c = ensure_days(symbol, days, verbose=True)
    print(f"완료: {len(c)}개 캔들 캐시됨")
    info()


if __name__ == "__main__":
    main()
